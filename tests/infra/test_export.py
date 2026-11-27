from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from syvrio.infra.session.store import SessionStore
from syvrio.infra.storage.database import transaction
from syvrio.infra.vault import export
from syvrio.infra.vault.files import initialize_vault
from syvrio.infra.vault.sync import GitSyncService
from syvrio.memory.background.jobs import job_key, save_job
from syvrio.memory.models import MemoryDraft
from syvrio.memory.turn_undo import TurnJournal

pytestmark = pytest.mark.memory


def test_status_reuses_projection_and_refreshes_each_changed_input(service):
    session = SessionStore(service.paths).create()
    git = GitSyncService(service.config, service.paths)
    initialize_vault(git, service.initialize_files)
    tree = git.repo.path / "content"
    soul = service.paths.vault_dir / "SOUL.md"
    with patch.object(export, "render", wraps=export.render) as render:
        assert not git.is_dirty()
        save_job(job_key(service.paths, "dream", "schedule"), {"next_at": 1})
        with transaction(service.paths) as db:
            db.execute("UPDATE sessions SET tokens=tokens+1 WHERE id=?", (session.id,))
            db.execute("UPDATE session_events SET active=active WHERE session_id=?", (session.id,))
        assert git.status()["modified"] == 0
        render.assert_not_called()

        item = service.add(MemoryDraft(topic="notes", text="First item"))
        assert git.is_dirty() and git.is_dirty()
        assert render.call_count == 1
        assert item.text in next((tree / "memories").rglob("*.md")).read_text(encoding="utf-8")

        soul.write_text("Updated instructions", encoding="utf-8")
        assert (
            git.is_dirty()
            and (tree / "SOUL.md").read_text(encoding="utf-8") == "Updated instructions"
        )
        assert render.call_count == 2
        (tree / "unexpected.txt").write_text("Changed outside the service", encoding="utf-8")
        git.is_dirty()
        assert render.call_count == 3 and not (tree / "unexpected.txt").exists()
        soul.unlink()
        git.is_dirty()
        assert render.call_count == 4 and not (tree / "SOUL.md").exists()


def test_export_batches_sources_and_invalidates_on_direct_source_changes(service):
    a, b = (service.add(MemoryDraft(topic="notes", text=text)) for text in ("First", "Second"))
    items = [service.propose_inference(f"Finding {i}", [a.id], "habit") for i in range(5)]
    git = GitSyncService(service.config, service.paths)
    initialize_vault(git, service.initialize_files)
    sql = []
    with transaction(service.paths, write=False) as db:
        db.set_trace_callback(sql.append)
        export.render(service.paths)
        db.set_trace_callback(None)
    assert sum("FROM inference_sources" in stmt for stmt in sql) == 1

    commit = service.store.version_history(b.id)[-1]["commit_id"]
    with transaction(service.paths) as db:
        db.execute(
            "UPDATE inference_sources SET memory_id=?,memory_version=? WHERE inference_id=?",
            (b.id, commit, items[0].id),
        )
    assert git.is_dirty()
    contents = (git.repo.path / "content" / "inferences.jsonl").read_text(encoding="utf-8")
    item = next(row for row in map(json.loads, contents.splitlines()) if row["id"] == items[0].id)
    assert item["source_ids"] == [b.id] and item["source_versions"] == {b.id: commit}


def test_rolled_back_revision_cannot_be_reused_for_a_different_export(service):
    git = GitSyncService(service.config, service.paths)
    initialize_vault(git, service.initialize_files)
    profile_path = git.repo.path / "content" / "profile.md"
    before = profile_path.read_bytes() if profile_path.exists() else None
    with pytest.raises(ValueError, match="rollback"):
        with transaction(service.paths):
            service.profile.publish("Discarded", {})
            assert not git.is_dirty()
            raise ValueError("rollback")

    assert (profile_path.read_bytes() if profile_path.exists() else None) == before
    service.profile.publish("Committed", {})
    assert git.is_dirty()
    assert (git.repo.path / "content" / "profile.md").read_text(encoding="utf-8") == "Committed\n"


