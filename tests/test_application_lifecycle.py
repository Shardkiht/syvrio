"""请求生命周期：Worker 完成与 UI 终态确认分离、过期回调、异常与取消。"""
from __future__ import annotations

import threading

import pytest

from syvrio.agent.runtime import AgentRuntime
from syvrio.agent.session import SessionStore
from syvrio.application import conversation as conversation_module
from syvrio.application.contracts import TurnCallbacks
from syvrio.application.conversation import ConversationUseCases
from syvrio.infra.config import ChatConfig, Config, LLMConfig, RetrievalConfig
from syvrio.llm import AssistantTurn
from syvrio.memory.extractor import StaticExtractor
from syvrio.memory.models import ExtractionResult
from syvrio.memory.service import MemoryService

pytestmark = pytest.mark.agent


class FakeModel:
    def __init__(self, turns):
        self.turns = list(turns)

    def chat(self, messages, tools, on_delta=None):
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if on_delta is not None and turn.content:
            on_delta(turn.content)
        return turn


class GatedModel(FakeModel):
    def __init__(self, turns):
        super().__init__(turns)
        self.entered = threading.Event()
        self.release = threading.Event()

    def chat(self, messages, tools, on_delta=None):
        self.entered.set()
        assert self.release.wait(5)
        return super().chat(messages, tools, on_delta)


class FakeSummarizer:
    def summarize(self, previous, transcript, max_chars=1200):
        return "summary"


def build(tmp_path, turns):
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        chat=ChatConfig(context_window=32000, auto_recall=0, recall_rewrite=False),
        retrieval=RetrievalConfig(method="keyword"),
    )
    service = MemoryService(config, extractor=StaticExtractor(ExtractionResult(memories=[])))
    service.init_vault()
    runtime = AgentRuntime(
        service,
        FakeModel(turns),
        SessionStore(config.vault),
        summarizer=FakeSummarizer(),
        config=config.chat,
    )
    runtime.start()
    return runtime, ConversationUseCases(runtime, runtime.sessions)


def test_worker_completion_waits_for_ui_ack_before_accepting_next_turn(tmp_path):
    runtime, conv = build(tmp_path, [AssistantTurn(content="reply A")])
    runtime.ensure_session()
    token = conv.begin_turn()

    result = conv.run_turn("message A", TurnCallbacks(), token)

    # Worker 已返回，但 UI 尚未确认终态：仍视为忙碌，新消息只能排队。
    assert result.status == "ok"
    assert conv.ask_active is True
    assert conv.active_turn_id == result.turn_id
    submission = conv.submit_during_ask("message B")
    assert submission.kind == "queued"
    assert conv.queue_snapshot().count == 1

    assert conv.end_turn(result.turn_id) is True
    assert conv.ask_active is False
    queued = conv.take_next()
    assert queued is not None and queued.line == "message B"


def test_stale_finish_callback_cannot_clear_newer_turn(tmp_path):
    runtime, conv = build(tmp_path, [AssistantTurn(content="A"), AssistantTurn(content="B")])
    runtime.ensure_session()
    first_token = conv.begin_turn()
    first = conv.run_turn("first", TurnCallbacks(), first_token)
    assert conv.end_turn(first.turn_id)

    second_token = conv.begin_turn()
    second = conv.run_turn("second", TurnCallbacks(), second_token)
    assert conv.ask_active and conv.cancel_event is second_token

    # 迟到的第一次完成回调不得释放第二次请求。
    assert conv.end_turn(first.turn_id) is False
    assert conv.ask_active is True
    assert conv.cancel_event is second_token
    assert conv.active_turn_id == second.turn_id

    assert conv.end_turn(second.turn_id) is True
    assert conv.end_turn(second.turn_id) is False


