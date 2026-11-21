from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from syvrio.memory.extractor import ExtractorError, parse_extraction
from syvrio.memory.models import MemoryContext

pytestmark = pytest.mark.memory


@pytest.mark.parametrize("value", ["2024", "2024-02", "2024-02-29"])
def test_context_preserves_date_precision(value):
    payload = {"memories": [{"text": "合成事件", "context": {"event_time": value}}]}
    candidate = parse_extraction(json.dumps(payload)).memories[0]
    assert candidate.context.event_time == value
    assert candidate.context.valid_to is None


@pytest.mark.parametrize(
    "value", ["0000", "2023-02-29", "2024-13", "2024-1", "昨天", "2024-02-29T12:00:00Z"]
)
def test_context_rejects_invalid_or_invented_date_formats(value):
    with pytest.raises(ValidationError):
        MemoryContext(event_time=value)


def test_context_relative_time_without_anchor_stays_unknown():
    candidate = parse_extraction('{"memories":[{"text":"去年去过合成展馆"}]}').memories[0]
    assert candidate.text.startswith("去年")
    assert candidate.context is None


def test_missing_context_does_not_add_dates_or_emotions():
    assert parse_extraction('{"memories":[{"text":"喜欢热饮"}]}').memories[0].context is None


@pytest.mark.parametrize(
    "start,end", [("2025", "2024"), ("2024-03", "2024-02"), ("2024-02-29", "2024-02-28")]
)
def test_context_rejects_reversed_validity(start, end):
    with pytest.raises(ValidationError, match="结束时间"):
        MemoryContext(valid_from=start, valid_to=end)


def test_partial_validity_dates_are_not_fabricated():
    context = MemoryContext(valid_from="2024-06-15", valid_to="2024")
    assert context.valid_from == "2024-06-15"
    assert context.valid_to == "2024"


def test_context_represents_stated_location_without_an_inferred_category():
    assert MemoryContext(location="合成地点").model_dump(exclude_none=True) == {
        "location": "合成地点"
    }
    with pytest.raises(ValidationError):
        MemoryContext(location="合成地点", location_kind="residence")


@pytest.mark.parametrize(
    "context",
    [
        {"location_kind": "event"},
        {"emotion": {"inferred": "伤心"}},
        {"emotion": "开心"},
        {"emotion_source": "explicit"},
        {"emotion_at": "event"},
        {"emotion": {"event": "  "}},
        {"emotion": {"recollection": ["开心"]}},
    ],
)
def test_context_rejects_ambiguous_or_inferred_labels(context):
    with pytest.raises(ValidationError):
        MemoryContext.model_validate(context)


@pytest.mark.parametrize(
    "wording",
    [
        {"event": "有点紧张"},
        {"recollection": "挺开心"},
        {"event": "有点紧张", "recollection": "挺开心"},
        {"unspecified": "五味杂陈"},
    ],
)
def test_extraction_preserves_only_the_stated_emotions(wording):
    payload = {"memories": [{"text": "参加合成展览", "context": {"emotion": wording}}]}
    context = parse_extraction(json.dumps(payload)).memories[0].context
    assert context.model_dump(exclude_none=True) == {"emotion": wording}


@pytest.mark.parametrize("empty", [None, {}, {"event": None, "recollection": None}])
def test_empty_emotion_is_omitted(empty):
    context = MemoryContext(emotion=empty)
    assert context.emotion is None
    assert context.markdown_fields() == []


@pytest.mark.parametrize("adapter", ["chat", "import"])
@pytest.mark.parametrize("wording", [{"event": "有点紧张"}, {}])
def test_model_extraction_keeps_only_emotions_quoted_in_the_source(adapter, wording, monkeypatch):
    from syvrio.infra.config import LLMConfig
    from syvrio.memory import extractor

    emotions = {**wording, "recollection": "十分兴奋"}
    payload = {"memories": [{"text": "参加展览", "context": {"emotion": emotions}}]}
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
    )
    config = LLMConfig(provider="local")
    source = "参加展览，当时有点紧张。"
    if adapter == "chat":
        monkeypatch.setattr(extractor, "complete", lambda *args, **kwargs: response)
        result = extractor.LLMExtractor(config).extract(source, context="一条旧记忆：十分兴奋。")
    else:
        model = extractor.ImportExtractor(config, completer=lambda *args, **kwargs: response)
        result = model.extract(source)
    candidate = result.memories[0]
    assert candidate.text == "参加展览"
    if wording:
        assert candidate.context.model_dump(exclude_none=True) == {"emotion": wording}
    else:
        assert candidate.context is None


def test_llm_extractor_requests_optional_context_without_current_date(monkeypatch):
    from syvrio.infra.config import LLMConfig
    from syvrio.memory import extractor

    captured = []
    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "synthetic-key")

    def fake_complete(config, messages, **kwargs):
        captured.extend(messages)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"memories":[{"text":"去年参加合成展览"}]}')
                )
            ]
        )

    monkeypatch.setattr(extractor, "complete", fake_complete)
    candidate = extractor.LLMExtractor(LLMConfig()).extract("去年参加合成展览").memories[0]
    assert "没有可靠锚点" in captured[0]["content"]
    assert all(slot in captured[0]["content"] for slot in ("event", "recollection", "unspecified"))
    assert candidate.context is None


def test_parse_plain_json_preserves_explicit_question_and_references():
    result = parse_extraction(
        json.dumps(
            {
                "memories": [
                    {"text": "喜欢清淡风味", "topic": "preferences"},
                    {
                        "text": "可能在搬家",
                        "topic": "housing",
                        "question": "搬家完成了吗？",
                        "related_ids": ["fact-1"],
                    },
                ]
            }
        )
    )
    assert result.memories[0].question is None
    assert result.memories[1].related_ids == ["fact-1"]
    assert result.memories[1].question == "搬家完成了吗？"


def test_parse_fenced_json():
    content = '```json\n{"memories":[{"text":"使用 手冲滤纸","topic":"devices"}]}\n```'
    result = parse_extraction(content)
    assert result.memories[0].text == "使用 手冲滤纸"


def test_parse_list_payload():
    result = parse_extraction('[{"text":"事实","topic":"misc"}]')
    assert len(result.memories) == 1


def test_parse_skips_empty_text():
    result = parse_extraction('{"memories":[{"text":"  "},{"text":"有效"}]}')
    assert [item.text for item in result.memories] == ["有效"]


def test_parse_rejects_non_json():
    with pytest.raises(ExtractorError):
        parse_extraction("模型拒绝回答")
