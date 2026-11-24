from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from syvrio.memory.models import MemoryDraft
from syvrio.memory.schema import SchemaError
from syvrio.memory.service import MemoryService

pytestmark = pytest.mark.memory


def test_inference_is_separate_from_facts_and_recall(service):
    source = service.note("喜欢在安静环境阅读", topic="preferences")
    before = service.store.topic_path("preferences").read_bytes()
    item = service.propose_inference("似乎偏好安静的活动", [source.id], kind="profile")
    assert item.status == "hypothesis"
    assert item.kind == "profile"
    assert item.source_ids == [source.id]
    assert datetime.fromisoformat(item.created_at).utcoffset() is not None
    assert item.updated_at == item.created_at
    assert service.store.topic_path("preferences").read_bytes() == before
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
    path = service.inferences.path_for(item.kind, item.id)
    before = path.read_bytes()
    again = service.propose_inference("  似乎喜欢阅读  ", [first.id, second.id], kind="profile")
    assert again == item
    assert item.source_ids == sorted([first.id, second.id])
    assert path.read_bytes() == before
    assert service.inferences.all() == [item]
    assert "confidence" not in item.model_dump()


def test_changed_evidence_creates_distinct_hypothesis(service):
    first = service.note("偏好纸质阅读", topic="preferences")
    second = service.note("常看合成读书笔记", topic="events")
    one = service.propose_inference("似乎喜欢阅读", [first.id], kind="profile")
    path = service.inferences.path_for(one.kind, one.id)
    before = path.read_bytes()
    two = service.propose_inference("似乎喜欢阅读", [first.id, second.id], kind="profile")
    assert one.id != two.id
    assert path.read_bytes() == before
    stored = {entry.id: entry for entry in service.inferences.all()}
    assert stored == {one.id: one, two.id: two}


@pytest.mark.parametrize("kind", ["profile", "emotion"])
def test_inference_kind_preserves_uncertainty(service, kind):
    source = service.note("最近常提起合成展览", topic="events")
    assert service.propose_inference("可能期待下次展览", [source.id], kind).status == "hypothesis"


@pytest.mark.parametrize("source_state", ["missing", "deprecated", "corrected", "inference"])
def test_inference_rejects_non_fact_or_inactive_sources(service, source_state):
    source = service.note("偏好纸质阅读", topic="preferences")
    source_id = source.id
    if source_state == "missing":
        source_id = "mem-missing"
    elif source_state == "deprecated":
        service.deprecate(source.id)
    elif source_state == "corrected":
        service.correct(source.id, MemoryDraft(topic=source.topic, text="偏好电子阅读"))
    else:
        source_id = service.propose_inference("似乎喜欢阅读", [source.id], kind="profile").id

    def files():
        return {path: path.read_bytes() for path in service.inferences.root.rglob("*.md")}

    before = files()
    with pytest.raises(ValueError, match="active memory"):
        service.propose_inference("可能适合阅读活动", [source_id], kind="profile")
    assert files() == before


@pytest.mark.parametrize("ids", [[], [""], ["   "]])
def test_inference_requires_nonempty_evidence(service, ids):
    with pytest.raises(ValidationError):
        service.propose_inference("合成假设", ids, kind="profile")
    assert not list(service.inferences.root.rglob("*.md"))


@pytest.mark.parametrize("text", [" ", "密码是 synthetic-only"])
def test_inference_rejects_empty_text_and_credentials(service, text):
    source = service.note("合成事实")
    with pytest.raises(ValueError):
        service.propose_inference(text, [source.id], kind="profile")
    assert not list(service.inferences.root.rglob("*.md"))


def test_inference_damaged_file_fails_without_overwriting(service):
    source = service.note("合成事实")
    item = service.propose_inference("合成假设", [source.id], kind="profile")
    path = service.inferences.path_for(item.kind, item.id)
    original = path.read_bytes()
    damaged = original.replace(b"`hypothesis`", b"`invalid-status`")
    assert damaged != original
    path.write_bytes(damaged)
    with pytest.raises(SchemaError, match="status"):
        service.propose_inference("另一条合成假设", [source.id], kind="profile")
    assert path.read_bytes() == damaged
    assert list(service.inferences.root.rglob("*.md")) == [path]
