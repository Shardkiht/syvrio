from __future__ import annotations

from types import SimpleNamespace

import pytest

from syvrio.agent.recall import RecallQueryBuilder
from syvrio.infra.config import LLMConfig

pytestmark = pytest.mark.agent


def response(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def builder(**kwargs) -> RecallQueryBuilder:
    return RecallQueryBuilder(LLMConfig(), rewrite=False, **kwargs)


def test_followup_uses_recent_context():
    query = builder().build("那附近便宜吗", ["我在看滨江的房子"])

    assert "滨江" in query.text
    assert "那附近便宜吗" in query.text
    assert query.cue is False


def test_memory_cue_marks_cue_and_uses_context():
    query = builder().build("你还记得我的咖啡偏好吗", ["我之前喜欢清淡风味"])

    assert query.cue is True
    assert "清淡" in query.text


def test_plain_question_uses_current_text_only():
    query = builder().build("今天适合跑步吗", ["我在看滨江的房子"])

    assert query.text == "今天适合跑步吗"


def test_greeting_ignores_recent_context():
    query = builder().build("你好", ["我在看滨江的房子"])

    assert query.text == "你好"


def test_rewrite_uses_llm(monkeypatch):
    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "test-key")
    calls = []

    def fake_complete(config, messages, **kwargs):
        calls.append((messages, kwargs))
        return response("滨江 租房 房源 预算")

    query = RecallQueryBuilder(LLMConfig(), rewrite=True, completer=fake_complete).build(
        "那附近便宜吗", ["我在看滨江的房子"]
    )

    assert query.text == "滨江 租房 房源 预算"
    assert query.rewritten is True
    assert query.original == "那附近便宜吗"
    assert calls
    assert "同义词" in calls[0][0][0]["content"]


def test_rewrite_is_skipped_without_api_key():
    calls = []

    def should_not_run(*args, **kwargs):
        calls.append(True)
        return response("should not run")

    query = RecallQueryBuilder(LLMConfig(), rewrite=True, completer=should_not_run).build(
        "那附近便宜吗", ["我在看滨江的房子"]
    )

    assert calls == []
    assert "滨江" in query.text


def test_rewrite_falls_back_to_combined_text(monkeypatch):
    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "test-key")

    def fail(*args, **kwargs):
        raise RuntimeError("offline")

    query = RecallQueryBuilder(LLMConfig(), rewrite=True, completer=fail).build(
        "那附近便宜吗", ["我在看滨江的房子"]
    )

    assert "滨江" in query.text
    assert query.rewritten is False
    assert query.original == "那附近便宜吗"


def test_identity_question_can_rewrite_on_first_turn(monkeypatch):
    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "test-key")
    calls = []

    def complete(config, messages, **kwargs):
        calls.append(kwargs)
        return response("姓名 个人身份")

    query = RecallQueryBuilder(LLMConfig(provider="spark"), completer=complete).build(
        "你知道我是谁吗", []
    )
    assert query.text == "姓名 个人身份"
    assert query.original == "你知道我是谁吗"
    assert query.rewritten
    assert len(calls) == 1


def test_slow_rewrite_returns_original_before_request_finishes(monkeypatch):
    from threading import Event

    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "test-key")
    monkeypatch.setattr("syvrio.agent.recall.REWRITE_TIMEOUT", 0.1)
    release = Event()
    completed = Event()
    configs = []

    def slow(config, *args, **kwargs):
        configs.append(config)
        try:
            release.wait(3)
            return response("姓名")
        finally:
            completed.set()

    try:
        cfg = LLMConfig(provider="spark", timeout=60, num_retries=2, fallbacks=["other"])
        query = RecallQueryBuilder(cfg, completer=slow).build("我叫什么名字", [])
        assert query.text == query.original == "我叫什么名字"
        assert query.rewrite_timed_out and not completed.is_set()
        assert configs and configs[0].timeout == 0.1
        assert configs[0].num_retries == 0 and not configs[0].fallbacks
        assert cfg.timeout == 60 and cfg.num_retries == 2
    finally:
        release.set()
        completed.wait(2)


@pytest.mark.parametrize(
    "text,recent,expected",
    [
        ("你好", [], False),
        ("帮我解释 Python", [], False),
        ("帮我翻译这句话", [], False),
        ("搜索 https://example.com", [], False),
        ("HTTP 是什么", [], False),
        ("我喜欢什么饮料", [], True),
        ("你还记得上次的计划吗", [], True),
        ("那应该怎么选", ["我的预算有限"], True),
        ("那怎么用", ["解释 Python"], False),
    ],
)
def test_automatic_recall_gate(text, recent, expected):
    from syvrio.agent.recall import should_auto_recall

    assert should_auto_recall(text, recent) is expected