def test_git_exports_effective_changes_and_keeps_audit_after_undo(service):
    entry = service.note("Original fact", "notes")
    store = SessionStore(service.paths)
    session = store.create()
    message = store.append(session, dict(type="message", role="user", content="Edit fact"))
    git = GitSyncService(service.config, service.paths)
    initialize_vault(git, service.initialize_files)
    records = git.repo.path / "content" / "records"

    def exported(name):
        lines = (records / name).read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    baseline = exported("memory_changes.jsonl")
    assert len(baseline) == 1 and baseline[0]["change"] == "created"
    journal = TurnJournal(service.paths, session.id, message["id"])
    with journal.capture():
        service.correct(
            entry.id,
            MemoryDraft(text="Revised fact", topic=entry.topic),
            session_id=session.id,
            message_id=message["id"],
        )
        service.deprecate(entry.id)
    assert git.is_dirty()
    changes = exported("memory_changes.jsonl")
    assert changes[:1] == baseline
    assert [row["change"] for row in changes[1:]] == ["corrected", {"status": "deprecated"}]
    assert all(
        set(row) == {"seq", "memory_id", "change"} and row["memory_id"] == entry.id
        for row in changes
    )
    audit = exported("audit.jsonl")
    assert [row["action"] for row in audit] == ["add", "correct", "update"]
    details = audit[1]["details"]
    assert details["memory_id"] == entry.id and details["session_id"] == session.id
    assert details["message_id"] == message["id"] and details["revision"] > entry.revision

    journal.restore()
    assert git.is_dirty()
    assert exported("memory_changes.jsonl") == baseline
    after = exported("audit.jsonl")
    assert after[: len(audit)] == audit
    assert sorted(row["reverts"] for row in after if row["action"] == "undo_memory_change") == [
        row["seq"] for row in audit[1:]
    ]

    store.set_archived(session.id, True)
    assert git.is_dirty()
    visible = exported("audit.jsonl")
    assert all(row["action"] != "correct" for row in visible)
    visible_seq = {row["seq"] for row in visible}
    assert all("reverts" not in row or row["reverts"] in visible_seq for row in visible)

    with transaction(service.paths) as db:
        db.execute("UPDATE topics SET archived=1 WHERE name=?", (entry.topic,))
    assert git.is_dirty()
    assert exported("memory_changes.jsonl") == []
    assert exported("audit.jsonl") == []


def test_export_preserves_change_details_of_unrelated_audit_events(service):
    entry = service.note("Recorded fact", "notes")
    fields = {"memory_id": entry.id, "change": {"batch": 1}, "reason": "synthetic"}
    service.audit.write("review_progress", **fields)
    files = export.render(service.paths)
    changes = [json.loads(line) for line in files["records/memory_changes.jsonl"].splitlines()]
    audit = [json.loads(line) for line in files["records/audit.jsonl"].splitlines()]
    assert [row["change"] for row in changes] == ["created"]
    assert audit[-1]["action"] == "review_progress" and audit[-1]["details"] == fields


def test_new_memory_action_needs_no_export_registration(service, monkeypatch):
    write_change = service.store._change

    def record_new_action(db, entry_id, action, change, revision, **origin):
        return write_change(db, entry_id, "reconcile", change, revision, **origin)

    monkeypatch.setattr(service.store, "_change", record_new_action)
    entry = service.note("Synthetic fact", "notes")
    ordinary = {"memory_id": entry.id, "change": "progress only"}
    service.audit.write("reconcile", **ordinary)
    files = export.render(service.paths)
    changes = [json.loads(line) for line in files["records/memory_changes.jsonl"].splitlines()]
    audit = [json.loads(line) for line in files["records/audit.jsonl"].splitlines()]
    assert changes == [{"seq": audit[0]["seq"], "memory_id": entry.id, "change": "created"}]
    assert [row["action"] for row in audit] == ["reconcile", "reconcile"]
    assert "change" not in audit[0]["details"]
    assert audit[1]["details"] == ordinary
