from __future__ import annotations

import pytest

from syvrio.agent.context import ContextManager, records_to_messages
from syvrio.agent.session import SessionStore, new_message_id
from syvrio.infra.config import ChatConfig
from syvrio.infra.tokens import estimate_messages_tokens

pytestmark = pytest.mark.agent


class FakeSummarizer:
    def __init__(self):
        self.calls = []

    def summarize(self, previous, transcript, max_chars=1200):
        self.calls.append((previous, transcript))
        return f"摘要-{len(self.calls)}"


def build_session(store, repeat=3):
    session = store.create()
    for index in range(6):
        store.append(
            session,
            {
                "type": "message",
                "id": new_message_id(),
                "role": "user",
                "content": f"第 {index} 轮用户消息：" + "这是一个比较长的偏好描述。" * repeat,
            },
        )
        store.append(
            session,
            {
                "type": "assistant",
                "id": new_message_id(),
                "content": f"第 {index} 轮助手回复：" + "已记录。" * 5,
            },
        )
    return session


def test_prepare_under_budget_does_not_compact(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    store.append(session, {"type": "message", "id": "m1", "role": "user", "content": "你好"})
    manager = ContextManager(
        ChatConfig(context_window=32000, reserve_output_tokens=1000),
        FakeSummarizer(),
        store,
    )

    plan = manager.prepare(session, "系统提示", "继续")

    assert not plan.compacted
    assert plan.summary == ""
    assert plan.messages[-1] == {"role": "user", "content": "继续"}


@pytest.mark.parametrize("fail", [False, True])
def test_compaction_phases_follow_summary_persistence(tmp_path, fail):
    store = SessionStore(tmp_path)
    session = build_session(store, repeat=45)
    summarizer = FakeSummarizer()
    manager = ContextManager(
        ChatConfig(context_window=2500, keep_recent_tokens=350, reserve_output_tokens=300),
        summarizer,
        store,
    )
    phases = []

    def on_compaction(phase):
        phases.append((phase, bool(session.summary_text())))

    if fail:

        def fail_summary(*args):
            raise ValueError("synthetic summary failure")

        summarizer.summarize = fail_summary
        with pytest.raises(ValueError, match="synthetic summary failure"):
            manager.prepare(session, "system", "", force=True, on_compaction=on_compaction)
        assert phases == [("running", False), ("failed", False)]
    else:
        plan = manager.prepare(session, "system", "", force=True, on_compaction=on_compaction)
        assert plan.compacted
        assert phases == [("running", False), ("done", True)]


def test_tool_replay_drops_orphans_and_duplicates_and_closes_each_batch():
    records = [
        {"type": "tool", "tool_call_id": "orphan", "content": "orphan"},
        {"type": "assistant", "tool_calls": [{"id": "a", "name": "recall"}]},
        {"type": "tool", "tool_call_id": "a", "content": "original"},
        {"type": "tool", "tool_call_id": "a", "content": "duplicate"},
        {"type": "assistant", "tool_calls": [{"id": "b", "name": "recall"}]},
        {"type": "message", "role": "user", "content": "next turn"},
        {"type": "tool", "tool_call_id": "b", "content": "late"},
        {"type": "assistant", "tool_calls": [{"id": "c", "name": "recall"}]},
    ]
    messages = records_to_messages(records)
    assert [message["role"] for message in messages] == [
        "assistant",
        "tool",
        "assistant",
        "tool",
        "user",
        "assistant",
        "tool",
    ]
    results = [message for message in messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in results] == ["a", "b", "c"]
    assert results[0]["content"] == "original"
    assert all("was not recorded" in message["content"] for message in results[1:])


def test_compaction_requests_distillation_after_persisting_summary(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store)
    store.append(session, {"type": "message", "id": "current", "role": "user", "content": "当前问题"})
    requests = []

    def request(session_id, through):
        current = store.load(session_id)
        assert current.latest_summary()["distilled_ids"] == []
        assert through in {row["id"] for row in current.undistilled_messages()}
        requests.append(through)

    manager = ContextManager(
        ChatConfig(context_window=1000, reserve_output_tokens=100, keep_recent_tokens=100),
        FakeSummarizer(), store, request_distillation=request,
    )
    plan = manager.prepare(session, "系统提示", "当前问题", current_id="current", force=True)
    assert plan.compacted and plan.evicted_records > 0
    assert requests and "current" not in requests
    assert plan.summary.startswith("摘要-")
    assert sum(m["content"] == "当前问题" for m in plan.messages) == 1
    assert store.load(session.id).user_texts_after_distilled() == session.user_texts()


def test_resume_uses_persisted_summary(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store)
    manager = ContextManager(
        ChatConfig(context_window=800, reserve_output_tokens=100, keep_recent_tokens=100),
        FakeSummarizer(),
        store,
    )
    manager.prepare(session, "系统提示", "触发压缩", force=True)

    reloaded = store.load(session.id)
    plan = manager.prepare(reloaded, "系统提示", "下一轮")

    assert plan.summary
    assert any(message["content"].startswith("<session_summary>") for message in plan.messages)
    assert all("第 0 轮用户消息" not in str(message.get("content")) for message in plan.messages)


def test_undo_invalidates_later_summary_and_compacts_against_raw_positions(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    for index in range(1, 5):
        store.append(
            session,
            {
                "type": "message",
                "id": f"m{index}",
                "role": "user",
                "content": f"第 {index} 问" + "较长的对话内容" * 20,
            },
        )
        store.append(
            session,
            {
                "type": "assistant",
                "id": f"a{index}",
                "content": f"第 {index} 答" + "较长的回复内容" * 20,
            },
        )
    store.append(
        session,
        {"type": "summary", "id": "s1", "content": "旧摘要", "covered_records": 8},
    )
    store.append(session, {"type": "undo", "id": "u1", "message_id": "m3"})
    loaded = store.load(session.id)

    assert loaded.summary_text() == ""
    assert [text.split("较长", 1)[0] for text in loaded.user_texts()] == ["第 1 问", "第 2 问"]

    manager = ContextManager(
        ChatConfig(context_window=1000, reserve_output_tokens=100, keep_recent_tokens=100),
        FakeSummarizer(),
        store,
    )
    plan = manager.prepare(loaded, "系统提示", "下一问", force=True)

    assert loaded.covered_records() == 3  # meta + 第一条用户消息 + 第一条助手回复
    assert loaded.summary_text() == "摘要-1"
    contents = [str(message.get("content")) for message in plan.messages]
    assert any(content.startswith("第 2 问") for content in contents)
    assert all(not content.startswith("第 3 问") for content in contents)
    assert all("旧摘要" not in content for content in contents)


def test_no_compaction_with_only_current_message(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    store.append(session, {"type": "message", "id": "m1", "role": "user", "content": "唯一一句"})
    manager = ContextManager(ChatConfig(), FakeSummarizer(), store)

    plan = manager.prepare(session, "系统提示", "唯一一句", current_id="m1", force=True)

    assert not plan.compacted
    assert plan.summary == ""


def test_failed_enqueue_still_compacts_and_keeps_messages_for_recovery(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store, repeat=80)
    def fail(*args):
        raise OSError("synthetic write failure")
    manager = ContextManager(
        ChatConfig(context_window=4096, reserve_output_tokens=512, keep_recent_tokens=1500),
        FakeSummarizer(), store, request_distillation=fail,
    )
    plan = manager.prepare(session, "系统提示", "继续", force=True)
    assert plan.compacted
    assert estimate_messages_tokens(plan.messages) <= manager.input_budget
    assert any("recovery" in note for note in plan.events)
    loaded = store.load(session.id)
    assert loaded.latest_summary()["distilled_ids"] == []
    assert loaded.user_texts_after_distilled() == session.user_texts()


def test_failed_summary_never_enqueues_evicted_messages(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store)
    requests = []
    class Broken:
        def summarize(self, *args):
            raise TimeoutError("synthetic")
    manager = ContextManager(
        ChatConfig(context_window=1000, reserve_output_tokens=100, keep_recent_tokens=100),
        Broken(), store, request_distillation=lambda *args: requests.append(args),
    )
    with pytest.raises(TimeoutError):
        manager.prepare(session, "sys", "current", force=True)
    assert not requests
    assert not store.load(session.id).latest_summary()


def test_compaction_without_flush_does_not_acknowledge_messages(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store)
    manager = ContextManager(
        ChatConfig(context_window=1000, reserve_output_tokens=100, keep_recent_tokens=100),
        FakeSummarizer(),
        store,
    )
    plan = manager.prepare(session, "系统提示", "继续", force=True)
    assert plan.compacted
    assert store.load(session.id).user_texts_after_distilled() == session.user_texts()


def test_compaction_skips_messages_before_the_distillation_cursor(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store)
    store.append(session, {"type": "distilled", "up_to": session.last_user_message_id()})
    requests = []
    manager = ContextManager(
        ChatConfig(context_window=1000, reserve_output_tokens=100, keep_recent_tokens=100),
        FakeSummarizer(), store, request_distillation=lambda *args: requests.append(args),
    )
    assert manager.prepare(session, "系统提示", "继续", force=True).compacted
    assert not requests and not store.load(session.id).needs_consolidation()
