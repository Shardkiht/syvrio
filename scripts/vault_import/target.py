"""把冻结的历史快照写成当前应用可直接读取的记录。"""

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

from syvrio.infra.session.attachment_objects import original_path
from syvrio.infra.session.attachments import Attachment
from syvrio.infra.session.store import SessionStore
from syvrio.infra.storage.audit import append_audit
from syvrio.infra.storage.database import encode, transaction
from syvrio.infra.storage.files import sync_directory
from syvrio.infra.time import utc_timestamp

from .snapshot import fingerprint


def save_blob(snapshot, digest, size, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        with path.open("rb") as f:
            if (
                path.stat().st_size != size
                or hashlib.file_digest(f, "sha256").hexdigest() != digest
            ):
                raise ValueError(f"目标已有不同内容: {path.name}")
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        staging = Path(out.name)
        try:
            with snapshot.blob(digest, size) as blob:
                shutil.copyfileobj(blob, out)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            out.close()
            staging.unlink(missing_ok=True)
            raise
    try:
        with staging.open("rb") as f:
            if hashlib.file_digest(f, "sha256").hexdigest() != digest:
                raise ValueError("暂存原件校验失败")
        os.replace(staging, path)
        sync_directory(path.parent)
    finally:
        staging.unlink(missing_ok=True)


def transfer(records, paths):
    changes = Counter()
    store = SessionStore(paths)
    with ExitStack() as cleanup:
        counts = _transfer(records, paths, changes, store, cleanup)
        cleanup.pop_all()
        return counts


def _transfer(records, paths, changes, store, cleanup):
    def publish_blob(digest, size, path):
        if not path.exists():
            cleanup.callback(path.unlink, missing_ok=True)
        save_blob(records.snapshot, digest, size, path)

    with transaction(paths) as db:
        known = {}
        for (details,) in db.execute(
            "SELECT details FROM audit WHERE action='legacy_vault_import'"
        ):
            for kind, key, digest in json.loads(details)["entries"]:
                known[kind, key] = digest
        added = []

        def fresh(kind, key, value):
            digest = fingerprint(value)
            old = known.get((kind, key))
            if old:
                if old != digest:
                    raise ValueError(f"来源与上次导入不同，未覆盖: {kind}/{key}")
                return False
            added.append((kind, key, digest))
            known[kind, key] = digest
            changes[kind] += 1
            return True

        for sid, (events, archived) in records.sessions.items():
            if not fresh("sessions", sid, (archived, events)):
                continue
            store._validate_session_id(sid)
            at = events[0]["started_at"]
            db.execute("INSERT INTO sessions(id,started_at) VALUES(?,?)", (sid, at))
            for event in events:
                for raw in event.get("attachments", []):
                    obj = Attachment(**raw)
                    dest = original_path(paths.attachments_dir, obj.sha256)
                    obj.path(paths.attachments_dir, paths.attachments_dir)
                    publish_blob(obj.sha256, obj.size, dest)
                store._insert(db, sid, event)
            if archived:
                db.execute("UPDATE sessions SET archived=1 WHERE id=?", (sid,))
        for name, (title, archived) in records.topics.items():
            if not fresh("topics", name, (title, archived)):
                continue
            at = max((m[3] for m in records.memories.values() if m[0] == name), default="")
            old = db.execute("SELECT title,archived FROM topics WHERE name=?", (name,)).fetchone()
            if old and bool(old[1]) != archived:
                raise ValueError(f"已有主题归档状态冲突: {name}")
            db.execute(
                "INSERT INTO topics(name,title,archived,updated_at) VALUES(?,?,?,?) ON CONFLICT(name) DO NOTHING",
                (name, title, archived, at),
            )
        for key, row in records.memories.items():
            if not fresh("memories", key, row):
                continue
            name, state, text, at, ctx, sup, sid, mid, ext = row
            if (sid is None) != (mid is None):
                raise ValueError(f"来源不完整: {key}")
            if (
                sid
                and not db.execute(
                    "SELECT 1 FROM session_events WHERE session_id=? AND message_id=? AND kind='message' AND role='user'",
                    (sid, mid),
                ).fetchone()
            ):
                raise ValueError(f"无法还原正式记忆的原始消息: {key}")
            db.execute(
                """INSERT INTO memories(id,topic,status,text,recorded_at,context,supersedes,session_id,message_id,extensions,revision)
                VALUES(?,?,?,?,?,?,?,?,?,?,(SELECT version+1 FROM revisions WHERE domain='memories'))""",
                (
                    key,
                    name,
                    state,
                    text,
                    at,
                    encode(ctx) if ctx else None,
                    sup,
                    sid,
                    mid,
                    encode({k: v if isinstance(v, str) else encode(v) for k, v in ext.items()}),
                ),
            )
        for key, row in records.candidates.items():
            if not fresh("candidates", key, row):
                continue
            name, text, question, at, state, ctx, sid, mid, related = row
            db.execute(
                "INSERT INTO candidates(id,topic,text,question,created_at,state,context,session_id,message_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (key, name, text, question, at, state, encode(ctx) if ctx else None, sid, mid),
            )
            db.executemany(
                "INSERT INTO candidate_related VALUES(?,?,?)",
                [(key, i, x) for i, x in enumerate(related)],
            )
        for key, row in records.inferences.items():
            if not fresh("inferences", key, row):
                continue
            kind, text, state, created, updated, sources = row
            db.execute(
                "INSERT INTO inferences(id,kind,text,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (key, kind, text, state, created, updated),
            )
            db.executemany(
                "INSERT INTO inference_sources(inference_id,memory_id,position) VALUES(?,?,?)",
                [(key, x, i) for i, x in enumerate(sources)],
            )
        for (name, digest, num), row in records.logs.items():
            if not fresh(name, f"{digest}/{num}", row):
                continue
            if name == "memory-changes.jsonl":
                append_audit(
                    db,
                    "legacy_memory_change",
                    {"memory_id": row["id"]},
                    at=row.get("at"),
                    memory_change=row["change"],
                )
            elif name == "resolved.jsonl":
                db.execute(
                    "INSERT INTO audit(at,action,details) VALUES(?,?,?)",
                    (row["resolved_at"], "legacy_candidate_" + row["status"], encode(row)),
                )
            else:
                db.execute(
                    "INSERT INTO audit(at,action,details) VALUES(?,?,?)",
                    (
                        row["at"],
                        row["action"],
                        encode({k: v for k, v in row.items() if k not in ("at", "action")}),
                    ),
                )
        if records.profile and fresh("profile", "published", records.profile):
            body, meta = records.profile
            if not db.execute("SELECT 1 FROM profile").fetchone():
                db.execute("INSERT INTO profile VALUES(1,?,?)", (body, encode(meta)))
        if records.soul and fresh("soul", "SOUL.md", records.soul):
            soul = paths.vault_dir / "SOUL.md"
            if not soul.exists():
                size = records.snapshot.db.execute(
                    "SELECT length(content) FROM objects WHERE digest=?", (records.soul,)
                ).fetchone()[0]
                publish_blob(records.soul, size, soul)
        if added:
            db.execute(
                "INSERT INTO audit(at,action,details) VALUES(?,?,?)",
                (utc_timestamp(), "legacy_vault_import", encode({"entries": added})),
            )
        if (
            db.execute("PRAGMA foreign_key_check").fetchone()
            or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
        ):
            raise ValueError("导入未通过 SQLite 完整性或来源外键校验")
    return changes
