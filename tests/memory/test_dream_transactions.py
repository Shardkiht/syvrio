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


def test_change_during_planning_keeps_budget_and_skips_stale_request(service, monkeypatch):
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
    monkeypatch.setattr(dream, "ask", lambda *args: pytest.fail("stale request sent"))
    engine.step(Event())
    assert read_job(engine.path) == before


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
