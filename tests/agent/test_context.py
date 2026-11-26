from __future__ import annotations

import pytest

from syvrio.agent.context import SUMMARY_TEMPLATE, ContextManager, records_to_messages
from syvrio.agent.summarizer import LLMSummarizer
from syvrio.infra.config import ChatConfig, LLMConfig
from syvrio.infra.llm.tokens import estimate_messages_tokens
from syvrio.infra.session import SessionStore, new_message_id, transcript

pytestmark = pytest.mark.agent


@pytest.mark.parametrize("summary", ["", "此前已保存的摘要"])
def test_unchanged_request_is_counted_once(tmp_path, monkeypatch, summary):
    store = SessionStore(tmp_path / "vault")
    session = build_session(store, repeat=1)
    manager = ContextManager(ChatConfig(context_window=32768), FakeSummarizer(), store)
    if summary:
        store.append(
            session,
            {
                "type": "summary",
                "content": summary,
                "covered_records": 1,
                "records": 1,
                "distilled_ids": [],
                "tokens": 8,
            },
        )
    counted = []
    count = manager._request_tokens

    def capture(messages, **options):
        counted.append(messages)
        return count(messages, **options)

    monkeypatch.setattr(manager, "_request_tokens", capture)
    plan = manager.prepare(session, "系统", "继续", tool_tokens=17, memory_context="参考")
    assert len(counted) == 1
    assert plan.estimated_tokens == 17 + count(plan.messages)


@pytest.mark.parametrize("offline", (False, True))
def test_long_text_token_counts_match_the_serial_wire_format(monkeypatch, offline):
    from concurrent.futures import ThreadPoolExecutor

    from syvrio.infra.llm import tokens

    if offline:
        monkeypatch.setattr(tokens, "_ENCODING", False)
    encoding = tokens._encoding()
    calls = []
    dispatch = tokens._TOKEN_POOL.map

    def observe(fn, batches):
        parts = list(batches)
        calls.append(len(parts))
        return dispatch(fn, parts)

    monkeypatch.setattr(tokens._TOKEN_POOL, "map", observe)
    special = "<|endoftext|>你好🌙e\u0301\ud800" * 100
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                {"type": "text", "text": special},
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "想一想",
            "tool_calls": [{"function": {"name": "search", "arguments": '{"q":"你好"}'}}],
        },
        {"role": "tool", "content": "结果"},
    ]
    parts = (special, "想一想", "search", '{"q":"你好"}', "结果")
    single = (
        4 * len(messages)
        + 1024
        + sum(
            len(encoding.encode(part, disallowed_special=()))
            if encoding
            else tokens.estimate_tokens(part)
            for part in parts
        )
    )
    assert tokens.estimate_messages_tokens(messages) == single
    assert calls == []
    with ThreadPoolExecutor(max_workers=3) as readers:
        results = readers.map(tokens.estimate_messages_tokens, [messages * 40] * 3)
        assert list(results) == [single * 40] * 3
    assert calls == ([4] * 3 if encoding else [])


class FakeSummarizer:
    def __init__(self):
        self.calls = []

    def summarize(self, previous, transcript, max_chars=1200):
        self.calls.append((previous, transcript))
        return f"摘要-{len(self.calls)}"


def build_session(store, repeat=30):
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


@pytest.mark.parametrize("has_current_input", [False, True], ids=["manual", "before-reply"])
def test_trigger_boundary_compacts_completed_history_once(tmp_path, has_current_input):
    store = SessionStore(tmp_path)
    session = store.create()
    store.append(
        session, {"type": "message", "role": "user", "id": "old", "content": "已完成的问题"}
    )
    store.append(session, {"type": "assistant", "id": "reply", "content": "已完成的答案"})
    history = transcript(session.records_after_summary())
    text = "尚未回答的问题" if has_current_input else ""
    current_id = "now" if has_current_input else None
    if has_current_input:
        store.append(
            session, {"type": "message", "role": "user", "id": current_id, "content": text}
        )
    summarizer = FakeSummarizer()
    manager = ContextManager(
        ChatConfig(context_window=1200, reserve_output_tokens=200, compact_threshold=0.8),
        summarizer,
        store,
    )
    tools_at_threshold = manager.trigger_tokens - manager.estimate_usage(session, "system")
    below = manager.prepare(session, "system", text, current_id, tool_tokens=tools_at_threshold - 1)
    assert below.estimated_tokens == manager.trigger_tokens - 1 and not below.compacted
    assert not summarizer.calls and session.latest_summary() is None

    plan = manager.prepare(session, "system", text, current_id, tool_tokens=tools_at_threshold)
    assert plan.compacted and plan.evicted_records == 2 and summarizer.calls == [("", history)]
    restored = store.load(session.id)
    assert [record["id"] for record in restored.records_after_summary()] == (
        ["now"] if has_current_input else []
    )
    again = manager.prepare(restored, "system", text, current_id, tool_tokens=tools_at_threshold)
    assert not again.compacted and summarizer.calls == [("", history)]
    assert again.messages == plan.messages
    assert [m["content"] for m in plan.messages if m["role"] == "user"] == (
        [text] if has_current_input else []
    )


