"""Application Conversation 用例：不启动 Textual。"""

from __future__ import annotations

import asyncio
import threading

import pytest

from syvrio.agent.context import ContextManager
from syvrio.agent.runtime import AgentRuntime
from syvrio.agent.session import SessionStore
from syvrio.application.channels import ConversationDirectory
from syvrio.application.contracts import TurnCallbacks
from syvrio.application.conversation import ConversationUseCases
from syvrio.application.service import ApplicationService
from syvrio.errors import SessionProtectedError
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


@pytest.mark.parametrize("fail_queued_start", [False, True])
def test_application_owns_admitted_turns_across_views(tmp_path, monkeypatch, fail_queued_start):
    runtime, _ = build(tmp_path, [])
    runtime.model = GatedModel([AssistantTurn(content="answer") for _ in range(3)])
    owner = ApplicationService(runtime, runtime.service)
    scheduling = owner.conversation.scheduling

    def new_channel():
        sibling = AgentRuntime(runtime.service, runtime.model, runtime.sessions)
        sibling.start()
        return ApplicationService(
            sibling,
            runtime.service,
            registry=scheduling.registry,
            settings_lock=scheduling.settings_lock,
        )

    directory = ConversationDirectory(owner, new_channel)
    channel, first = directory.submit("alice", "first", None)
    sid = first.session_id
    try:
        assert runtime.model.entered.wait(5)
        assert directory.select("bob", sid).id == sid
        assert directory.channel("bob") is channel is owner
        _, second = directory.submit("bob", "second", sid)
        _, third = directory.submit("alice", "third", sid)
        assert owner.conversation.active_turn is first
        assert scheduling.registry.get(second.turn_id, sid) is second
        assert owner.conversation.queue == [second, third]
        assert [item.line for item in owner.conversation.queue_snapshot().items] == [
            "second",
            "third",
        ]
        assert not scheduling.cancel(second.turn_id)
        directory.disconnect("alice")
        assert directory.queue_state("bob")["active"]["turn_id"] == first.turn_id
        if fail_queued_start:
            start = threading.Thread.start

            def fail_second(thread):
                if thread.name == f"syvrio-turn-{second.turn_id}":
                    raise RuntimeError("synthetic startup failure")
                start(thread)

            monkeypatch.setattr(threading.Thread, "start", fail_second)
    finally:
        runtime.model.release.set()
    assert third.finished.wait(5)
    assert scheduling.activity() == (False, None)
    assert owner.conversation.queue_snapshot().count == 0

    async def replay(turn):
        return [event async for event in turn.events() if event is not None]

    events = asyncio.run(replay(second))
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert events[-1].type == ("failed" if fail_queued_start else "done")
    expected = ["first", "third"] if fail_queued_start else ["first", "second", "third"]
    assert runtime.sessions.load(sid).user_texts() == expected


def test_turn_settings_are_fixed_before_the_worker_starts(tmp_path, monkeypatch):
    runtime, conv = build(tmp_path, [AssistantTurn(content="original model")])
    budgets = []
    prepare = ContextManager.prepare

    def record_budget(manager, *args, **kwargs):
        budgets.append(manager.config.context_window)
        return prepare(manager, *args, **kwargs)

    monkeypatch.setattr(ContextManager, "prepare", record_budget)
    first = conv.begin_turn()
    runtime.model = FakeModel([AssistantTurn(content="new model")])
    runtime.config.context_window = 16000
    original = conv.run_turn("first", TurnCallbacks(), first)
    assert original.content == "original model"
    assert conv.end_turn(original.turn_id)

    second = conv.begin_turn()
    updated = conv.run_turn("second", TurnCallbacks(), second)
    assert updated.content == "new model"
    assert conv.end_turn(updated.turn_id)
    assert budgets == [32000, 16000]


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
    assert not allowed and "Archive" in reason
    assert conv.archive_guard(current.id) == (True, None)
    allowed, reason = conv.delete_guard(other.id)
    assert allowed

    conv.enqueue(other.id, "queued")
    allowed, reason = conv.delete_guard(other.id)
    assert not allowed and "queued" in reason


@pytest.mark.parametrize("status", ["active", "deprecated"])
def test_refuses_session_delete_if_memory_was_written_during_confirmation(tmp_path, status):
    runtime, conv = build(tmp_path, [])
    target = runtime.sessions.create()
    runtime.sessions.append(
        target,
        {"type": "message", "id": "m-source", "role": "user", "content": "The original source"},
    )
    assert conv.delete_guard(target.id) == (True, None)
    conv.archive_session(target.id)
    archived = runtime.sessions.load(target.id, archived=True).path
    original = archived.read_bytes()
    with runtime.service.write_lock:
        fact = runtime.service.store.append(
            "personal", "Saved fact", extra={"session_id": target.id, "message_id": "m-source"}
        )
        runtime.service.store.set_status(fact.id, status)
    with pytest.raises(SessionProtectedError, match=fact.id):
        conv.delete_session(target.id)
    assert archived.read_bytes() == original


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
