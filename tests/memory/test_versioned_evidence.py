from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest

from syvrio.infra.config import Config, RetrievalConfig
from syvrio.infra.session import SessionStore
from syvrio.infra.storage.database import transaction
from syvrio.memory.background import dream
from syvrio.memory.background.dream import Dream
from syvrio.memory.background.jobs import job_key, read_job, save_job
from syvrio.memory.models import MemoryCorrection, MemoryDraft
from syvrio.memory.service import MemoryService
from syvrio.memory.turn_undo import TurnJournal, TurnUndoError

pytestmark = pytest.mark.memory


def test_correct_partial_fields_keeps_both_sources_and_detects_stale_edits(service):
    def origin(text):
        sessions = SessionStore(service.paths)
        session = sessions.create()
        sessions.append(
            session, {"type": "message", "role": "user", "id": "m-test", "content": text}
        )
        return {"session_id": session.id, "message_id": "m-test"}

    original = origin("original")
    corrected = origin("correction")
    entry = service.memorize(
        MemoryDraft(
            text="old",
            topic="notes",
            context={
                "event_time": "2024",
                "location": "old location",
                "emotion": {"event": "calm", "recollection": "glad"},
            },
        ),
        **original,
    )
    old_commit = service.store.evidence_commit()
    patch = MemoryCorrection(
        text="new",
        context={"location": None, "emotion": {"event": None}},
        expected_revision=entry.revision,
    )
    new = service.correct(entry.id, patch, **corrected)
    new_commit = service.store.evidence_commit()
    assert new.id == entry.id and new_commit > old_commit
    assert new.context.model_dump(exclude_none=True) == {
        "event_time": "2024",
        "emotion": {"recollection": "glad"},
    }
    assert new.topic == entry.topic and new.status == entry.status
    assert service.read_memory_source(new.id, old_commit)["message"]["content"] == "original"
    assert service.read_memory_source(new.id, new_commit)["message"]["content"] == "correction"
    for src in (original, corrected):
        assert service.memory_referencing_session(src["session_id"]) == new.id
    with pytest.raises(ValueError, match="changed"):
        service.correct(new.id, patch)
    assert service.correct(new.id, MemoryCorrection(text="new")).revision == new.revision
    assert service.store.evidence_commit() == new_commit
    history = service.store.version_history(new.id)
    assert [(row["text"], row["commit_id"]) for row in history] == [
        ("old", old_commit),
        ("new", new_commit),
    ]

    retired = service.deprecate(new.id)
    assert retired.source_session_id == corrected["session_id"]
    assert retired.source_message_id == corrected["message_id"]
    assert (
        service.read_memory_source(new.id, service.store.evidence_commit())["message"]["content"]
        == "correction"
    )
    assert [row["status"] for row in service.store.version_history(new.id)] == [
        "active",
        "active",
        "deprecated",
    ]
    assert service.deprecate(new.id).revision == retired.revision
    assert len(service.store.version_history(new.id)) == 3


def test_dream_resumes_mid_batch_on_disk_with_original_draft_facts_and_inferences(
    service, monkeypatch
):
    first = service.note("original A", "a")
    second = service.note("original B", "b")
    hypothesis = service.propose_inference("confirmed original", [first.id], "habit")
    service.review_inference(hypothesis.id, "confirm")
    later = service.propose_inference("confirmed later", [second.id], "habit")
    cut = service.store.evidence_commit()
    engine = Dream(service)
    job_id = engine.request()
    batches = []

    def one_row(evidence, cursor, *_):
        index = cursor[0]
        return evidence[index : index + 1], [min(index + 1, len(evidence)), 0]

    def ask(_, prompt, data, schema):
        if prompt == dream.PROFILE:
            batches.append((data["draft"], data["evidence"][0]["text"]))
            return schema(summary=f"draft {len(batches)}")
        (fact,) = data["facts"]
        return schema(
            proposals=[
                {"kind": "habit", "text": "derived " + fact["text"], "source_ids": [fact["id"]]}
            ]
        )

    monkeypatch.setattr(dream, "_next_batch", one_row)
    monkeypatch.setattr(dream, "ask", ask)
    engine.step(Event())
    checkpoint = read_job(engine.path)
    assert checkpoint["cursor"] == [1, 0] and checkpoint["partial"] == "draft 1"
    with transaction(service.paths, write=False) as db:
        saved = db.execute(
            "SELECT evidence_commit,checkpoint FROM jobs WHERE kind='dream' AND target='job'"
        ).fetchone()
    assert saved["evidence_commit"] == cut
    assert json.loads(saved["checkpoint"]) == {
        k: v for k, v in checkpoint.items() if k not in {"state", "retry_at", "evidence_commit"}
    }
    service.correct(first.id, MemoryCorrection(text="corrected A"))
    with transaction(service.paths) as db:
        db.execute("UPDATE topics SET archived=1 WHERE name=?", (second.topic,))
    service.note("added later", "c")
    service.review_inference(later.id, "confirm")
    restarted = Dream(MemoryService(service.config))
    assert read_job(restarted.path) == checkpoint
    for _ in range(8):
        if restarted.status()["state"] == "completed":
            break
        if restarted.status()["state"] == "paused":
            assert restarted.request() == job_id
        restarted.step(Event())
    assert restarted.status()["state"] == "completed"
    assert restarted.status()["evidence_commit"] == cut
    assert sorted(text for _, text in batches) == sorted(
        ["original A", "original B", "confirmed original"]
    )
    assert [prior for prior, _ in batches] == ["", "draft 1", "draft 2"]
    spawned = [i for i in service.list_inferences() if i.text.startswith("derived ")]
    assert {i.text for i in spawned} == {"derived original A", "derived original B"}
    assert all(
        i.source_versions == service.store.evidence_sources(i.source_ids, cut) for i in spawned
    )
    assert restarted.request() != job_id
    fresh, confirmed, _, _ = dream._state(service, restarted.status()["evidence_commit"])
    assert {row["text"] for row in fresh} == {"corrected A", "added later"}
    assert {row["id"] for row in confirmed} == {hypothesis.id, later.id}


