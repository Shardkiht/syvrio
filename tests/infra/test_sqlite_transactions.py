from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from syvrio.infra.session.store import SessionStore
from syvrio.infra.storage.backup import assemble
from syvrio.infra.storage.database import (
    APPLICATION_ID,
    DatabaseError,
    database_path,
    transaction,
)
from syvrio.infra.storage.paths import SyvrioPaths
from syvrio.memory.background.jobs import JobKey, read_job
from syvrio.memory.models import MemoryDraft
from syvrio.memory.store import MemoryStore
from syvrio.memory.turn_undo import TurnJournal, TurnUndoError


def test_schema_empty_reopen_and_refuse_foreign_database(tmp_path):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    for _ in range(2):
        with transaction(paths) as db:
            assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert db.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    with sqlite3.connect(database_path(paths)) as db:
        db.execute("PRAGMA application_id=7")
    with pytest.raises(DatabaseError), transaction(paths):
        pytest.fail("accepted a foreign database")


def test_database_rejects_redirected_file(tmp_path, monkeypatch):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    path = database_path(paths)
    outside = tmp_path / "outside" / path.name
    resolve = Path.resolve

    def redirect(candidate, *args, **kwargs):
        return outside if candidate == path else resolve(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirect)
    with pytest.raises(DatabaseError, match="inside the configured Vault"), transaction(paths):
        pytest.fail("accepted redirected database")
    assert not outside.exists()


def test_incomplete_vault_is_refused_without_changing_its_tables(tmp_path):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    paths.vault_dir.mkdir()
    with sqlite3.connect(database_path(paths)) as db:
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO sessions VALUES('kept')")
    with pytest.raises(DatabaseError), transaction(paths):
        pytest.fail("accepted an incomplete database")
    with sqlite3.connect(database_path(paths)) as db:
        assert db.execute("SELECT * FROM sessions").fetchall() == [("kept",)]
        assert db.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall() == [
            ("sessions",)
        ]