def test_session_load_failure_returns_turn_result_and_releases_on_ack(tmp_path, monkeypatch):
    runtime, conv = build(tmp_path, [])
    logged: list[tuple] = []
    monkeypatch.setattr(conversation_module, "record_event", lambda *args, **kwargs: logged.append(args))
    token = conv.begin_turn("missing-session")

    def fail_load(session_id):
        raise OSError("vault unavailable")

    monkeypatch.setattr(runtime.sessions, "load", fail_load)
    result = conv.run_turn("message", TurnCallbacks(), token, "missing-session")

    assert result.status == "failed"
    assert "vault unavailable" in result.error
    assert result.session_id == "missing-session"
    assert result.user_message_id is None
    assert result.exception is not None
    assert logged and logged[0][0] == "chat.failed"
    assert conv.ask_active is True  # 仍等 UI 终态确认
    assert conv.end_turn(result.turn_id) is True
    assert conv.ask_active is False


@pytest.mark.parametrize("model_fails", [False, True])
def test_switch_cancel_and_undo_are_bound_to_original_session(tmp_path, monkeypatch, model_fails):
    from queue import Queue
    from syvrio.infra import diagnostics

    runtime, conv = build(tmp_path, [])
    monkeypatch.setattr(diagnostics, "_queue", Queue())
    response = RuntimeError("Synthetic model failure") if model_fails else AssistantTurn(content="reply A")
    model = GatedModel([response])
    runtime.model = model
    a = runtime.ensure_session()
    runtime.sessions.append(a, {"type": "message", "id": "old", "role": "user", "content": "earlier"})
    b = runtime.sessions.create()
    token = conv.begin_turn(a.id)
    outcome = {}
    thread = threading.Thread(
        target=lambda: outcome.setdefault("result", conv.run_turn("question A", TurnCallbacks(), token, a.id)),
        daemon=True,
    )
    thread.start()
    assert model.entered.wait(5)

    conv.switch(b.id)
    assert conv.request_cancel() is True
    assert conv.request_cancel() is True  # 连续取消保持幂等
    submission = conv.request_undo()
    assert submission.kind == "undo"
    model.release.set()
    thread.join(10)

    result = outcome["result"]
    assert result.status == "undo"
    assert result.undo.target_session_id == a.id
    assert runtime.sessions.load(b.id).user_texts() == []
    assert runtime.sessions.load(a.id).user_texts() == ["earlier"]
    assert conv.end_turn(result.turn_id) is True
    assert conv.request_cancel() is False


def test_fast_submissions_keep_order_and_single_active_turn(tmp_path):
    runtime, conv = build(tmp_path, [AssistantTurn(content=f"reply {i}") for i in range(3)])
    runtime.ensure_session()
    token = conv.begin_turn()
    first_turn = conv.active_turn_id
    for line in ("m1", "m2", "m3"):
        assert conv.submit_during_ask(line).kind == "queued"
    assert [item.line for item in conv.queue_snapshot().items] == ["m1", "m2", "m3"]

    assert conv.end_turn(first_turn) is True
    for expected in ("m1", "m2", "m3"):
        item = conv.take_next()
        assert item is not None and item.line == expected
        turn_token = conv.begin_turn(item.session_id)
        result = conv.run_turn(item.line, TurnCallbacks(), turn_token, item.session_id)
        assert result.status == "ok"
        assert conv.ask_active is True
        assert conv.end_turn(result.turn_id) is True
    assert conv.ask_active is False
    assert conv.queue_snapshot().count == 0

def test_end_turn_requires_matching_turn_id(tmp_path):
    runtime, conv = build(tmp_path, [AssistantTurn(content="reply")])
    runtime.ensure_session()
    conv.begin_turn()
    turn_id = conv.active_turn_id

    assert conv.end_turn(None) is False
    assert conv.ask_active is True and conv.active_turn_id == turn_id
    assert conv.end_turn(turn_id + 1) is False
    assert conv.ask_active is True and conv.active_turn_id == turn_id
    assert conv.end_turn(turn_id) is True
    assert conv.ask_active is False and conv.active_turn_id is None


def test_run_turn_without_active_turn_does_not_fabricate_id(tmp_path):
    runtime, conv = build(tmp_path, [])
    runtime.ensure_session()
    result = conv.run_turn("message", TurnCallbacks(), threading.Event())

    assert result.status == "failed"
    assert result.turn_id is None
    assert conv.ask_active is False
    assert conv.active_turn_id is None