def test_history_and_current_row_roll_back_together(service):
    entry = service.note("before", "test")
    cut = service.store.evidence_commit()
    with pytest.raises(RuntimeError), transaction(service.paths):
        service.correct(entry.id, MemoryCorrection(text="not committed"))
        assert service.store.version_history(entry.id)[-1]["text"] == "not committed"
        raise RuntimeError("rollback")
    assert service.store.find(entry.id).text == "before"
    assert service.store.evidence_commit() == cut
    assert len(service.store.version_history(entry.id)) == 1


def _interrupt_before_checkpoint(vault):
    service = MemoryService(Config(vault=Path(vault), retrieval=RetrievalConfig(method="keyword")))
    save = dream.save_job

    def interrupted(path, job):
        if job["phase"] == "hypotheses":
            os._exit(23)
        save(path, job)

    dream.save_job = interrupted
    dream.ask = lambda _, prompt, payload, schema: schema(summary="uncommitted")
    Dream(service).step(Event())


def test_process_exit_rolls_back_publication_and_retries_same_input(service, monkeypatch):
    entry = service.note("original")
    engine = Dream(service)
    engine.request()
    before = read_job(engine.path)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests.memory.test_versioned_evidence import _interrupt_before_checkpoint as run; import sys; run(sys.argv[1])",
            str(service.config.vault),
        ],
        timeout=45,
    )
    assert child.returncode == 23
    interrupted = read_job(engine.path)
    assert interrupted["requests"] == 1 and interrupted["cursor"] == before["cursor"]
    assert interrupted["evidence_commit"] == before["evidence_commit"]
    assert not service.profile.read_text()
    service.correct(entry.id, MemoryCorrection(text="edited during outage"))

    def ask(_, prompt, payload, schema):
        assert [item["text"] for item in payload["evidence"]] == ["original"]
        return schema(summary="recovered")

    monkeypatch.setattr(dream, "ask", ask)
    resumed = Dream(MemoryService(service.config))
    resumed.step(Event())
    assert resumed.status()["phase"] == "hypotheses" and resumed.status()["requests"] == 2
    assert service.profile.read_text() == "recovered"


def test_undo_refuses_pinned_fact_and_new_round_drops_revoked_evidence(service, monkeypatch):
    session = SessionStore(service.paths).create()
    journal = TurnJournal(service.paths, session.id, "synthetic-turn")
    with journal.capture():
        fact = service.note("temporary")
    engine = Dream(service)
    engine.request()
    cut = engine.status()["evidence_commit"]
    with pytest.raises(TurnUndoError, match="background task"):
        journal.restore()
    assert service.store.find(fact.id) == fact and service.store.evidence_commit() == cut
    monkeypatch.setattr(
        dream,
        "ask",
        lambda _, prompt, payload, schema: (
            schema(summary="ok") if prompt == dream.PROFILE else schema(proposals=[])
        ),
    )
    engine.step(Event())
    engine.step(Event())
    assert engine.status()["state"] == "completed"
    assert journal.restore()
    assert not service.store.find(fact.id) and service.store.evidence_commit() > cut
    engine.request()
    engine.step(Event())
    assert service.profile.read_text() == ""


@pytest.mark.parametrize(
    ("state", "finish"),
    [("queued", "completed"), ("running", None), ("paused", None), ("failed", "completed")],
)
def test_undo_preserves_another_jobs_read_view_until_release(service, state, finish):
    sessions = SessionStore(service.paths)
    journal = TurnJournal(service.paths, sessions.create().id, "turn-with-two-facts")
    older = job_key(service.paths, "context", "older-evidence")
    held = job_key(service.paths, "context", "review-evidence")
    save_job(older, {"state": "queued", "evidence_commit": service.store.evidence_commit()})
    with journal.capture():
        pinned = service.note("included in the checkpoint", "notes")
        cut = service.store.evidence_commit()
        later = service.note("written after the checkpoint", "notes")
    save_job(held, {"state": state, "evidence_commit": cut})

    history = service.store.version_history(pinned.id)
    with pytest.raises(TurnUndoError):
        journal.restore()
    assert service.store.version_history(pinned.id) == history
    assert service.store.find(later.id) == later
    assert [record["id"] for record in service.store.facts_at(cut)] == [pinned.id]
    assert journal.has_effects()

    save_job(held, {"state": finish, "evidence_commit": cut} if finish else None)
    assert journal.restore()
    assert not service.store.version_history(pinned.id)
    assert not service.store.find(later.id)


def test_task_pins_only_facts_visible_at_its_cut(service):
    journal = TurnJournal(service.paths, SessionStore(service.paths).create().id, "archived-topic")
    with journal.capture():
        fact = service.note("visible before archiving", "notes")
    from_before_archive = service.store.evidence_commit()
    with transaction(service.paths) as db:
        db.execute("UPDATE topics SET archived=1 WHERE name=?", (fact.topic,))
    from_after_archive = service.store.evidence_commit()
    assert not service.store.facts_at(from_after_archive)
    job = job_key(service.paths, "distillation", "fixed-evidence")
    save_job(job, {"evidence_commit": from_before_archive})
    with pytest.raises(TurnUndoError, match="background task"):
        journal.restore()
    save_job(job, {"evidence_commit": from_after_archive})
    assert journal.restore()
