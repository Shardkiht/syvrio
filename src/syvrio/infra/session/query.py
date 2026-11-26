from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from heapq import merge
from itertools import islice
from pathlib import Path

from ..storage.database import transaction
from ..storage.documents import DocumentError
from ..storage.paths import SESSION_ID_RE, SyvrioPaths, vault_paths
from ..storage.session_indexes import PENDING_USER, search_signature
from ..storage.session_previews import PREVIEW_SIZE
from .attachments import AttachmentBatch
from .model import Session


@dataclass
class HistoryWindow:
    id: str
    title: str
    summary: str | None
    records: list[dict]
    earlier_cursor: int | None


class SessionQuery:
    def __init__(self, vault: Path | SyvrioPaths):
        self.paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
        self.attachments_dir = self.paths.attachments_dir
        self.runtime_dir = self.paths.runtime_dir

    @staticmethod
    def _validate_session_id(session_id):
        if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
            raise KeyError("非法会话 ID")

    def exists(self, session_id, *, archived=False):
        self._validate_session_id(session_id)
        with transaction(self.paths, write=False) as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM sessions WHERE id=? AND archived=?", (session_id, archived)
                ).fetchone()
            )

    def load(self, session_id, *, include_reasoning=True, archived=False):
        self._validate_session_id(session_id)
        with transaction(self.paths, write=False) as db:
            session = db.execute(
                "SELECT started_at FROM sessions WHERE id=? AND archived=?", (session_id, archived)
            ).fetchone()
            if not session:
                raise KeyError(session_id)
            select = (
                "SELECT e.*, r.body FROM session_events e LEFT JOIN session_reasoning r ON r.event_id=e.seq"
                if include_reasoning
                else "SELECT e.*,NULL AS body FROM session_events e"
            )
            event_attachments = {}
            for attachment in db.execute(
                """SELECT a.message_id,a.id,a.name,a.media_type,a.sha256,o.size FROM attachments a JOIN attachment_objects o USING(sha256) WHERE a.session_id=? ORDER BY a.rowid""",
                (session_id,),
            ):
                meta = dict(attachment)
                event_attachments.setdefault(meta.pop("message_id"), []).append(meta)
            records = []
            for row in db.execute(select + " WHERE e.session_id=? ORDER BY e.seq", (session_id,)):
                records.append(self._record(row, event_attachments))
            return Session(id=session_id, started_at=session[0], records=records)

    @staticmethod
    def _record(row, attachments):
        record = json.loads(row["payload"])
        record["type"] = row["kind"]
        if row["message_id"] is not None:
            record["id"] = row["message_id"]
        if row["role"] is not None:
            record["role"] = row["role"]
        if row["kind"] != "meta":
            record["at"] = row["at"]
        if row["content"] is not None:
            record["content"] = row["content"]
        if row["kind"] == "tool" and "tool_length" in row.keys() and row["tool_length"] is not None:
            record["tool_content_length"] = row["tool_length"]
        if row["body"]:
            record["reasoning"] = json.loads(row["body"])
        if parts := attachments.get(row["message_id"]):
            record["attachments"] = parts
        return record

    def context_history(
        self, session_id: str, *, include_reasoning: bool, active_message_id: str | None
    ) -> tuple[str, list[dict], bool]:
        """只取上下文实际重放的记录。已压缩的历史、附件原件及控制事件不参与统计。"""
        with transaction(self.paths, write=False) as db:
            summary = db.execute(
                """
                SELECT content, json_extract(payload,'$.covered_records') FROM session_events
                WHERE session_id=? AND active=1 AND kind='summary' ORDER BY seq DESC LIMIT 1
            """,
                (session_id,),
            ).fetchone()
            covered = int(summary[1] or 0) if summary else 0
            # covered_records 是该会话原始事件的数量，多个会话的全库 seq 可以交错。
            start = ""
            args = [session_id]
            if covered:
                start = """AND e.seq >= (SELECT seq FROM session_events WHERE session_id=?
                           ORDER BY seq LIMIT 1 OFFSET ?)"""
                args.extend((session_id, min(covered, 2**63 - 1)))
            reasoning = (
                "LEFT JOIN session_reasoning r ON r.event_id=e.seq" if include_reasoning else ""
            )
            body = "r.body" if include_reasoning else "NULL"
            rows = db.execute(
                f"""
                SELECT e.kind,e.message_id,e.role,e.at,e.payload,e.content,{body} AS body
                FROM session_events e {reasoning} WHERE e.session_id=? AND e.active=1
                AND e.kind IN ('message','assistant','tool') {start} ORDER BY e.seq
            """,
                args,
            )
            history = [self._record(row, {}) for row in rows]
            active = False
            if active_message_id:
                last = db.execute(
                    """SELECT message_id FROM session_events WHERE session_id=? AND active=1
                    AND kind='message' AND role='user' ORDER BY seq DESC LIMIT 1""",
                    (session_id,),
                ).fetchone()
                active = last is not None and last[0] == active_message_id
            return summary[0] or "" if summary else "", history, active

    def session_title(self, session_id: str) -> str:
        with transaction(self.paths, write=False) as db:
            row = db.execute(
                "SELECT title FROM sessions WHERE id=? AND archived=0", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            return row[0] or "未命名会话"

    def history_version(self, session_id: str) -> int:
        with transaction(self.paths, write=False) as db:
            row = db.execute(
                "SELECT history_revision FROM sessions WHERE id=? AND archived=0", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            return row[0]

    def history_window(
        self,
        session_id: str,
        limit: int,
        *,
        archived: bool,
        before_seq: int | None,
        tool_preview: int,
    ) -> HistoryWindow:
        self._validate_session_id(session_id)
        if before_seq is not None and not (1 <= before_seq < 2**63):
            raise ValueError("Invalid history cursor.")
        if not (0 <= tool_preview <= PREVIEW_SIZE):
            raise ValueError("Invalid history preview size.")
        with transaction(self.paths, write=False) as db:
            session = db.execute(
                "SELECT title FROM sessions WHERE id=? AND archived=?", (session_id, archived)
            ).fetchone()
            if session is None:
                raise KeyError(session_id)
            bounds = " AND e.seq < ?" if before_seq is not None else ""
            args = (session_id, before_seq) if before_seq is not None else (session_id,)
            db.create_function("tool_excerpt", 1, lambda text: (text or "")[:PREVIEW_SIZE])
            db.create_function("tool_length", 1, lambda text: len(text or ""))
            preview = "e.kind='tool' AND e.message_id IS NOT NULL"
            select = f"""SELECT e.seq,e.kind,e.message_id,e.role,e.at,e.payload,r.body,
                CASE WHEN {preview} THEN coalesce(p.preview,tool_excerpt(e.content)) ELSE e.content END AS content,
                CASE WHEN {preview} THEN coalesce(p.length,tool_length(e.content)) END AS tool_length FROM session_events e
                LEFT JOIN tool_previews p ON p.event_id=e.seq
                LEFT JOIN session_reasoning r ON r.event_id=e.seq
                WHERE e.session_id=? AND e.active=1
                AND e.kind IN ('message','assistant','tool','summary','turn_failed')"""
            rows = db.execute(
                select + bounds + " ORDER BY e.seq DESC LIMIT ?", (*args, limit + 1)
            ).fetchall()
            has_earlier = len(rows) > limit
            rows = rows[:limit]
            if has_earlier and rows[-1]["kind"] == "tool":
                # 工具结果和前一个助手调用一起展示，游标不能切在这组记录中间。
                first = rows[-1]["seq"]
                owner = db.execute(
                    """SELECT seq,kind FROM session_events WHERE session_id=?
                    AND active=1 AND kind IN ('message','assistant','summary','turn_failed')
                    AND seq<? ORDER BY seq DESC LIMIT 1""",
                    (session_id, first),
                ).fetchone()
                if owner is not None and owner["kind"] == "assistant":
                    rows.extend(
                        db.execute(
                            select + " AND e.seq>=? AND e.seq<? ORDER BY e.seq DESC",
                            (session_id, owner["seq"], first),
                        )
                    )
                    has_earlier = (
                        db.execute(
                            """SELECT 1 FROM session_events WHERE session_id=? AND active=1
                        AND kind IN ('message','assistant','tool','summary','turn_failed') AND seq<? LIMIT 1""",
                            (session_id, owner["seq"]),
                        ).fetchone()
                        is not None
                    )
            ids = [r["message_id"] for r in rows if r["kind"] == "message"]
            attachments = {}
            if ids:
                for item in db.execute(
                    """SELECT a.message_id,a.id,a.name,a.media_type,a.sha256,o.size
                    FROM attachments a JOIN attachment_objects o USING(sha256) WHERE a.session_id=?
                    AND a.message_id IN (SELECT value FROM json_each(?)) ORDER BY a.rowid""",
                    (session_id, json.dumps(ids)),
                ):
                    data = dict(item)
                    attachments.setdefault(data.pop("message_id"), []).append(data)
            summary = db.execute(
                """SELECT content FROM session_events WHERE session_id=? AND active=1
                AND kind='summary' ORDER BY seq DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
            records = [self._record(row, attachments) for row in reversed(rows)]
            if tool_preview < PREVIEW_SIZE:
                for record in records:
                    if record.get("tool_content_length") is not None:
                        record["content"] = record["content"][:tool_preview]
            return HistoryWindow(
                session_id,
                session[0] or "未命名会话",
                summary[0] if summary else None,
                records,
                rows[-1]["seq"] if has_earlier else None,
            )

    def tool_content(self, session_id: str, record_id: str, *, archived: bool) -> str | None:
        with transaction(self.paths, write=False) as db:
            row = db.execute(
                """SELECT e.content,e.seq FROM sessions s LEFT JOIN session_events e
                ON e.session_id=s.id AND e.message_id=? AND e.active=1 AND e.kind='tool'
                WHERE s.id=? AND s.archived=?""",
                (record_id, session_id, archived),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            return (row["content"] or "") if row["seq"] is not None else None

    def copy_message_attachment(
        self, session_id, message_id, attachment_id, destination, *, archived=False
    ):
        self._validate_session_id(session_id)
        with transaction(self.paths, write=False) as db:
            row = db.execute(
                """SELECT a.id,a.name,a.media_type,a.sha256,o.size FROM attachments a
                JOIN attachment_objects o ON o.sha256=a.sha256
                JOIN sessions s ON s.id=a.session_id
                JOIN session_events e ON (e.session_id,e.message_id)=(a.session_id,a.message_id)
                WHERE s.id=? AND s.archived=? AND e.active=1 AND a.message_id=? AND a.id=?""",
                (session_id, archived, message_id, attachment_id),
            ).fetchone()
        if row is None:
            return None
        return AttachmentBatch.copy_original(
            self.attachments_dir, session_id, message_id, dict(row), destination
        )

    def verify_attachments(self):
        with transaction(self.paths, write=False) as db:
            rows = db.execute(
                "SELECT a.*,o.size FROM attachments a JOIN attachment_objects o USING(sha256)"
            ).fetchall()
        checked = 0
        errors = []
        for row in rows:
            meta = dict(row)
            sid, mid = meta.pop("session_id"), meta.pop("message_id")
            try:
                AttachmentBatch.restore(self.attachments_dir, sid, mid, [meta])
                checked += 1
            except (OSError, ValueError, DocumentError) as exc:
                errors.append(f"{sid}/{mid} ({meta['name']}): {exc}")
        return checked, errors

    def list(self, limit=20, with_tokens=True, *, archived=False, offset=0):
        with transaction(self.paths, write=False) as db:
            rows = []
            for row in db.execute(
                """SELECT id,started_at,updated_at,last_message_at,messages,summaries,title,tokens FROM sessions
                WHERE archived=? ORDER BY last_message_at DESC,id ASC LIMIT ? OFFSET ?""",
                (archived, max(0, limit), max(0, offset)),
            ):
                item = dict(row)
                item["title"] = item["title"] or "未命名会话"
                if not with_tokens:
                    item["tokens"] = 0
                rows.append(item)
            return rows

    def pending_target(self, session_id: str, message_id: str | None = None) -> sqlite3.Row | None:
        """返回指定待沉淀消息，或最新一条待沉淀消息的位置与 ID。"""
        with transaction(self.paths, write=False) as db:
            match = " AND message_id=?" if message_id is not None else ""
            args = (session_id, message_id) if message_id is not None else (session_id,)
            return db.execute(
                f"""SELECT seq,message_id FROM session_events WHERE session_id=?
                AND {PENDING_USER} AND EXISTS(SELECT 1 FROM sessions WHERE id=session_id AND archived=0)"""
                + match
                + " ORDER BY seq DESC LIMIT 1",
                args,
            ).fetchone()

    def first_pending(self, session_id: str, through: str | None = None) -> dict | None:
        with transaction(self.paths, write=False) as db:
            bound = (
                " AND seq <= (SELECT seq FROM session_events WHERE session_id=? AND message_id=?)"
                if through is not None
                else ""
            )
            args = (session_id, session_id, through) if through is not None else (session_id,)
            row = db.execute(
                f"""SELECT message_id,content,at,payload FROM session_events
                WHERE session_id=? AND {PENDING_USER}
                AND EXISTS(SELECT 1 FROM sessions WHERE id=session_id AND archived=0)"""
                + bound
                + " ORDER BY seq LIMIT 1",
                args,
            ).fetchone()
            if row is not None:
                return {
                    **json.loads(row["payload"]),
                    "id": row["message_id"],
                    "content": row["content"],
                    "at": row["at"],
                }

    def has_pending_through(self, session_id: str, through: str) -> bool:
        with transaction(self.paths, write=False) as db:
            return (
                db.execute(
                    f"""SELECT 1 FROM session_events WHERE session_id=? AND {PENDING_USER}
                AND seq <= (SELECT seq FROM session_events WHERE session_id=? AND message_id=?) LIMIT 1""",
                    (session_id, session_id, through),
                ).fetchone()
                is not None
            )

    def pending_sessions(self) -> list[str]:
        with transaction(self.paths, write=False) as db:
            return [
                r[0]
                for r in db.execute("""SELECT id FROM sessions WHERE archived=0 AND undistilled>0
                ORDER BY updated_at DESC,id DESC""")
            ]

    def unconsolidated(self, limit=3, exclude=None, min_new_messages=1):
        with transaction(self.paths, write=False) as db:
            return [
                self.load(row[0], include_reasoning=False)
                for row in db.execute(
                    """SELECT id FROM sessions
                WHERE archived=0 AND id!=? AND undistilled>=? ORDER BY updated_at DESC,id DESC LIMIT ?""",
                    (exclude or "", max(1, min_new_messages), max(0, limit)),
                )
            ]

    def search_chats(self, query: str, limit: int = 20, *, exclude_session: str = "") -> dict:
        keyword = query.strip()
        if not 1 <= len(keyword) <= 160 or not 1 <= limit <= 50:
            raise ValueError("Enter 1–160 characters and request 1–50 matches.")
        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
        hits = []
        with transaction(self.paths, write=False) as db:
            db.create_function(
                "matches_keyword", 1, lambda text: bool(pattern.search(text)), deterministic=True
            )
            c0, c1, p0, p1 = search_signature(keyword)
            titles = db.execute(
                """SELECT started_at,id,1,0,coalesce(nullif(title,''),'未命名会话'),
                          '', 'title', coalesce(nullif(title,''),'未命名会话')
                   FROM sessions WHERE archived=0 AND id<>?
                     AND matches_keyword(coalesce(nullif(title,''),'未命名会话'))
                   ORDER BY started_at DESC,id DESC LIMIT ?""",
                (exclude_session, limit),
            )
            # 签名按展示顺序存放。全库只走一个游标，先筛候选再读正文，凑齐即可停。
            messages = db.execute(
                """SELECT g.started_at,g.session_id,0,g.event_id,coalesce(nullif(s.title,''),'未命名会话'),
                          e.message_id,coalesce(e.role,'assistant'),e.content
                   FROM search_signatures g CROSS JOIN sessions s ON s.id=g.session_id
                   CROSS JOIN session_events e ON e.seq=g.event_id
                   WHERE g.session_id<>?1 AND (g.c0 & ?2)=?2 AND (g.c1 & ?3)=?3
                     AND (g.p0 & ?4)=?4 AND (g.p1 & ?5)=?5 AND s.archived=0
                     AND e.session_id=s.id AND e.active=1 AND e.content IS NOT NULL
                     AND e.kind IN ('message','assistant') AND matches_keyword(e.content)
                   ORDER BY g.started_at DESC,g.session_id DESC,g.event_id DESC LIMIT ?6""",
                (exclude_session, c0, c1, p0, p1, limit),
            )
            ordered = merge(titles, messages, key=lambda row: row[:4], reverse=True)
            for _, sid, heading, _, title, mid, role, content in islice(ordered, limit):
                excerpt = content
                if not heading:
                    match = pattern.search(content)
                    start, end = max(0, match.start() - 48), min(len(content), match.end() + 80)
                    excerpt = (
                        ("…" if start else "")
                        + content[start:end]
                        + ("…" if end < len(content) else "")
                    )
                hits.append(
                    dict(session_id=sid, title=title, message_id=mid, role=role, excerpt=excerpt)
                )
        return {"results": hits}

    def search_message(self, session_id: str, message_id: str) -> dict:
        self._validate_session_id(session_id)
        columns = "seq,message_id,coalesce(role,'assistant') AS role,content,at"
        visible = "session_id=? AND active=1 AND kind IN ('assistant','message') AND content<>''"
        with transaction(self.paths, write=False) as db:
            session = db.execute(
                "SELECT title FROM sessions WHERE id=? AND archived=0", (session_id,)
            ).fetchone()
            if session is None:
                raise KeyError(session_id)
            hit = db.execute(
                f"SELECT {columns} FROM session_events WHERE {visible} AND message_id=?",
                (session_id, message_id),
            ).fetchone()
            if hit is None:
                raise KeyError(message_id)
            neighbors = []
            for operator, order in (("<", "DESC"), (">", "ASC")):
                neighbors.append(
                    db.execute(
                        f"SELECT {columns} FROM session_events WHERE {visible} AND seq{operator}? ORDER BY seq {order} LIMIT 1",
                        (session_id, hit["seq"]),
                    ).fetchone()
                )
            return {
                "title": session[0] or "未命名会话",
                "messages": [
                    {key: row[key] for key in ("message_id", "role", "content", "at")}
                    for row in (neighbors[0], hit, neighbors[1])
                    if row is not None
                ],
            }

    def _source(self, session_id, message_id, *, active_only, include_archived):
        self._validate_session_id(session_id)
        if not isinstance(message_id, str) or not SESSION_ID_RE.fullmatch(message_id):
            raise ValueError("Invalid source message ID.")
        with transaction(self.paths, write=False) as db:
            row = db.execute(
                """
                SELECT s.title,s.archived,e.kind,e.role,e.content,e.at,
                       json_extract(e.payload,'$.original_local_date') AS local_date
                FROM sessions s LEFT JOIN session_events e ON e.session_id=s.id
                  AND e.message_id=? AND (e.active=1 OR ?=0)
                WHERE s.id=? AND (s.archived=0 OR ?=1)
            """,
                (message_id, active_only, session_id, include_archived),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        if row["kind"] != "message" or row["role"] != "user":
            raise ValueError("Source does not identify a unique user message.")
        result = {"content": row["content"], "at": row["at"]}
        if row["local_date"] is not None:
            result["original_local_date"] = row["local_date"]
        if include_archived:
            result.update(title=row["title"] or "未命名会话", archived=bool(row["archived"]))
        return result

    def user_message(self, session_id: str, message_id: str, *, active_only: bool = False) -> dict:
        return self._source(session_id, message_id, active_only=active_only, include_archived=False)

    def review_source(self, session_id: str, message_id: str) -> dict:
        return self._source(session_id, message_id, active_only=False, include_archived=True)
