"""Review routing is determined by an explicit model question and validated references."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from syvrio.memory.extractor import StaticExtractor
from syvrio.memory.models import Candidate, ExtractionResult, MemoryDraft

pytestmark = pytest.mark.memory


def test_clear_personal_fact_needs_no_score_or_review(service):
    draft = Candidate(text="我对花生过敏", topic="health")
    result = service.import_text(
        draft.text, extractor=StaticExtractor(ExtractionResult(memories=[draft]))
    )
    assert [entry.text for entry in result.admitted] == [draft.text]
    assert not service.pending()


def test_explicit_review_question_keeps_related_fact_intact(service):
    fact = service.add(MemoryDraft(text="我住在杭州", topic="housing"))
    draft = Candidate(
        text="我搬到苏州了", topic="housing", question="是否已经搬家？", related_ids=[fact.id]
    )
    result = service.import_text(
        draft.text, extractor=StaticExtractor(ExtractionResult(memories=[draft]))
    )
    assert not result.admitted and len(result.queued) == 1
    pending = service.pending()[0]
    assert (pending.question, pending.related_ids) == (draft.question, [fact.id])
    assert service.store.find(fact.id).status == "active"


def test_unknown_memory_reference_is_an_error_and_never_becomes_pending(service):
    draft = Candidate(
        text="可能换了工作", topic="career", question="已经换工作了吗？", related_ids=["missing"]
    )
    result = service.import_text(
        draft.text, extractor=StaticExtractor(ExtractionResult(memories=[draft]))
    )
    assert result.blocked
    assert not result.admitted and not service.pending()


def test_extraction_rejects_obsolete_confidence_field():
    with pytest.raises(ValidationError):
        Candidate.model_validate({"text": "合成事实", "topic": "test", "confidence": 0.2})
