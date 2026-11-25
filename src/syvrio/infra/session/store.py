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
from ..time import serialize_timestamp, timestamp_calendar_date, utc_now
from .attachment_objects import cleanup
from .errors import ConversationError
from .model import Session, new_message_id, now_iso, validate_record
from .query import SessionQuery


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
        # Called under the exclusive backend lease before accepting any uploads.
        import shutil

        with transaction(self.paths) as db:
            db.execute("DELETE FROM attachment_reservations")
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
        cur = db.execute(
            "INSERT INTO session_events(session_id,kind,message_id,role,at,content,payload) VALUES(?,?,?,?,?,?,?)",
            (key, kind, msg, role, at, content, encode(data)),
        )
        if reason:
            db.execute("INSERT INTO session_reasoning VALUES(?,?)", (cur.lastrowid, encode(reason)))
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
                "INSERT INTO attachments VALUES(?,?,?,?,?,?)",
                (key, msg, item["id"], item["sha256"], item["name"], item["media_type"]),
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
        if kind in {"distilled", "summary", "title", "undo"}:
            projected = self.load(key, include_reasoning=False)
            db.execute(
                "UPDATE sessions SET title=?,last_message_at=?,messages=?,undistilled=?  ,tokens=? WHERE id=?",
                (
                    projected.saved_title(),
                    projected.last_message_at,
                    len(projected.messages),
                    len(projected.undistilled_messages()),
                    projected.estimated_tokens(),
                    key,
                ),
            )

    def append(self, session, record):
        with transaction(self.paths):
            result = self.append_event_transactionally(session, record)
            on_commit(lambda: session.records.append(result))
            return result

    def append_event_transactionally(self, session, event, *following):
        with transaction(self.paths) as db:
            if not self.exists(session.id):
                raise KeyError(session.id)
            for raw in (event, *following):
                record = _redact_record(raw)
                if record.get("reasoning") and record["reasoning"] != raw.get("reasoning"):
                    record["reasoning"].pop("source", None)
                _stamp_event(record)
                self._insert(db, session.id, record)
            return record

    def advance_distilled(self, session: Session, message_id: str) -> None:
        """将下一个游标与从该消息提取的所有事实一同提交。"""
        with transaction(self.paths):
            current = self.load(session.id, include_reasoning=False)
            pending = current.undistilled_messages()
            if not pending or pending[0]["id"] != message_id:
                raise ValueError("Distillation cursor no longer points at this message.")
            self.append_event_transactionally(current, {"type": "distilled", "up_to": message_id})

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
