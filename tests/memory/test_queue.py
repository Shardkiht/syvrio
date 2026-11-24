from __future__ import annotations

import pytest

from syvrio.memory.models import PendingItem
from syvrio.memory.queue import PendingQueue

pytestmark = pytest.mark.memory


def make_item(item_id: str = "p-1") -> PendingItem:
    return PendingItem(
        id=item_id,
        created_at="2026-10-22T12:00:00+00:00",
        text="候选事实",
        topic="preferences",
        question="请问具体是哪一天？",
    )


def test_add_and_load(tmp_path):
    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    assert [item.id for item in queue.all()] == ["p-1"]
    assert queue.get("p-1").text == "候选事实"


@pytest.mark.parametrize("failed", [False, True])
def test_archived_deletion_commits_or_restores_pending(tmp_path, failed, monkeypatch):
    from pathlib import Path

    from syvrio.infra.session import SessionStore

    sessions = SessionStore(tmp_path)
    session = sessions.create()
    sessions.set_archived(session.id, True)
    archived = sessions.load(session.id, archived=True)
    queue = PendingQueue(tmp_path)
    queue.add(make_item().model_copy(update={"session_id": session.id, "message_id": "m-1"}))
    queue.add(make_item("other"))
    original = queue.pending_path.read_bytes()
    _, revision = queue.deletion_preview(session.id)

    def delete():
        return sessions.delete(
            session.id,
            archived=True,
            before_delete=lambda: queue.remove_for_session(session.id, revision),
        )

    if failed:
        unlink = Path.unlink

        def fail_unlink(path, *args, **kwargs):
            if path == archived.path:
                raise PermissionError("synthetic deletion failure")
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_unlink)
        with pytest.raises(PermissionError):
            delete()
        assert sessions.exists(session.id, archived=True)
        assert queue.pending_path.read_bytes() == original
    else:
        assert delete()
        assert not sessions.exists(session.id, archived=True)
    assert [item.id for item in PendingQueue(tmp_path).all()] == (
        ["p-1", "other"] if failed else ["other"]
    )


def test_resolve_removes_and_logs(tmp_path):
    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    queue.add(make_item("p-2"))

    resolved = queue.resolve("p-1", "approved")

    assert resolved.id == "p-1"
    assert [item.id for item in queue.all()] == ["p-2"]
    assert not queue.resolved_path.exists()


def test_rejection_preserves_original_unknown_extensions(tmp_path):
    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    raw = queue.pending_path.read_bytes() + b"  * `other.example`: `manual`\n"
    queue.pending_path.write_bytes(raw)
    queue.resolve("p-1", "rejected")
    assert queue.all() == []
    archive = queue.resolved_path.read_bytes()
    assert raw[raw.index(b"* **Core**") :] in archive
    assert queue.decode(archive) == [make_item()]


def test_resolve_missing_returns_none(tmp_path):
    assert PendingQueue(tmp_path).resolve("nope", "rejected") is None


def test_save_failure_leaves_pending_queue_intact(tmp_path, monkeypatch):
    from syvrio.infra.storage import files

    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    before = queue.pending_path.read_text(encoding="utf-8")

    def fail_replace(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(files.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        queue.add(make_item("p-2"))

    assert queue.pending_path.read_text(encoding="utf-8") == before
    assert [item.id for item in queue.all()] == ["p-1"]
    assert not [item for item in queue.pending_path.parent.iterdir() if item.name.endswith(".tmp")]


def test_duplicate_add_is_idempotent_and_conflict_is_rejected(tmp_path):
    queue = PendingQueue(tmp_path)
    original = make_item("p-dup")
    queue.add(original)
    queue.add(original)
    with pytest.raises(ValueError, match="Duplicate"):
        queue.add(original.model_copy(update={"text": "changed"}))
    assert queue.all() == [original]


def test_resolve_log_failure_keeps_candidate(tmp_path):
    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    queue.resolved_path.mkdir(parents=True)
    with pytest.raises(OSError):
        queue.resolve("p-1", "rejected")
    assert queue.get("p-1") is not None


@pytest.mark.parametrize("stage", ["archive", "pending"])
def test_reject_write_failure_restores_both_files_and_retry_archives_once(
    tmp_path, monkeypatch, stage
):
    from syvrio.infra.storage import transaction

    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    before = queue.pending_path.read_bytes()
    real_write = transaction.atomic_write

    def fail(path, payload):
        if path == (queue.resolved_path if stage == "archive" else queue.pending_path):
            raise OSError("synthetic disk failure")
        return real_write(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(transaction, "atomic_write", fail)
        with pytest.raises(OSError, match="synthetic"):
            queue.resolve("p-1", "rejected")
    assert queue.pending_path.read_bytes() == before
    assert not queue.resolved_path.exists()
    queue.resolve("p-1", "rejected")
    assert queue.all() == [] and queue.decode(queue.resolved_path.read_bytes()) == [make_item()]


def test_failure_of_recovery_retains_both_errors_and_next_open_finishes_rollback(
    tmp_path, monkeypatch
):
    from syvrio.infra.storage import transaction
    from syvrio.memory.store import MemoryStore

    store = MemoryStore(tmp_path)
    path = tmp_path / "ordinary.md"
    path.write_bytes(b"original")
    replace = transaction.atomic_write

    def fail_restore(target, payload):
        if target == path and payload == b"original":
            raise OSError("synthetic rollback failure")
        return replace(target, payload)

    with monkeypatch.context() as patch, pytest.raises(ExceptionGroup) as caught:
        with transaction.vault_transaction(store.paths) as tx:
            tx.write(path, b"changed")
            patch.setattr(transaction, "atomic_write", fail_restore)
            raise OSError("synthetic original failure")
    assert [str(e) for e in caught.value.exceptions] == [
        "synthetic original failure",
        "synthetic rollback failure",
    ]
    transaction.recover_vault_transaction(store.paths)
    assert path.read_bytes() == b"original"


def test_restart_restores_archived_transcript_candidates_and_attachment_after_interruption(
    tmp_path,
):
    import subprocess
    import sys

    from syvrio.infra.session import SessionStore
    from syvrio.infra.storage.transaction import recover_vault_transaction

    store = SessionStore(tmp_path)
    session = store.create()
    store.set_archived(session.id, True)
    queue = PendingQueue(store.paths)
    queue.add(make_item().model_copy(update={"session_id": session.id, "message_id": "m1"}))
    _, revision = queue.deletion_preview(session.id)
    folder = store.attachments_dir / session.id
    folder.mkdir(parents=True)
    (folder / "original.txt").write_bytes(b"saved")
    # 在删除文件已落盘、事务日志尚未清除时终止子进程，绕过所有 finally。
    code = """
import os, sys
from pathlib import Path
from syvrio.infra.session import SessionStore
from syvrio.infra.storage.transaction import VaultTransaction
from syvrio.memory.queue import PendingQueue
store = SessionStore(Path(sys.argv[1]))
queue = PendingQueue(store.paths)
sid, revision = sys.argv[2:]
VaultTransaction._forget = lambda tx: os._exit(93)
store.delete(sid, archived=True, before_delete=lambda: queue.remove_for_session(sid, revision))
"""
    proc = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), session.id, revision], timeout=20
    )
    assert proc.returncode == 93
    recover_vault_transaction(store.paths)
    store.recover_deletions()
    assert store.exists(session.id, archived=True)
    assert queue.get("p-1").session_id == session.id
    assert (folder / "original.txt").read_bytes() == b"saved"