def test_full_summary_includes_recent_tool_exchange_and_preserves_current_input(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store, repeat=30)
    for record in [
        {"type": "message", "role": "user", "id": "previous", "content": "查询"},
        {"type": "assistant", "tool_calls": [{"id": "c1", "name": "recall"}]},
        {"type": "tool", "name": "recall", "tool_call_id": "c1", "content": "已找到"},
        {"type": "assistant", "content": "查询完成"},
        {"type": "message", "role": "user", "id": "now", "content": "继续"},
    ]:
        store.append(session, record)
    summarizer = FakeSummarizer()
    manager = ContextManager(ChatConfig(context_window=2000), summarizer, store)
    history = session.records_after_summary()[:-1]
    persisted = records_to_messages(history)

    plan = manager.prepare(session, "系统", "继续", current_id="now", memory_context="参考")

    assert plan.compacted and plan.evicted_records == len(history)
    assert summarizer.calls == [("", transcript(history))]
    assert "调用工具：recall" in summarizer.calls[0][1] and "已找到" in summarizer.calls[0][1]
    assert plan.estimated_tokens == estimate_messages_tokens(plan.messages) <= manager.input_budget
    assert plan.messages == [
        {"role": "system", "content": "系统"},
        {"role": "system", "content": SUMMARY_TEMPLATE.format("摘要-1")},
        {"role": "user", "content": "<long_term_memory>\n参考\n</long_term_memory>"},
        {"role": "user", "content": "继续"},
    ]
    restored = store.load(session.id)
    assert [record["id"] for record in restored.records_after_summary()] == ["now"]
    assert records_to_messages(restored.active_records())[:-1] == persisted