def test_approve_failure_is_atomic_and_replay_is_idempotent(service):
    item = service.create_pending(MemoryDraft(topic="demo", text="draft"), "Confirm?", [])
    with transaction(service.paths) as db:
        db.execute(
            """CREATE TRIGGER fail_job BEFORE INSERT ON jobs BEGIN SELECT RAISE(ABORT,'injected failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        service.approve(item.id, "edited")
    assert service.pending() == [item] and service.current_facts() == []
    with transaction(service.paths) as db:
        assert db.execute("SELECT count(*) FROM memory_versions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit WHERE action='add'").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_job")
    record = service.approve(item.id, "edited")
    assert service.approve(item.id, "edited") == record and not service.pending()
    assert read_job(JobKey(service.paths, "context", record.id))["version"] == str(record.revision)


def test_nested_rollback_and_turn_references(service):
    store = SessionStore(service.paths)
    session = store.create()
    msg = store.append(session, dict(type="message", role="user", content="synthetic"))
    journal = TurnJournal(service.paths, session.id, msg["id"])
    with transaction(service.paths), journal.capture():
        with pytest.raises(RuntimeError), transaction(service.paths):
            service.note("should roll back")
            raise RuntimeError
        assert not journal.has_effects()
        entry = service.note("kept")
    later = service.propose_inference("possible", [entry.id], "demo")
    with pytest.raises(TurnUndoError):
        journal.restore()
    assert service.store.find(entry.id) == entry and service.inferences.all() == [later]


def test_process_exit_and_restart(service):
    row = service.note("keep")
    program = """import sys,os
from pathlib import Path
from syvrio.memory.store import MemoryStore
from syvrio.infra.storage.database import transaction
s=MemoryStore(Path(sys.argv[1]))
with transaction(s.paths):
    s.append("demo","uncommitted")
    os._exit(37)
"""
    p = subprocess.run([sys.executable, "-c", program, str(service.paths.vault_dir)], check=False)
    assert p.returncode == 37 and MemoryStore(service.paths).entries() == [row]


def test_concurrent_writers_and_snapshot(service):
    service.store.ensure()
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i: service.note(f"synthetic {i}"), range(36)))
    snap = service.store.snapshot()
    assert len({row.id for row in snap.records}) == 36
    with transaction(service.paths, write=False) as db:
        assert db.execute("SELECT count(*) FROM audit").fetchone()[0] == 36
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


async def test_async_child_does_not_inherit_connection(service):
    service.store.ensure()

    async def get_connection():
        with transaction(service.paths, write=False) as child:
            return child

    with transaction(service.paths, write=False) as parent:
        assert await asyncio.create_task(get_connection()) is not parent


def test_backup_restores_profile_jobs_and_session(service, tmp_path):
    session = SessionStore(service.paths).create()
    source = service.note("preserved")
    service.profile.publish("profile", {"fact_count": 1})
    from syvrio.memory.background.jobs import save_job

    save_job(JobKey(service.paths, "title", session.id), {"id": session.id})
    backup = assemble(service.paths.vault_dir, tmp_path / "backup")
    restore = assemble(backup, tmp_path / "restored", restore=True)
    paths = SyvrioPaths(restore, tmp_path / "fresh-runtime")
    assert MemoryStore(paths).find(source.id) == source
    from syvrio.memory.reflection import ProfileReader

    assert ProfileReader(paths).read_text() == "profile"
    assert SessionStore(paths).exists(session.id)
    assert read_job(JobKey(paths, "title", session.id)) == {"id": session.id}
    assert not (restore / ".backend.lock").exists()


def test_backup_copies_archived_originals_and_rejects_tampering(service, tmp_path):
    from dataclasses import asdict
    from io import BytesIO

    from syvrio.infra.session.attachment_objects import original_path
    from syvrio.infra.session.attachments import AttachmentBatch

    sessions = SessionStore(service.paths)
    chat = sessions.create()
    batch = AttachmentBatch.stage(
        service.paths.attachments_dir, [("sample.txt", BytesIO(b"private original"))]
    )
    with batch.sending(service.paths):
        sessions.append(
            chat,
            {
                "type": "message",
                "role": "user",
                "content": "document",
                "attachments": [asdict(batch.files[0])],
            },
        )
    sessions.set_archived(chat.id, True)
    backup = assemble(service.paths.vault_dir, tmp_path / "backup")
    restored = assemble(backup, tmp_path / "restored", restore=True)
    query = SessionStore(SyvrioPaths(restored, tmp_path / "runtime-restored"))
    assert query.verify_attachments() == (1, [])
    assert query.load(chat.id, archived=True).user_texts() == ["document"]
    original_path(backup / "attachments", batch.files[0].sha256).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="Attachment verification"):
        assemble(backup, tmp_path / "invalid", restore=True)
    assert not (tmp_path / "invalid").exists()


def test_export_retries_complete_snapshot_without_private_state(service, tmp_path, monkeypatch):
    import subprocess

    from syvrio.infra.vault.export import render
    from syvrio.infra.vault.files import initialize_vault
    from syvrio.infra.vault.sync import GitSyncService

    git = GitSyncService(service.config, service.paths)
    initialize_vault(git, service.initialize_files)
    remote = tmp_path / "bare"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)

    def run(*args):
        return subprocess.check_output(["git", "-C", str(git.repo.path), *args], text=True).strip()

    run("remote", "add", "origin", str(remote))
    run("push", "-u", "origin", "HEAD")
    initial = run("rev-parse", "HEAD")
    archived_marker = "private archived exchange"
    private_reason = "private reasoning marker"
    store = SessionStore(service.paths)
    archived = store.create()
    store.append(archived, {"type": "message", "role": "user", "content": archived_marker})
    store.set_archived(archived.id, True)
    visible = store.create()
    store.append(
        visible,
        {"type": "assistant", "content": "public response", "reasoning": {"text": private_reason}},
    )
    old = service.note("superseded spelling")
    corrected = service.correct(old.id, MemoryDraft(text="correct spelling", topic=old.topic))
    assert corrected.id == old.id and service.store.find(old.id).status == "active"
    rendered = render(service.paths)
    assert rendered == render(service.paths)
    public = "\n".join(rendered.values())
    assert old.text in public and corrected.text in public
    assert archived_marker not in public and private_reason not in public
    write = Path.write_bytes

    def fail(path, body):
        if path.name == "inferences.jsonl":
            raise OSError("injected export failure")
        return write(path, body)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_bytes", fail)
        assert git.sync("manual").status == "failed"
    assert run("rev-parse", "HEAD") == initial and run("status", "--porcelain") == ""
    assert service.store.find(corrected.id).text == corrected.text
    assert git.sync("manual").pushed
    assert (
        run("rev-parse", "HEAD")
        == subprocess.check_output(
            ["git", "--git-dir", str(remote), "rev-parse", "HEAD"], text=True
        ).strip()
    )
    assert not any(
        name.endswith((".sqlite3", "-wal", "-shm")) for name in run("ls-files").splitlines()
    )


def test_preparation_follows_nested_savepoints(service):
    from syvrio.infra.storage.database import before_commit, on_commit
    from syvrio.infra.storage.evidence_versions import current, group_changes

    events = []
    with transaction(service.paths) as db:
        initial = current(db)
        before_commit(lambda _: events.append("parent"))
        with pytest.raises(RuntimeError), transaction(service.paths):
            group_changes(db)
            before_commit(lambda _: events.append("discarded"))
            raise RuntimeError("rollback nested savepoint")
        assert (
            db.execute("SELECT reserved FROM revisions WHERE domain='evidence'").fetchone()[0]
            is None
        )
        with transaction(service.paths):
            group_changes(db)
            before_commit(lambda _: events.append("child"))
        service.note("one")
        service.note("two")
        assert current(db) == initial + 1
        on_commit(lambda: events.append("committed"))
        assert events == []

    with transaction(service.paths, write=False) as db:
        assert tuple(
            db.execute("SELECT version,reserved FROM revisions WHERE domain='evidence'").fetchone()
        ) == (initial + 1, None)
    assert events == ["parent", "child", "committed"]


def test_failed_preparation_rolls_back_evidence_and_publication(service):
    from syvrio.infra.storage.database import before_commit, on_commit
    from syvrio.infra.storage.evidence_versions import current

    def fail(_):
        raise ValueError("reject before commit")

    published = []
    with pytest.raises(ValueError, match="reject before commit"), transaction(service.paths) as db:
        first_version = current(db)
        entry = service.note("rejected")
        on_commit(lambda: published.append(entry.id))
        before_commit(fail)

    with transaction(service.paths, write=False) as db:
        assert current(db) == first_version
        assert service.store.find(entry.id) is None
        assert (
            db.execute(
                "SELECT count(*) FROM memory_versions WHERE memory_id=?", (entry.id,)
            ).fetchone()[0]
            == 0
        )
    assert published == []
