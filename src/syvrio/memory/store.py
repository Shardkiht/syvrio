from __future__ import annotations

import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from types import MappingProxyType

from ..infra.storage import evidence_versions
from ..infra.storage.audit import append_audit
from ..infra.storage.database import encode, transaction, transaction_mode
from ..infra.storage.files import safe_component
from ..infra.storage.paths import SyvrioPaths, vault_paths
from ..infra.time import local_timestamp, serialize_timestamp, utc_now
from .models import Entry, MemoryContext, MemoryDraft, MemoryUpdate, RecallEntry, TopicInfo
from .undo_store import track

SLUG_RE = re.compile(r"[^\w\u4e00-\u9fff-]+")
STATUS_ACTIVE = "active"
STATUS_DEPRECATED = "deprecated"


def slugify(value: str) -> str:
    slug = SLUG_RE.sub("-", value.strip().lower()).replace("_", "-")
    return re.sub(r"-{2,}", "-", slug).strip("-") or "uncategorized"


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text).strip().lower()


@dataclass(frozen=True, slots=True)
class RecallRecord:
    id: str
    topic: str
    text: str
    timestamp: str
    index_text: str
    context_json: str | None
    revision: int = 0

    @classmethod
    def from_row(cls, row):
        key, topic, text, stamp, raw, revision = row
        context = MemoryContext.model_validate_json(raw) if raw else None
        return cls(
            key, topic, text, stamp, context.index_text(text) if context else text, raw, revision
        )

    @property
    def embedding_text(self) -> str:
        return self.text

    @property
    def context(self) -> MemoryContext | None:
        # 每个调用方获得自己的上下文对象，无法改动共享快照。
        return MemoryContext.model_validate_json(self.context_json) if self.context_json else None

    def as_recall(self, context: MemoryContext | None) -> RecallEntry:
        return RecallEntry(
            id=self.id, topic=self.topic, text=self.text, context=context, revision=self.revision
        )


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    revision: int | None
    records: tuple[RecallRecord, ...]
    by_id: Mapping[str, RecallRecord]


