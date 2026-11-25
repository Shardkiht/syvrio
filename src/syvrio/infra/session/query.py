from __future__ import annotations

import json
import re
from pathlib import Path

from ..storage.database import transaction
from ..storage.documents import DocumentError
from ..storage.paths import SESSION_ID_RE, SyvrioPaths, vault_paths
from .attachments import AttachmentBatch
from .model import Session


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
                if row["body"]:
                    record["reasoning"] = json.loads(row["body"])
                if parts := event_attachments.get(row["message_id"]):
                    record["attachments"] = parts
                records.append(record)
            return Session(id=session_id, started_at=session[0], records=records)

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

    @staticmethod
    def _chat_messages(session: Session):
        for record in session.active_records():
            kind = record["type"]
            if kind in {"assistant", "message"}:
                if record.get("content"):
                    yield {
                        "message_id": record["id"],
                        "role": record.get("role", "assistant"),
                        "content": record["content"],
                        "at": record.get("at", ""),
                    }

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
            rows = db.execute(
                """
                WITH visible AS (
                    SELECT id, started_at, coalesce(nullif(title,''), ?) AS title
                    FROM sessions WHERE archived=0 AND id<>?
                ), matches AS (
                    SELECT id, started_at, title, 1 AS heading, 0 AS seq, '' AS message_id,
                           'title' AS role, title AS content FROM visible WHERE matches_keyword(title)
                    UNION ALL
                    SELECT s.id, s.started_at, s.title, 0, e.seq, e.message_id,
                           coalesce(e.role,'assistant'), e.content
                    FROM visible s JOIN session_events e ON e.session_id=s.id
                    WHERE e.active=1 AND e.kind IN ('assistant','message')
                      AND e.content IS NOT NULL AND matches_keyword(e.content)
                )
                SELECT * FROM matches ORDER BY started_at DESC,id DESC,heading DESC,seq DESC LIMIT ?
            """,
                ("未命名会话", exclude_session, limit),
            )
            for row in rows:
                content = row["content"]
                excerpt = content
                if not row["heading"]:
                    match = pattern.search(content)
                    start, end = max(0, match.start() - 48), min(len(content), match.end() + 80)
                    excerpt = (
                        ("…" if start else "")
                        + content[start:end]
                        + ("…" if end < len(content) else "")
                    )
                hits.append(
                    {
                        "session_id": row["id"],
                        "title": row["title"],
                        "message_id": row["message_id"],
                        "role": row["role"],
                        "excerpt": excerpt,
                    }
                )
        return {"results": hits}

    def search_message(self, session_id: str, message_id: str) -> dict:
        session = self.load(session_id, include_reasoning=False)
        messages = list(self._chat_messages(session))
        for i, message in enumerate(messages):
            if message["message_id"] == message_id:
                return {"title": session.title(), "messages": messages[max(0, i - 1) : i + 2]}
        raise KeyError(message_id)

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