def test_small_window_fits_long_summary_without_overwriting_it(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    store.append(session, {"type": "message", "role": "user", "id": "old", "content": "问题" * 900})
    store.append(session, {"type": "assistant", "content": "答复" * 900})
    store.append(session, {"type": "message", "role": "user", "id": "now", "content": "继续"})
    summary = "历史摘要。" * 200

    class LongSummarizer:
        def summarize(self, previous, transcript, max_chars):
            return summary

    manager = ContextManager(ChatConfig(context_window=2000), LongSummarizer(), store)
    plan = manager.prepare(session, "系统", "继续", current_id="now")

    assert plan.compacted and plan.evicted_records == 2
    assert plan.estimated_tokens <= manager.input_budget
    assert plan.messages[1]["content"].startswith("<session_summary>\n历史摘要")
    assert summary not in plan.messages[1]["content"]
    assert plan.messages[-1] == {"role": "user", "content": "继续"}
    assert store.load(session.id).summary_text() == summary
    previous_summary = manager.prepare(store.load(session.id), "系统", "继续", current_id="now")
    assert not previous_summary.compacted
    assert previous_summary.messages == plan.messages


def test_small_window_bounds_recalled_text_without_splitting_its_wrapper(tmp_path):
    store = SessionStore(tmp_path)
    manager = ContextManager(ChatConfig(context_window=2000), FakeSummarizer(), store)
    plan = manager.prepare(store.create(), "系统", "问题", memory_context="虚构的参考材料" * 900)
    assert plan.estimated_tokens <= manager.input_budget
    assert plan.messages[1]["role"] == "user"
    assert plan.messages[1]["content"].startswith("<long_term_memory>\n虚构")
    assert plan.messages[1]["content"].endswith("\n</long_term_memory>")
    assert plan.messages[-1] == {"role": "user", "content": "问题"}


def test_compaction_summarizes_inserted_input_with_its_root_turn(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store)
    latest = session.last_user_message_id()
    store.append(
        session,
        {
            "type": "message",
            "id": "extra",
            "role": "user",
            "turn_root": latest,
            "content": "补充资料" * 180,
        },
    )
    store.append(session, {"type": "assistant", "content": "补充资料已处理"})
    summarizer = FakeSummarizer()
    manager = ContextManager(ChatConfig(context_window=2000), summarizer, store)
    expected = transcript(session.records_after_summary())
    plan = manager.prepare(session, "系统", "")
    assert plan.compacted and not session.records_after_summary()
    assert summarizer.calls == [("", expected)]
    assert expected.index("第 5 轮用户消息") < expected.index("用户：补充资料")
    assert expected.endswith("助手：补充资料已处理")


def test_followup_summary_keeps_current_attachments_and_inserted_input(tmp_path):
    from syvrio.agent.context import records_to_messages

    store = SessionStore(tmp_path)
    session = store.create()
    pending = []
    manager = ContextManager(
        ChatConfig(context_window=4096, reserve_output_tokens=512),
        FakeSummarizer(),
        store,
        lambda *args: pending.append(args),
    )
    question = "Read the source in the image."
    image = [
        {"type": "text", "text": question},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]
    store.append(session, {"type": "message", "role": "user", "id": "root", "content": question})
    plan = manager.prepare(session, "system", image, "root")
    records = [
        {"type": "assistant", "tool_calls": [{"id": "c1", "name": "search", "arguments": {}}]},
        {"type": "tool", "tool_call_id": "c1", "name": "search", "content": " ref" * 4096},
        {
            "type": "message",
            "role": "user",
            "id": "extra",
            "turn_root": "root",
            "content": "Include dates.",
        },
    ]
    for record in records:
        store.append(session, record)
    messages = records_to_messages(records)
    plan.messages.extend(messages)
    result = manager.prepare(
        session, "system", image, "root", active_plan=plan, additional_inputs=[messages[-1]]
    )
    assert result.compacted and result.estimated_tokens <= manager.input_budget
    assert result.messages[-2:] == [{"role": "user", "content": image}, messages[-1]]
    assert not pending
    assert [r["id"] for r in session.undistilled_messages()] == ["root", "extra"]


def test_local_summary_uses_the_same_budget_for_tool_records():
    from syvrio.agent.summarizer import fallback_summary

    source = "工具 search：" + "reference " * 30 + "https://example.org/source"
    assert fallback_summary("", source, 500) == source


def test_summary_requests_cover_full_evicted_history_within_turn_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace

    store = SessionStore(tmp_path)
    session = store.create()
    # 同一长行跨过了词元边界，也超过了原来的 16,000 字符截断点。
    old = "开始。" + "abcdef" * 3000 + "。中间及末尾。"
    store.append(session, {"type": "message", "role": "user", "id": "old", "content": old})
    store.append(session, {"type": "assistant", "content": "答复完整。"})
    source = transcript(session.records_after_summary())
    store.append(session, {"type": "message", "role": "user", "id": "new", "content": "继续"})
    chunks = []
    sizes = []
    manager = ContextManager(ChatConfig(context_window=2500), LLMSummarizer(LLMConfig()), store)
    monkeypatch.setattr(LLMConfig, "requires_api_key", lambda self: False)

    def complete(config, messages, **kwargs):
        sizes.append((estimate_messages_tokens(messages), kwargs["max_tokens"]))
        chunk = (
            messages[1]["content"].split("需要压缩的对话：\n", 1)[1].rsplit("\n\n输出不超过", 1)[0]
        )
        chunks.append(chunk)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=f"合成摘要 {len(chunks)}"))]
        )

    monkeypatch.setattr("syvrio.agent.summarizer.complete", complete)
    plan = manager.prepare(session, "系统", "继续", current_id="new")
    assert 1 < len(chunks) <= 8 and "".join(chunks) == source
    assert all(used <= manager.input_budget for used, _ in sizes)
    assert all(0 < output <= manager.config.context_window - used for used, output in sizes)
    assert plan.compacted and store.load(session.id).summary_text() == f"合成摘要 {len(chunks)}"
    assert [r["id"] for r in session.records_after_summary()] == ["new"]
    assert old in store.load(session.id).user_texts()


