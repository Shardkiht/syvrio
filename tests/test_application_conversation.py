"""Application Conversation 用例：不启动 Textual。"""

from __future__ import annotations

import threading

import pytest

from syvrio.agent.runtime import AgentRuntime
from syvrio.agent.session import SessionStore
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
        self.calls = 0

    def chat(self, messages, tools, on_delta=None):
        self.calls += 1
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if on_delta is not None and turn.content:
            half = max(1, len(turn.content) // 2)
            on_delta(turn.content[:half])
            on_delta(turn.content[half:])
        return turn


class GatedModel(FakeModel):
    def __init__(self, turns):
        super().__init__(turns)
        self.entered = threading.Event()
        self.release = threading.Event()

    def chat(self, messages, tools, on_delta=None):
        self.entered.set()
        self.release.wait(5)
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


def test_turn_streams_in_order_and_binds_session(tmp_path):
    runtime, conv = build(tmp_path, [AssistantTurn(content="abcdef")])
    session = runtime.ensure_session()
    deltas: list[str] = []
    events: list[str] = []
    token = conv.begin_turn(None)
    result = conv.run_turn(
        "hello",
        TurnCallbacks(on_delta=deltas.append, on_event=events.append),
        token,
    )

    assert result.status == "ok"
    assert result.content == "abcdef"
    assert result.session_id == session.id
    assert deltas == ["abc", "def"]
    assert conv.ask_active is True  # UI 终态确认前仍占用请求槽
    assert conv.end_turn(result.turn_id) is True
    assert conv.ask_active is False
    assert runtime.sessions.load(session.id).user_texts() == ["hello"]


def test_fifo_queue_keeps_submit_order_and_session(tmp_path):
    runtime, conv = build(tmp_path, [])
    first = runtime.ensure_session()
    second = runtime.sessions.create()
    runtime.resume(second.id, close_current=False)
    conv.begin_turn(second.id)
    a = conv.submit_during_ask("first")
    b = conv.submit_during_ask("second")
    safe = conv.submit_during_ask("/status")
    undo = conv.submit_during_ask("/undo")

    assert a.kind == b.kind == "queued"
    assert [item.line for item in conv.queue] == ["first", "second"]
    assert {item.session_id for item in conv.queue} == {second.id}
    assert safe.kind == "command"
    assert undo.kind == "undo"
    assert conv.undo_requested is True
    assert [item.line for item in conv.queue] == ["first", "second"]
    assert conv.cancel_event is not None and conv.cancel_event.is_set()
    assert first.id != second.id


def test_generation_stays_bound_to_original_session_after_switch(tmp_path):
    runtime, conv = build(tmp_path, [])
    model = GatedModel([AssistantTurn(content="reply for A")])
    runtime.model = model
    a = runtime.ensure_session()
    runtime.sessions.append(a, {"type": "message", "id": "a0", "role": "user", "content": "old"})
    b = runtime.sessions.create()
    token = conv.begin_turn(a.id)

    outcome = {}
    thread = threading.Thread(
        target=lambda: outcome.setdefault(
            "result",
            conv.run_turn("question A", TurnCallbacks(), token, a.id),
        ),
        daemon=True,
    )
    thread.start()
    assert model.entered.wait(5)
    conv.switch(b.id)
    model.release.set()
    thread.join(10)

    assert outcome["result"].status == "ok"
    assert conv.current_id == b.id
    loaded_a = runtime.sessions.load(a.id)
    assert "question A" in loaded_a.user_texts()
    assert loaded_a.title() != ""
    assert [
        record["content"] for record in loaded_a.records if record.get("type") == "assistant"
    ] == ["reply for A"]
    assert runtime.sessions.load(b.id).user_texts() == []


def test_undo_targets_only_bound_user_message(tmp_path):
    runtime, conv = build(tmp_path, [])
    session = runtime.ensure_session()
    runtime.sessions.append(
        session, {"type": "message", "id": "u0", "role": "user", "content": "earlier"}
    )
    runtime.sessions.append(
        session, {"type": "message", "id": "u1", "role": "user", "content": "current"}
    )
    conv.begin_turn(session.id)
    conv.bind_user_message("u1")

    outcome = conv.perform_undo(session.id, "u1")

    assert outcome.restored == "current"
    assert runtime.sessions.load(session.id).user_texts() == ["earlier"]
    assert not conv.undo_requested


def test_undo_without_bound_message_keeps_earlier_turn(tmp_path):
    runtime, conv = build(tmp_path, [])
    session = runtime.ensure_session()
    runtime.sessions.append(
        session, {"type": "message", "id": "u0", "role": "user", "content": "earlier"}
    )
    conv.begin_turn(session.id)

    outcome = conv.perform_undo(session.id, None)

    assert outcome.restored is None
    assert runtime.sessions.load(session.id).user_texts() == ["earlier"]


def test_queue_pauses_after_successful_undo(tmp_path):
    runtime, conv = build(tmp_path, [])
    session = runtime.ensure_session()
    runtime.sessions.append(
        session, {"type": "message", "id": "u1", "role": "user", "content": "current"}
    )
    conv.begin_turn(session.id)
    conv.enqueue(session.id, "queued")
    conv.bind_user_message("u1")

    outcome = conv.perform_undo(session.id, "u1")

    assert outcome.restored == "current"
    assert outcome.paused is True
    assert conv.queue_paused_after_undo is True
    conv.resume_after_undo()
    assert conv.queue_paused_after_undo is False


def test_delete_guard_protects_current_and_queued_sessions(tmp_path):
    runtime, conv = build(tmp_path, [])
    current = runtime.ensure_session()
    other = runtime.sessions.create()

    allowed, reason = conv.delete_guard(current.id)
    assert not allowed and "current" in reason
    allowed, reason = conv.delete_guard(other.id)
    assert allowed

    conv.enqueue(other.id, "queued")
    allowed, reason = conv.delete_guard(other.id)
    assert not allowed and "queued" in reason


def test_session_switch_new_resume_and_rename(tmp_path):
    runtime, conv = build(tmp_path, [])
    first = runtime.ensure_session()
    conv.rename_current("First title")
    second = runtime.sessions.create()
    switched = conv.switch(second.id)
    assert switched.session.id == second.id
    conv.rename_session(second.id, "Second title")
    assert runtime.sessions.load(second.id).title() == "Second title"
    new_view = conv.switch(None)
    assert new_view.session is None and not conv.has_session
    assert first.id != second.id