class MemoryStore:
    def __init__(self, vault: Path | SyvrioPaths):
        self.paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
        self._snapshot_lock = Lock()
        self._snapshot: MemorySnapshot | None = None

    def ensure(self) -> None:
        with transaction(self.paths):
            pass

    def revisions(self, *domains: str) -> tuple[int, ...]:
        with transaction(self.paths, write=False) as db:
            return tuple(
                db.execute("SELECT version FROM revisions WHERE domain=?", (domain,)).fetchone()[0]
                for domain in domains
            )

    def read_topic(self, topic: str) -> tuple[str, list[Entry]]:
        with transaction(self.paths, write=False) as db:
            logical = self.topic_name(topic)
            if not db.execute(
                "SELECT 1 FROM topics WHERE archived=0 AND name=?", (logical,)
            ).fetchone():
                raise KeyError(topic)
            return logical, self.entries(logical)

    def first_source_reference(self, session_id: str) -> str | None:
        with transaction(self.paths, write=False) as db:
            row = db.execute(
                "SELECT memory_id FROM memory_versions WHERE session_id=? ORDER BY memory_id LIMIT 1",
                (session_id,),
            ).fetchone()
            return row[0] if row else None

    def counts(self) -> dict[str, int]:
        with transaction(self.paths, write=False) as db:
            return dict(
                db.execute("""
                SELECT (SELECT count(*) FROM topics WHERE archived=0) AS topics,
                       (SELECT count(*) FROM memories m JOIN topics t ON t.name=m.topic
                        WHERE m.status='active' AND t.archived=0) AS entries,
                       (SELECT count(*) FROM candidates WHERE state='pending') AS pending
            """).fetchone()
            )

    def topic_name(self, topic: str) -> str:
        parts = topic.strip().split("/")
        if not all(safe_component(part) for part in parts):
            raise ValueError("Invalid memory topic.")
        if len(parts) == 1:
            leaf = slugify(parts[0])
            with transaction(self.paths, write=False) as db:
                names = [
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM topics WHERE archived=0 AND substr(name,-length(?))=?",
                        ("/" + leaf, "/" + leaf),
                    )
                ]
            if len(names) > 1:
                raise ValueError("Ambiguous topic; supply its category.")
            return names[0] if names else "general/" + leaf
        return "/".join(map(slugify, parts))

    def revision(self) -> int:
        with transaction(self.paths, write=False) as db:
            return db.execute("SELECT version FROM revisions WHERE domain='memories'").fetchone()[0]

    def snapshot(self) -> MemorySnapshot:
        with transaction(self.paths, write=False) as db, self._snapshot_lock:
            version = db.execute(
                "SELECT version FROM revisions WHERE domain='memories'"
            ).fetchone()[0]
            cached = self._snapshot
            if cached is not None and cached.revision == version:
                return cached
            # 写事务的版本在回滚后可被另一份数据复用，不能发布到任一检索索引。
            revision = None if transaction_mode() == "write" else version
            records = tuple(
                RecallRecord.from_row(row)
                for row in db.execute("""
                    SELECT m.id,m.topic,m.text,m.recorded_at,m.context,m.revision FROM memories m
                    JOIN topics t ON t.name=m.topic WHERE t.archived=0 AND m.status='active'
                    ORDER BY m.topic,m.rowid
                """)
            )
            snapshot = MemorySnapshot(
                revision, records, MappingProxyType({r.id: r for r in records})
            )
            if revision is not None and (cached is None or cached.revision < revision):
                self._snapshot = snapshot
            return snapshot

    @staticmethod
    def new_entry_id(stamp: datetime | None = None) -> str:
        stamp = stamp or utc_now()
        return f"mem-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"

    @staticmethod
    def _change(db, entry_id, action, change, revision, **origin):
        details = {"memory_id": entry_id, "revision": revision, **origin}
        seq = append_audit(db, action, details, memory_change=change)
        track(db, "audit", seq, before=None)

    def append(
        self,
        topic: str,
        text: str,
        *,
        when: datetime | None = None,
        title: str | None = None,
        extra: dict[str, str] | None = None,
        entry_id: str | None = None,
        context: MemoryContext | None = None,
        supersedes: str | None = None,
    ) -> Entry:
        with transaction(self.paths) as db:
            evidence_versions.group_changes(db)
            stamp = when or utc_now()
            key = entry_id or self.new_entry_id(stamp)
            topic = self.topic_name(topic)
            at = serialize_timestamp(stamp)
            while self.find(key):
                if entry_id is not None:
                    raise ValueError("Duplicate memory id.")
                key = self.new_entry_id(stamp)
            if supersedes and not self.find(supersedes):
                raise ValueError("Unknown superseded memory id.")
            fields = dict(extra or {})
            sid, mid = fields.pop("session_id", None), fields.pop("message_id", None)
            stored_topic = db.execute(
                "SELECT archived FROM topics WHERE name=?", (topic,)
            ).fetchone()
            if stored_topic and stored_topic[0]:
                raise ValueError("Topic is archived.")
            new_topic = stored_topic is None
            db.execute(
                """INSERT INTO topics(name,title,updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at""",
                (topic, title or topic.split("/")[-1], at),
            )
            if new_topic:
                track(db, "topics", topic, before=None)
            revision = self.revision() + 1
            db.execute(
                """INSERT INTO memories(id,topic,status,text,recorded_at,context,supersedes,session_id,message_id,extensions,revision)
                VALUES(?,?,'active',?,?,?,?,?,?,?,?)""",
                (
                    key,
                    topic,
                    text,
                    at,
                    context.model_dump_json(exclude_none=True)
                    if context and context.model_dump(exclude_none=True)
                    else None,
                    supersedes,
                    sid,
                    mid,
                    encode(fields),
                    revision,
                ),
            )
            track(db, "memories", key, before=None)
            origin = {"session_id": sid, "message_id": mid} if sid is not None else {}
            self._change(db, key, "add", "created", revision, **origin)
            return self.find(key)

    def _entry(self, row):
        metadata = json.loads(row["extensions"])
        if row["session_id"]:
            metadata.update(session_id=row["session_id"], message_id=row["message_id"])
        return Entry(
            id=row["id"],
            topic=row["topic"],
            text=row["text"],
            timestamp=row["recorded_at"],
            status=row["status"],
            context=MemoryContext.model_validate_json(row["context"]) if row["context"] else None,
            metadata=metadata,
            supersedes=row["supersedes"],
            revision=row["revision"],
            topic_title=row["title"],
        )

    def entries(self, topic: str | None = None, include_inactive: bool = False) -> list[Entry]:
        with transaction(self.paths, write=False) as db:
            sql = "SELECT m.*,t.title FROM memories m JOIN topics t ON t.name=m.topic WHERE t.archived=0"
            args = []
            if topic:
                sql += " AND m.topic=?"
                args.append(self.topic_name(topic))
            if not include_inactive:
                sql += " AND m.status='active'"
            return [self._entry(r) for r in db.execute(sql + " ORDER BY m.topic,m.rowid", args)]

    def entries_from_message(self, session_id: str, message_id: str) -> list[Entry]:
        with transaction(self.paths, write=False) as db:
            return [
                self._entry(row)
                for row in db.execute(
                    """SELECT m.*, t.title FROM memories m JOIN topics t ON t.name=m.topic
                       WHERE m.session_id=? AND m.message_id=? AND m.status='active'
                       AND t.archived=0 ORDER BY m.topic,m.rowid""",
                    (session_id, message_id),
                )
            ]

    def source_texts(self, ids: set[str]) -> dict[str, str]:
        if not ids:
            return {}
        with transaction(self.paths, write=False) as db:
            return dict(
                db.execute(
                    """SELECT m.id, m.text FROM json_each(?) ids
                   JOIN memories m ON m.id=ids.value JOIN topics t ON t.name=m.topic
                   WHERE t.archived=0""",
                    (encode(sorted(ids)),),
                )
            )

    def find(self, entry_id: str) -> Entry | None:
        with transaction(self.paths, write=False) as db:
            row = db.execute(
                "SELECT m.*,t.title FROM memories m JOIN topics t ON t.name=m.topic WHERE t.archived=0 AND m.id=?",
                (entry_id,),
            ).fetchone()
            return self._entry(row) if row else None

    def save_revision(
        self,
        entry_id: str,
        change: MemoryDraft | MemoryUpdate,
        *,
        source: dict[str, str] | None = None,
    ) -> Entry:
        """在同一事务保存新版本、当前事实、审计和撤销依据。"""
        with transaction(self.paths) as db:
            row = db.execute(
                """SELECT m.*, t.title FROM memories m JOIN topics t ON t.name=m.topic
                   WHERE t.archived=0 AND m.id=?""",
                (entry_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Memory not found: {entry_id}")
            old = self._entry(row)
            if isinstance(change, MemoryUpdate):
                if change.id != entry_id or source is not None:
                    raise ValueError(
                        "A supplemental update must match its target and preserve its source."
                    )
                context, additions = change.merge_context(old.context)
                status = change.status or old.status
                detail = {"context": additions} if additions else {}
                if status != old.status:
                    detail["status"] = status
                if not detail:
                    return old
                text, topic = old.text, old.topic
                sid, mid = row["session_id"], row["message_id"]
                action = "update"
            else:
                text, topic, context = change.text, self.topic_name(change.topic), change.context
                current = old.context.model_dump(exclude_none=True) if old.context else {}
                incoming = context.model_dump(exclude_none=True) if context else {}
                if (text, topic, incoming) == (old.text, old.topic, current):
                    return old
                status, action, detail = old.status, "correct", "corrected"
                origin = source or {}
                sid, mid = origin.get("session_id"), origin.get("message_id")

            evidence_versions.group_changes(db)
            now = serialize_timestamp(utc_now())
            if topic != old.topic:
                target = db.execute("SELECT archived FROM topics WHERE name=?", (topic,)).fetchone()
                if target and target["archived"]:
                    raise ValueError("Topic is archived.")
                if target is None:
                    db.execute(
                        "INSERT INTO topics(name,title,updated_at) VALUES(?,?,?)",
                        (topic, topic.split("/")[-1], now),
                    )
                    track(db, "topics", topic, before=None)
            revision = self.revision() + 1
            stored_context = context.model_dump(exclude_none=True) if context else {}
            before = dict(row)
            del before["title"]
            db.execute(
                """UPDATE memories SET text=?,topic=?,context=?,status=?,session_id=?,message_id=?,
                   revision=? WHERE id=?""",
                (
                    text,
                    topic,
                    encode(stored_context) if stored_context else None,
                    status,
                    sid,
                    mid,
                    revision,
                    entry_id,
                ),
            )
            db.execute(
                "UPDATE topics SET updated_at=? WHERE name IN (?,?)", (now, old.topic, topic)
            )
            track(db, "memories", entry_id, before=before)
            self._change(db, entry_id, action, detail, revision, **(source or {}))
            return self.find(entry_id)

    def evidence_commit(self) -> int:
        with transaction(self.paths, write=False) as db:
            return evidence_versions.current(db)

    def facts_at(self, commit: int) -> list[dict]:
        with transaction(self.paths, write=False) as db:
            rows = evidence_versions.visible_facts(db, commit)
            result = []
            for row in rows:
                fact = dict(row)
                raw = fact.pop("context")
                if raw:
                    fact["context"] = json.loads(raw)
                result.append(fact)
            return result

    def version_texts(self, versions: set[tuple[str, int]]) -> dict[str, str]:
        if not versions:
            return {}
        with transaction(self.paths, write=False) as db:
            return dict(
                db.execute(
                    """SELECT v.memory_id || '@' || v.commit_id,v.text
                FROM json_each(?) wanted JOIN memory_versions v
                ON v.memory_id=json_extract(wanted.value,'$[0]')
                AND v.commit_id=json_extract(wanted.value,'$[1]')""",
                    (encode(sorted(versions)),),
                )
            )

    def evidence_sources(self, ids: list[str], commit: int) -> dict[str, int]:
        with transaction(self.paths, write=False) as db:
            return dict(
                db.execute(
                    """SELECT memory_id,max(commit_id) FROM memory_versions
                WHERE memory_id IN (SELECT value FROM json_each(?)) AND commit_id<=?
                GROUP BY memory_id""",
                    (encode(ids), commit),
                )
            )

    def version_origin(self, entry_id: str, commit: int):
        with transaction(self.paths, write=False) as db:
            row = db.execute(
                """SELECT session_id,message_id FROM memory_versions
                WHERE memory_id=? AND commit_id=?""",
                (entry_id, commit),
            ).fetchone()
            if row is None:
                raise KeyError("Memory version not found.")
            return tuple(row)

    def version_history(self, entry_id: str) -> list[dict]:
        with transaction(self.paths, write=False) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM memory_versions WHERE memory_id=? ORDER BY commit_id",
                    (entry_id,),
                )
            ]

    def set_status(self, entry_id, status):
        with transaction(self.paths):
            if not self.find(entry_id):
                return False
            self.save_revision(entry_id, MemoryUpdate(id=entry_id, status=status))
            return True

    def fill_context(self, entry_id, context):
        return self.save_revision(entry_id, MemoryUpdate(id=entry_id, context=context))

    def topics(self) -> list[TopicInfo]:
        with transaction(self.paths, write=False) as db:
            return [
                TopicInfo(topic=r[0], title=r[1], updated=local_timestamp(r[2])[:10], entries=r[3])
                for r in db.execute("""SELECT t.name,t.title,t.updated_at,count(m.id) FROM topics t
                LEFT JOIN memories m ON m.topic=t.name AND m.status='active' WHERE t.archived=0 GROUP BY t.name ORDER BY t.name""")
            ]

    def topic_directory(self):
        return [r.model_dump(exclude={"entries"}) for r in self.topics()]

    def has_text(self, text):
        return any(normalize_text(row.text) == normalize_text(text) for row in self.entries())

    def recent_context(self, limit=60):
        recent = sorted(self.entries(), key=lambda e: e.timestamp, reverse=True)[:limit]
        return "\n".join(f"[{e.topic}] {e.timestamp} {e.text}" for e in reversed(recent))
