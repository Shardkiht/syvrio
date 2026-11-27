from __future__ import annotations

import sqlite3

import pytest

from syvrio.infra.storage.database import transaction
from syvrio.memory.models import PendingItem
from syvrio.memory.queue import PendingQueue
from tests.support.storage import rejected

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

    from syvrio.infra.session import SessionStore

    sessions = SessionStore(tmp_path)
    session = sessions.create()
    sessions.set_archived(session.id, True)
    queue = PendingQueue(tmp_path)
    queue.add(make_item().model_copy(update={"session_id": session.id, "message_id": "m-1"}))
    queue.add(make_item("other"))
    original = queue.all()
    _, revision = queue.deletion_preview(session.id)

    def delete():
        return sessions.delete(
            session.id,
            archived=True,
            before_delete=lambda: queue.remove_for_session(session.id, revision),
        )

    if failed:
        with transaction(sessions.paths) as db:
            db.execute(
                "CREATE TRIGGER fail_delete BEFORE DELETE ON sessions BEGIN SELECT RAISE(ABORT,'fault'); END"
            )
        with pytest.raises(sqlite3.IntegrityError):
            delete()
        assert sessions.exists(session.id, archived=True)
        assert queue.all() == original
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
    assert not rejected(queue)


def test_rejection_preserves_candidate_fields(tmp_path):
    queue = PendingQueue(tmp_path)
    row = make_item()
    queue.add(row)
    queue.resolve(row.id, "rejected")
    assert not queue.all() and rejected(queue) == [row]


def test_resolve_missing_returns_none(tmp_path):
    assert PendingQueue(tmp_path).resolve("nope", "rejected") is None


def test_duplicate_add_is_idempotent_and_conflict_is_rejected(tmp_path):
    queue = PendingQueue(tmp_path)
    original = make_item("p-dup")
    queue.add(original)
    queue.add(original)
    with pytest.raises(ValueError, match="Duplicate"):
        queue.add(original.model_copy(update={"text": "changed"}))
    assert queue.all() == [original]


def test_failed_rejection_is_retryable(tmp_path):
    queue = PendingQueue(tmp_path)
    row = make_item()
    queue.add(row)
    with transaction(queue.paths) as db:
        db.execute(
            "CREATE TRIGGER inject AFTER UPDATE ON candidates BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        queue.resolve(row.id, "rejected")
    assert queue.all() == [row] and not rejected(queue)
    with transaction(queue.paths) as db:
        db.execute("DROP TRIGGER inject")
    queue.resolve(row.id, "rejected")
    assert not queue.all() and rejected(queue) == [row]


def test_process_interruption_preserves_session_and_related_candidate(tmp_path):
    import subprocess
    import sys

    from syvrio.infra.session import SessionStore

    sessions = SessionStore(tmp_path)
    session = sessions.create()
    sessions.append(
        session, {"type": "message", "role": "user", "id": "m1", "content": "synthetic"}
    )
    queue = PendingQueue(sessions.paths)
    row = make_item().model_copy(update={"session_id": session.id, "message_id": "m1"})
    queue.add(row)
    sessions.set_archived(session.id, True)
    code = """import os,sys
from syvrio.infra.storage.database import transaction
from syvrio.infra.session import SessionStore
from pathlib import Path
s=SessionStore(Path(sys.argv[1]))
with transaction(s.paths) as db:
    db.execute("delete from candidates")
    db.execute("delete from sessions")
    os._exit(42)
"""
    assert subprocess.run([sys.executable, "-c", code, str(tmp_path)]).returncode == 42
    assert sessions.load(session.id, archived=True).user_texts() == ["synthetic"]
    assert queue.all() == [row]


def test_duplicate_only_reads_matching_candidates(tmp_path):
    queue = PendingQueue(tmp_path)
    for index in range(12):
        queue.add(make_item(str(index)).model_copy(update={"text": f"item {index}"}))
    statements = []
    with transaction(queue.paths) as db:
        db.set_trace_callback(statements.append)
        result = queue.add_unique(make_item("other").model_copy(update={"text": "item 11"}))
    assert result.id == "11"
    assert sum(sql.startswith("SELECT memory_id FROM candidate_related") for sql in statements) == 1
