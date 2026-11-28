from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
from pydantic import ValidationError

from syvrio.infra.storage.database import transaction
from syvrio.memory.service import MemoryService

pytestmark = pytest.mark.memory


def test_inference_is_separate_from_facts_and_recall(service):
    source = service.note("喜欢在安静环境阅读", topic="preferences")
    before = service.store.entries("preferences")
    item = service.propose_inference("似乎偏好安静的活动", [source.id], kind="profile")
    assert item.status == "hypothesis"
    assert item.kind == "profile"
    assert item.source_ids == [source.id]
    assert datetime.fromisoformat(item.created_at).utcoffset() is not None
    assert item.updated_at == item.created_at
    assert service.store.entries("preferences") == before
    assert service.store.find(item.id) is None
    assert service.recall("似乎偏好安静的活动")[0].id == source.id
    assert all(hit.id != item.id for hit in service.recall("安静"))
    assert service.pending() == []
    fresh = MemoryService(service.config)
    assert fresh.inferences.all() == [item]


def test_repeated_evidence_is_idempotent_without_confidence_inflation(service):
    first = service.note("偏好纸质阅读", topic="preferences")
    second = service.note("周末常去合成书屋", topic="events")
    item = service.propose_inference(
        "似乎喜欢阅读", [second.id, first.id, second.id], kind="profile"
    )
    before = item
    again = service.propose_inference("  似乎喜欢阅读  ", [first.id, second.id], kind="profile")
    assert again == item
    assert item.source_ids == sorted([first.id, second.id])
    assert before in service.inferences.all()
    assert service.inferences.all() == [item]
    assert "confidence" not in item.model_dump()


def test_propose_and_review_without_loading_inference_list(service, monkeypatch):
    sources = [service.note(f"synthetic fact {i}").id for i in range(2)]
    monkeypatch.setattr(service.inferences, "all", lambda: pytest.fail("full inference scan"))
    proposal = service.propose_inference_with_status
    item, created = proposal("Synthetic idea", sources, "habit")
    assert created
    assert proposal("synthetic  idea", list(reversed(sources)), "habit") == (item, False)
    assert proposal("Synthetic idea", sources[:1], "habit")[1]
    assert service.review_inference(item.id, "confirm").status == "confirmed"


def test_changed_evidence_creates_distinct_hypothesis(service):
    first = service.note("偏好纸质阅读", topic="preferences")
    second = service.note("常看合成读书笔记", topic="events")
    one = service.propose_inference("似乎喜欢阅读", [first.id], kind="profile")
    before = one
    two = service.propose_inference("似乎喜欢阅读", [first.id, second.id], kind="profile")
    assert one.id != two.id
    assert before in service.inferences.all()
    stored = {entry.id: entry for entry in service.inferences.all()}
    assert stored == {one.id: one, two.id: two}


@pytest.mark.parametrize("kind", ["profile", "emotion"])
def test_inference_kind_preserves_uncertainty(service, kind):
    source = service.note("最近常提起合成展览", topic="events")
    assert service.propose_inference("可能期待下次展览", [source.id], kind).status == "hypothesis"


@pytest.mark.parametrize("source_state", ["missing", "deprecated", "inference"])
def test_inference_rejects_non_fact_or_inactive_sources(service, source_state):
    source = service.note("偏好纸质阅读", topic="preferences")
    source_id = source.id
    if source_state == "missing":
        source_id = "mem-missing"
    elif source_state == "deprecated":
        service.deprecate(source.id)
    else:
        source_id = service.propose_inference("似乎喜欢阅读", [source.id], kind="profile").id

    def files():
        return service.inferences.all()

    before = files()
    with pytest.raises(ValueError, match="active memory"):
        service.propose_inference("可能适合阅读活动", [source_id], kind="profile")
    assert files() == before


@pytest.mark.parametrize("ids", [[], [""], ["   "]])
def test_inference_requires_nonempty_evidence(service, ids):
    with pytest.raises(ValidationError):
        service.propose_inference("合成假设", ids, kind="profile")
    assert not service.inferences.all()


@pytest.mark.parametrize("text", [" ", "密码是 synthetic-only"])
def test_inference_rejects_empty_text_and_credentials(service, text):
    source = service.note("合成事实")
    with pytest.raises(ValueError):
        service.propose_inference(text, [source.id], kind="profile")
    assert not service.inferences.all()


def test_inference_damaged_file_fails_without_overwriting(service):
    source = service.note("合成事实")
    item = service.propose_inference("合成假设", [source.id], kind="profile")
    with pytest.raises(sqlite3.IntegrityError), transaction(service.paths) as db:
        db.execute("UPDATE inferences SET status='invalid' WHERE id=?", (item.id,))
    assert service.inferences.all() == [item]
