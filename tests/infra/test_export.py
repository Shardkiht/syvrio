from __future__ import annotations

from unittest.mock import patch

import pytest

from syvrio.infra.session.store import SessionStore
from syvrio.infra.storage.database import transaction
from syvrio.infra.vault import export
from syvrio.infra.vault.files import initialize_vault
from syvrio.infra.vault.sync import GitSyncService
from syvrio.memory.background.jobs import job_key, save_job
from syvrio.memory.models import MemoryDraft

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

    with transaction(service.paths) as db:
        db.execute(
            "UPDATE inference_sources SET memory_id=? WHERE inference_id=?", (b.id, items[0].id)
        )
    assert git.is_dirty()
    contents = (git.repo.path / "content" / "inferences.jsonl").read_text(encoding="utf-8")
    assert b.id in contents


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