@pytest.mark.parametrize("failure", ["error", "empty", "tiny"])
def test_summary_fallback_keeps_previous_and_new_tail(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace

    store = SessionStore(tmp_path)
    session = store.create()
    store.append(
        session,
        {
            "type": "summary",
            "content": "已有背景。" * 200,
            "covered_records": 1,
            "distilled_ids": [],
            "id": "s0",
        },
    )
    text = "首条线索。" + "历史主体。" * 1100 + "可核对的尾部。"
    store.append(session, {"type": "message", "role": "user", "id": "m1", "content": text})
    manager = ContextManager(
        ChatConfig(context_window=30 if failure == "tiny" else 2000),
        LLMSummarizer(LLMConfig()),
        store,
    )
    calls = []
    monkeypatch.setattr(LLMConfig, "requires_api_key", lambda self: False)

    def fail(config, messages, **kwargs):
        calls.append(estimate_messages_tokens(messages))
        if failure == "error":
            raise TimeoutError("synthetic timeout")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])

    monkeypatch.setattr("syvrio.agent.summarizer.complete", fail)
    plan = manager.prepare(session, "sys", "next")
    assert len(calls) == (0 if failure == "tiny" else 1)
    assert all(used <= manager.input_budget for used in calls)
    assert plan.compacted and len(plan.summary) <= 1200
    assert (
        "已有背景" in plan.summary
        and "历史主体" in plan.summary
        and "可核对的尾部。" in plan.summary
    )
    assert store.load(session.id).summary_text() == plan.summary
    assert store.load(session.id).user_texts() == [text]


@pytest.mark.parametrize("offline", [False, True])
def test_summary_request_limit_preserves_recent_and_remaining_material(monkeypatch, offline):
    from types import SimpleNamespace

    sizes = []
    monkeypatch.setattr(LLMConfig, "requires_api_key", lambda self: True)
    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "" if offline else "synthetic")

    def complete(config, messages, **kwargs):
        sizes.append(estimate_messages_tokens(messages))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="已处理的合成内容"))]
        )

    monkeypatch.setattr("syvrio.agent.summarizer.complete", complete)
    result = LLMSummarizer(LLMConfig()).summarize(
        "此前合成背景",
        "用户：合成开头。" + "持续的历史记录。" * 600 + "合成尾部。",
        chat=ChatConfig(context_window=2000),
        input_budget=512,
    )
    assert len(sizes) == (0 if offline else 8)
    assert all(size <= 512 for size in sizes)
    assert "此前合成背景" in result if offline else "已处理的合成内容" in result
    assert result.startswith("本地节选") and result.endswith("合成尾部。")


