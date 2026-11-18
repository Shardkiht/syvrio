"""Application Memory 用例：Pending、推断、导入与画像读取。"""

from __future__ import annotations

import pytest

from syvrio.application.memory import MemoryUseCases
from syvrio.memory.extractor import StaticExtractor
from syvrio.memory.models import Candidate, ExtractionResult
from syvrio.memory.service import MemoryService

pytestmark = pytest.mark.memory


def build_service(config):
    service = MemoryService(
        config,
        extractor=StaticExtractor(
            ExtractionResult(memories=[Candidate(topic="preference", text="喜欢清淡饮食")])
        ),
    )
    service.init_vault()
    return service


def test_pending_approve_reject_and_edit(config):
    service = build_service(config)
    memory = MemoryUseCases(service)
    # 低置信度候选进入 Pending，不直接写事实。
    for text in ("pending one", "pending two"):
        extractor = StaticExtractor(
            ExtractionResult(memories=[Candidate(topic="preference", text=text, confidence=0.1)])
        )
        service.import_text(text, extractor=extractor)
    items = memory.pending().items

    assert len(items) == 2
    std = next(item for item in items if item.text == "pending one")
    assert memory.pending_item(std.id).text == "pending one"
    saved = memory.approve(std.id, text_override="pending one edited")
    assert saved.text == "pending one edited"
    assert memory.pending_count() == 1
    remaining = memory.pending().items[0]
    rejected = memory.reject(remaining.id)
    assert rejected.id == remaining.id
    assert memory.pending_count() == 0


def test_inference_review_keeps_facts_separate(config):
    service = build_service(config)
    memory = MemoryUseCases(service)
    source = service.note("合成事实")
    item = service.propose_inference("可能偏好安静活动", [source.id])

    review = memory.load_inference_review()
    assert [entry.id for entry in review.items] == [item.id]
    assert review.sources[source.id] == "合成事实"

    confirmed = memory.review_inference(item.id, "confirm")
    assert confirmed.updated_id == item.id
    assert confirmed.feedback == "Confirmed."
    assert service.list_inferences()[0].status == "confirmed"
    assert len(service.current_facts()) == 1  # 确认推断不写入事实

    second = service.propose_inference("第二条假设", [source.id])
    rejected = memory.review_inference(second.id, "reject")
    assert rejected.feedback == "Rejected."
    assert service.list_inferences()[-1].status == "rejected"

    edited = memory.review_inference(item.id, "correct", "修正后的假设")
    assert edited.feedback == "Edit saved as a new pending hypothesis."
    assert any(
        entry.text == "修正后的假设" and entry.status == "hypothesis"
        for entry in service.list_inferences()
    )


def test_import_preview_then_apply(config, tmp_path):
    service = build_service(config)
    memory = MemoryUseCases(service)
    document = tmp_path / "notes.md"
    document.write_text("我喜欢清淡饮食，也喜欢散步。" * 20, encoding="utf-8")

    extractor = StaticExtractor(
        ExtractionResult(memories=[Candidate(topic="preference", text="喜欢清淡饮食")])
    )
    preview = memory.import_path(document, apply=False, extractor=extractor)
    assert preview.files == 1 and preview.chunks >= 1
    assert preview.saved >= 1
    assert service.current_facts() == []

    applied = memory.import_path(document, apply=True, extractor=extractor)
    assert applied.saved >= 1
    assert service.current_facts()


def test_import_continues_after_chunk_failure(config, tmp_path, monkeypatch):
    service = build_service(config)
    memory = MemoryUseCases(service)
    document = tmp_path / "notes.md"
    document.write_text("合成内容。" * 4000, encoding="utf-8")
    original = service.import_text
    calls = {"count": 0}

    def fail_first(chunk, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("synthetic chunk failure")
        return original(chunk, **kwargs)

    monkeypatch.setattr(service, "import_text", fail_first)
    extractor = StaticExtractor(
        ExtractionResult(memories=[Candidate(topic="preference", text="合成内容")])
    )
    result = memory.import_path(document, apply=True, extractor=extractor)

    assert result.errors and "synthetic chunk failure" in result.errors[0]
    assert result.chunks >= 1  # 后续块继续处理


def test_profile_without_summary_is_read_only(config):
    service = build_service(config)
    memory = MemoryUseCases(service)

    snapshot = memory.profile()

    assert snapshot.status == "Not generated"
    assert "No profile summary yet" in snapshot.body
    assert service.current_facts() == []
