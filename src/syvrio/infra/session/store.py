from __future__ import annotations

import json
import secrets
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Any

from ..llm.tokens import estimate_tokens
from ..security.secrets import redact_secrets
from ..storage.database import encode, on_commit, transaction
from ..storage.session_indexes import PENDING_USER, TOOL_PREVIEW_SIZE, index_message
from ..time import serialize_timestamp, timestamp_calendar_date, utc_now
from .attachment_objects import cleanup
from .errors import ConversationError
from .model import Session, new_message_id, now_iso, validate_record
from .query import SessionQuery
from .receipts import ReceiptStore


@dataclass(frozen=True)
class HistoryRevision:
    session_id: str
    revision: int


@dataclass(frozen=True)
class SessionSnapshot:
    session: Session
    version: int
    history_revision: HistoryRevision


def _redact_record(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: _redact_record(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_record(item) for item in value]
    return value


def _stamp_event(record: dict) -> None:
    if record.get("type") in {"message", "assistant"} and "id" not in record:
        record["id"] = new_message_id()
    supplied = record.get("at")
    record.pop("original_local_date", None)
    if "at" in record:
        if record.get("type") == "message" and record.get("role") == "user":
            record["original_local_date"] = timestamp_calendar_date(supplied).isoformat()
    else:
        record["at"] = now_iso()
    validate_record(record)


class SessionStore(SessionQuery):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._snapshot_lock = RLock()
        self._snapshots = OrderedDict()
        self.receipts = ReceiptStore(self.paths)

    @staticmethod
    def publish_records(target: Session, updated: Session) -> None:
        """外层提交后再替换活跃会话的内存记录。"""
        on_commit(lambda: setattr(target, "records", updated.records))

    def load_snapshot(self, session_id):
        return self.read_snapshot(session_id).session

    def read_snapshot(self, session_id):
        self._validate_session_id(session_id)
        with self._snapshot_lock, transaction(self.paths, write=False) as db:
            row = db.execute(
                """SELECT revision,history_revision,
                (SELECT max(seq) FROM session_events WHERE session_id=s.id)
                FROM sessions s WHERE id=? AND archived=0""",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            version, history_version, last_seq = row
            cached = self._snapshots.get(session_id)
            if cached and cached[0].version == version:
                return deepcopy(cached[0])
            if (
                cached
                and cached[0].history_revision.revision == history_version
                and cached[0].version < version
            ):
                session = deepcopy(cached[0].session)
                for kind, at, payload in db.execute(
                    """SELECT kind,at,payload FROM session_events
                        WHERE session_id=? AND seq>? ORDER BY seq""",
                    (session_id, cached[1]),
                ):
                    session.records.append({**json.loads(payload), "type": kind, "at": at})
            else:
                session = self.load(session_id)
            snapshot = SessionSnapshot(
                session, version, HistoryRevision(session_id, history_version)
            )

            def publish():
                with self._snapshot_lock:
                    self._snapshots[session_id] = deepcopy(snapshot), last_seq
                    self._snapshots.move_to_end(session_id)
                    if len(self._snapshots) > 16:
                        self._snapshots.popitem(last=False)

            on_commit(publish)
            return snapshot

    def create(self):
        stamp = utc_now()
        started = serialize_timestamp(stamp.replace(microsecond=0))
        with transaction(self.paths) as db:
            while True:
                key = f"s-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
                if db.execute(
                    "INSERT INTO sessions(id,started_at) VALUES(?,?) ON CONFLICT DO NOTHING",
                    (key, started),
                ).rowcount:
                    break
            meta = {"type": "meta", "id": key, "started_at": started}
            self._insert(db, key, meta)
            return Session(id=key, started_at=started, records=[meta])

    def recover_deletions(self):
        # 后端持有独占租约，此时尚未接收上传。
        import shutil

        cleanup(self.paths)
        staging = self.attachments_dir / ".incoming"
        if staging.resolve() != staging:
            raise ValueError("Invalid attachment staging directory.")
        if staging.is_dir():
            for batch in staging.iterdir():
                if batch.is_dir() and not batch.is_symlink():
                    shutil.rmtree(batch)

    def _insert(self, db, key, record):
        data = dict(record)
        reason = data.pop("reasoning", None)
        items = data.pop("attachments", [])
        kind = data.pop("type")
        msg = data.pop("id", None)
        role = data.pop("role", None)
        at = data.pop("at", data.get("started_at"))
        content = data.pop("content", None)
        if reason:
            data["reasoning_local"] = True
        tool = (content or "") if kind == "tool" and msg is not None else None
        cur = db.execute(
            """INSERT INTO session_events(session_id,kind,message_id,role,at,content,payload,reasoning,tool_preview,tool_length)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                kind,
                msg,
                role,
                at,
                content,
                encode(data),
                encode(reason) if reason else None,
                tool[:TOOL_PREVIEW_SIZE] if tool is not None else None,
                len(tool) if tool is not None else None,
            ),
        )
        if kind in {"message", "assistant"} and content is not None:
            index_message(db, cur.lastrowid, content)
        if kind == "undo":
            db.execute(
                """UPDATE session_events SET active=0 WHERE session_id=? AND kind IN
                ('message','assistant','tool','summary','turn_failed') AND seq>=(
                    SELECT seq FROM session_events WHERE session_id=? AND kind='message' AND role='user' AND active=1 AND message_id=?)
                AND seq < ?""",
                (key, key, data.get("message_id"), cur.lastrowid),
            )
        for item in items:
            db.execute(
                "INSERT INTO attachments(session_id,message_id,id,sha256,name,media_type,size) VALUES(?,?,?,?,?,?,?)",
                (
                    key,
                    msg,
                    item["id"],
                    item["sha256"],
                    item["name"],
                    item["media_type"],
                    item["size"],
                ),
            )
        db.execute(
            """UPDATE sessions SET revision=revision+1,history_revision=history_revision+?,
            updated_at=?,last_message_at=CASE WHEN ? IN ('message','meta') THEN ? ELSE last_message_at END,
            tokens=tokens+?,messages=messages+?,undistilled=undistilled+?,summaries=summaries+? WHERE id=?""",
            (
                kind not in {"closed", "distilled"},
                at,
                kind,
                at,
                estimate_tokens(content or "")
                if kind in {"message", "assistant", "summary"}
                else 0,
                kind == "message",
                kind == "message",
                kind == "summary",
                key,
            ),
        )
        if kind in {"distilled", "summary"}:
            if kind == "distilled":
                boundary = (
                    "seq <= (SELECT seq FROM session_events WHERE session_id=? AND message_id=?)"
                )
                args = (key, key, data.get("up_to"))
            else:
                boundary = "message_id IN (SELECT value FROM json_each(?))"
                args = (key, encode(data["distilled_ids"]))
            consumed = db.execute(
                f"UPDATE session_events SET distilled=1 WHERE session_id=? AND {PENDING_USER} AND {boundary}",
                args,
            ).rowcount
            if consumed:
                db.execute(
                    "UPDATE sessions SET undistilled=undistilled-? WHERE id=?", (consumed, key)
                )
        if kind == "title" and data.get("title") and data.get("source") != "llm":
            db.execute("UPDATE sessions SET title=? WHERE id=?", (data["title"], key))
        elif kind == "title":
            pair = self._first_title_exchange(db, key)
            title = self._saved_title(db, key, pair[1]["id"] if pair else None)
            db.execute("UPDATE sessions SET title=? WHERE id=?", (title, key))
        elif kind == "undo":
            projected = self.load(key, include_reasoning=False)
            db.execute(
                "UPDATE sessions SET title=?,last_message_at=?,messages=?,undistilled=?,tokens=? WHERE id=?",
                (
                    projected.saved_title(),
                    projected.last_message_at,
                    len(projected.messages),
                    len(projected.undistilled_messages()),
                    projected.estimated_tokens(),
                    key,
                ),
            )

    def append_generated_title(
        self, session_id: str, record: dict, live_session: Session | None
    ) -> None:
        if live_session is not None:
            self.append(live_session, record)
        else:
            with transaction(self.paths) as db:
                self._append_events(db, session_id, (record,))

    def append(self, session, record):
        with transaction(self.paths) as db:
            result = self._append_events(db, session.id, (record,))
            on_commit(lambda: session.records.append(result))
            return result

    def append_delivery(
        self, session: Session, record: dict, submission_id: str, turn_id: int
    ) -> None:
        """在同一事务保存用户消息并确认送达；回复不在这个事务内。"""
        with transaction(self.paths):
            self.append(session, record)
            self.receipts.complete(submission_id, session.id, turn_id)

    def append_event_transactionally(self, session, event, *following):
        with transaction(self.paths) as db:
            return self._append_events(db, session.id, (event, *following))

    def _append_events(self, db, session_id, events):
        self._validate_session_id(session_id)
        if not db.execute(
            "SELECT 1 FROM sessions WHERE id=? AND archived=0", (session_id,)
        ).fetchone():
            raise KeyError(session_id)
        for raw in events:
            record = _redact_record(raw)
            if record.get("reasoning") and record["reasoning"] != raw.get("reasoning"):
                record["reasoning"].pop("source", None)
            _stamp_event(record)
            self._insert(db, session_id, record)
        return record

    def close_browsed(self, session_id: str) -> str | None:
        """浏览者离开时保留关闭事件和沉淀目标，无需将全部记录装入工作会话。"""
        with transaction(self.paths) as db:
            self._append_events(db, session_id, ({"type": "closed"},))
            row = db.execute(
                """SELECT message_id FROM session_events WHERE session_id=?
                AND active=1 AND kind='message' AND role='user' ORDER BY seq DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
            return row[0] if row else None

    def advance_distilled(self, session: Session | str, message_id: str) -> None:
        """将下一个游标与从该消息提取的所有事实一同提交。"""
        session_id = session.id if isinstance(session, Session) else session
        with transaction(self.paths) as db:
            first = db.execute(
                f"SELECT message_id FROM session_events WHERE session_id=? AND {PENDING_USER} AND EXISTS(SELECT 1 FROM sessions WHERE id=session_id AND archived=0) ORDER BY seq LIMIT 1",
                (session_id,),
            ).fetchone()
            if first is None or first[0] != message_id:
                raise ValueError("Distillation cursor no longer points at this message.")
            record = {"type": "distilled", "up_to": message_id}
            _stamp_event(record)
            self._insert(db, session_id, record)

    def rename(self, session: Session, title: str) -> None:
        if not isinstance(title, str) or not (title := title.strip()) or len(title) > 80:
            raise ConversationError("invalid_title", "Enter a session title of 1 to 80 characters.")
        self.append(session, {"type": "title", "title": title})

    def set_archived(self, session_id, archived):
        with transaction(self.paths) as db:
            if not db.execute(
                "UPDATE sessions SET archived=? WHERE id=?", (archived, session_id)
            ).rowcount:
                raise KeyError(session_id)
            if archived:
                db.execute(
                    "DELETE FROM jobs WHERE kind IN ('title','distillation') AND target=?",
                    (session_id,),
                )

    def delete(self, session_id, *, archived=False, before_delete=None):
        with transaction(self.paths) as db:
            if not self.exists(session_id, archived=archived):
                return False
            if before_delete:
                before_delete()
            db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            db.execute(
                "DELETE FROM jobs WHERE kind IN ('title','distillation') AND target=?",
                (session_id,),
            )
            on_commit(lambda: cleanup(self.paths))
        return True