@pytest.mark.parametrize("fail", [False, True])
def test_compaction_phases_follow_summary_persistence(tmp_path, fail):
    store = SessionStore(tmp_path)
    session = build_session(store, repeat=45)
    summarizer = FakeSummarizer()
    manager = ContextManager(
        ChatConfig(context_window=2500, reserve_output_tokens=300),
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
            manager.prepare(session, "system", "", on_compaction=on_compaction)
        assert phases == [("running", False), ("failed", False)]
    else:
        plan = manager.prepare(session, "system", "", on_compaction=on_compaction)
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
    store.append(
        session, {"type": "message", "id": "current", "role": "user", "content": "当前问题"}
    )
    requests = []

    def request(session_id, through):
        current = store.load(session_id)
        assert current.latest_summary()["distilled_ids"] == []
        assert through in {row["id"] for row in current.undistilled_messages()}
        requests.append(through)

    manager = ContextManager(
        ChatConfig(context_window=1000, reserve_output_tokens=100),
        FakeSummarizer(),
        store,
        request_distillation=request,
    )
    plan = manager.prepare(session, "系统提示", "当前问题", current_id="current")
    assert plan.compacted and plan.evicted_records > 0
    assert requests and "current" not in requests
    assert plan.summary.startswith("摘要-")
    assert sum(m["content"] == "当前问题" for m in plan.messages) == 1
    assert store.load(session.id).user_texts_after_distilled() == session.user_texts()


def test_resume_uses_persisted_summary(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store)
    manager = ContextManager(
        ChatConfig(context_window=800, reserve_output_tokens=100),
        FakeSummarizer(),
        store,
    )
    manager.prepare(session, "系统提示", "触发压缩")

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
        {
            "type": "summary",
            "id": "s1",
            "content": "此前摘要",
            "covered_records": 8,
            "distilled_ids": [],
        },
    )
    store.append(session, {"type": "undo", "id": "u1", "message_id": "m3"})
    loaded = store.load(session.id)

    assert loaded.summary_text() == ""
    assert [text.split("较长", 1)[0] for text in loaded.user_texts()] == ["第 1 问", "第 2 问"]

    store.append(loaded, {"type": "message", "id": "m5", "role": "user", "content": "撤销后新问"})
    store.append(loaded, {"type": "assistant", "id": "a5", "content": "撤销后新答"})
    coverage = len(loaded.records)  # 包括已撤回的记录和已失效的摘要。
    summarizer = FakeSummarizer()
    manager = ContextManager(
        ChatConfig(context_window=1000, reserve_output_tokens=100, compact_threshold=0.25),
        summarizer,
        store,
    )
    plan = manager.prepare(loaded, "系统提示", "下一问")

    assert plan.compacted and loaded.covered_records() == coverage
    assert loaded.summary_text() == "摘要-1"
    previous, content = summarizer.calls[0]
    assert previous == "" and "旧摘要" not in content
    assert all(text in content for text in ("第 1 问", "第 2 问", "撤销后新问", "撤销后新答"))
    assert all(text not in content for text in ("第 3 问", "第 4 问"))
    assert not loaded.records_after_summary()
    assert plan.messages[-1] == {"role": "user", "content": "下一问"}


def test_no_compaction_with_only_current_message(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    store.append(session, {"type": "message", "id": "m1", "role": "user", "content": "唯一一句"})
    manager = ContextManager(ChatConfig(context_window=1000), FakeSummarizer(), store)

    plan = manager.prepare(
        session, "系统提示", "唯一一句", current_id="m1", tool_tokens=manager.trigger_tokens
    )

    assert not plan.compacted and plan.estimated_tokens >= manager.trigger_tokens
    assert plan.summary == ""
    assert [r["id"] for r in session.records_after_summary()] == ["m1"]


def test_failed_enqueue_still_compacts_and_keeps_messages_for_recovery(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store, repeat=80)

    def fail(*args):
        raise OSError("synthetic write failure")

    manager = ContextManager(
        ChatConfig(context_window=4096, reserve_output_tokens=512),
        FakeSummarizer(),
        store,
        request_distillation=fail,
    )
    plan = manager.prepare(session, "系统提示", "继续")
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
        ChatConfig(context_window=1000, reserve_output_tokens=100),
        Broken(),
        store,
        request_distillation=lambda *args: requests.append(args),
    )
    with pytest.raises(TimeoutError):
        manager.prepare(session, "sys", "current")
    assert not requests
    assert not store.load(session.id).latest_summary()


def test_compaction_without_flush_does_not_acknowledge_messages(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store)
    manager = ContextManager(
        ChatConfig(context_window=1000, reserve_output_tokens=100),
        FakeSummarizer(),
        store,
    )
    plan = manager.prepare(session, "系统提示", "继续")
    assert plan.compacted
    assert store.load(session.id).user_texts_after_distilled() == session.user_texts()


def test_compaction_skips_messages_before_the_distillation_cursor(tmp_path):
    store = SessionStore(tmp_path)
    session = build_session(store)
    store.append(session, {"type": "distilled", "up_to": session.last_user_message_id()})
    requests = []
    manager = ContextManager(
        ChatConfig(context_window=1000, reserve_output_tokens=100),
        FakeSummarizer(),
        store,
        request_distillation=lambda *args: requests.append(args),
    )
    assert manager.prepare(session, "系统提示", "继续").compacted
    assert not requests and not store.load(session.id).needs_consolidation()
