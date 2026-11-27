from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from syvrio.infra.storage.database import transaction
from syvrio.memory.background import BackgroundMemory, dream
from syvrio.memory.background.dream import Dream
from syvrio.memory.background.jobs import read_job
from syvrio.memory.models import MemoryDraft

pytestmark = pytest.mark.memory


def add(service, text):
    return service.add(MemoryDraft(topic="notes", text=text))


def test_waiting_dream_uses_read_only_transactions(service):
    owner = BackgroundMemory(service)
    owner.schedule.tick(100)
    with transaction(service.paths, write=False):
        assert not owner.dream.due()
        assert owner.schedule.next_at() > 100
        owner.schedule.tick(101)


def test_due_schedule_retries_if_evidence_changes_before_enqueue(service, monkeypatch):
    owner = BackgroundMemory(service)
    owner.schedule.tick(100)
    due = owner.schedule.next_at()
    fingerprint = owner.dream.current_evidence_fingerprint

    def fingerprint_then_change():
        result = fingerprint()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(add, service, "Evidence changed during scheduling").result(timeout=3)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(owner.dream, "current_evidence_fingerprint", fingerprint_then_change)
        owner.schedule.tick(due)

    assert owner.dream.status()["state"] == "idle"
    assert owner.schedule.next_at() == due
    owner.schedule.tick(due)
    assert owner.dream.status()["state"] == "queued"
    assert owner.dream.status()["evidence_commit"] == service.store.evidence_commit()
    assert owner.schedule.next_at() > due


def test_change_during_planning_uses_frozen_input_and_charges_one_request(service, monkeypatch):
    add(service, "First fact")
    engine = Dream(service)
    engine.request()
    before = read_job(engine.path)
    next_batch = dream._next_batch

    def plan(*args):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(add, service, "Changed while planning").result(timeout=3)
        return next_batch(*args)

    monkeypatch.setattr(dream, "_next_batch", plan)

    def ask(_, prompt, payload, schema):
        assert [fact["text"] for fact in payload["evidence"]] == ["First fact"]
        return schema(summary="First profile")

    monkeypatch.setattr(dream, "ask", ask)
    engine.step(Event())
    after = read_job(engine.path)
    assert after["evidence_commit"] == before["evidence_commit"] < service.store.evidence_commit()
    assert after["requests"] == before["requests"] + 1
    assert service.profile.read_text() == "First profile"


def test_new_pending_inference_during_generation_keeps_valid_profile(service, monkeypatch):
    fact = add(service, "First fact")
    engine = Dream(service)
    engine.request()

    def ask(_, prompt, payload, schema):
        if prompt == dream.PROFILE:
            service.propose_inference("Unconfirmed proposal", [fact.id], "habit")
            return schema(summary="Current profile")
        service.propose_inference("Concurrent proposal", [fact.id], "habit")
        return schema(
            proposals=[{"kind": "habit", "text": "Concurrent proposal", "source_ids": [fact.id]}]
        )

    monkeypatch.setattr(dream, "ask", ask)
    engine.step(Event())
    assert engine.status()["requests"] == 1
    assert service.profile.read_text().strip() == "Current profile"
    engine.step(Event())
    assert engine.status()["proposed"] == 0
