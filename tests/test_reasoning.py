"""思考链路（reasoning）提取、落盘与隐私测试。

foundation 与 Evo 的差异（有意的裁剪，见 FOUNDATION-NOTES Phase 7）：
- reasoning_source 恒为空（Evo 的路由验证属 C 类不移植）；无论新旧记录、source 是否
  非空，推理原文都绝不回放进发给模型的请求消息。
- 推理文本只落 Runtime 附件目录（runtime/session-reasoning/），Vault JSONL 仅存
  reasoning_local 标记；附件缺失或损坏时正文仍能加载。
- 不移植 test_reasoning_replay_respects_route_and_turn（依赖 _reasoning_route）与
  test_installed_adapter_keeps_reasoning_in_request_body（依赖 Evo 安装的
  litellm 适配器子类）。
"""

from __future__ import annotations

import json
from threading import Event
from types import SimpleNamespace

import pytest
from test_agent import FakeSummarizer, build_runtime

from syvrio.agent.context import ContextManager, records_to_messages
from syvrio.agent.session import SessionStore
from syvrio.infra.config import ChatConfig, LLMConfig
from syvrio.llm import client

pytestmark = pytest.mark.agent
def _model(monkeypatch, provider="deepseek", **kwargs):
    monkeypatch.setenv("SYVRIO_TEST_REASONING_KEY", "synthetic-test-only")
    return client.LiteLLMChatModel(
        LLMConfig(
            provider=provider,
            model=kwargs.pop("model", "deepseek-v4-flash"),
            base_url=kwargs.pop("base_url", "https://api.deepseek.com"),
            api_key_env="SYVRIO_TEST_REASONING_KEY",
            **kwargs,
        )
    )


def _tool(call_id, arguments="{}", index=None):
    return {"id": call_id, "index": index, "function": {"name": "recall", "arguments": arguments}}


def _delta(**fields):
    return {"choices": [{"delta": fields}]}


class _Stream:
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closed = True


def test_nonstream_reasoning_and_opaque_fields_are_separate(monkeypatch):
    model = _model(monkeypatch)
    requests = []
    message = {
        "content": "visible answer",
        "reasoning_content": "actual thought",
        "reasoning": [{"summary": "opaque payload"}],
        "tool_calls": [_tool("a")],
    }

    def complete(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    monkeypatch.setattr(client, "_litellm", lambda: SimpleNamespace(completion=complete))
    turn = model.chat([{"role": "user", "content": "synthetic"}], [{"type": "function"}])
    assert turn.content == "visible answer" and turn.reasoning_content == "actual thought"
    assert turn.reasoning_fragments == [{"reasoning": [{"summary": "opaque payload"}]}]
    assert turn.tool_calls == [client.ToolCall("a", "recall", {})]
    # foundation 不做路由验证，来源标记恒为空，因此历史推理不回放。
    assert turn.reasoning_source == ""
    assert requests[0]["messages"] == [{"role": "user", "content": "synthetic"}]
    message["tool_calls"][0]["function"]["arguments"] = "{"
    with pytest.raises(client.ChatModelError, match="malformed"):
        model.chat([], [])


def test_stream_multi_tool_reasoning_survives_session_reload(tmp_path, monkeypatch):
    model = _model(monkeypatch)
    requests = []
    streams = [
        _Stream(
            [
                _delta(reasoning_content="first ", reasoning="duplicate"),
                _delta(
                    reasoning_content="phase",
                    tool_calls=[_tool("a", '{"query":', 0), _tool("b", "{}", 1)],
                ),
                _delta(tool_calls=[_tool(None, '"synthetic"}', 0)]),
            ]
        ),
        _Stream([_delta(reasoning="second phase", tool_calls=[_tool("c", "{}", 0)])]),
        _Stream(
            [
                _delta(),
                _delta(reasoning_content="final thought"),
                _delta(content="Final answer"),
                {"choices": [], "usage": {"prompt_tokens": 20}},
            ]
        ),
        _Stream([_delta(content="Next answer")]),
    ]

    async def acompletion(**kwargs):
        requests.append(kwargs)
        return streams[len(requests) - 1]

    monkeypatch.setattr(client, "_litellm", lambda: SimpleNamespace(acompletion=acompletion))
    runtime, _ = build_runtime(tmp_path, model)
    runtime.config.auto_recall = 0
    updates, body = [], []
    reply = runtime.ask(
        "Synthetic request", on_thinking=updates.append, on_delta=body.append, cancel=Event()
    )
    assert reply.content == "Final answer" and reply.usage.prompt_tokens == 20
    assert body == ["Final answer"]
    assert [event.text for event in updates if event.record_id is not None] == [
        "first phase",
        "second phase",
        "final thought",
    ]
    # foundation 语义：来源标记恒空 → 推理不回放进请求；推理文本只落 Runtime 附件。
    assert all("reasoning_content" not in m for call in requests for m in call["messages"])
    jsonl = runtime.current.path.read_text(encoding="utf-8")
    assert "first phase" not in jsonl and "final thought" not in jsonl
    assert runtime.current.records[-1]["reasoning_local"] is True
    assert runtime.current.records[-1]["reasoning"]["text"] == "final thought"
    attachment = (
        runtime.sessions._reasoning_root / runtime.current.id
        / f"{runtime.current.records[-1]['id']}.json"
    )
    assert "final thought" in attachment.read_text(encoding="utf-8")
    session_id = runtime.current.id
    runtime.resume(session_id)
    # 重新加载后附件水合回推理文本，历史思考展示仍可用。
    assert runtime.current.records[-1]["reasoning"]["text"] == "final thought"
    runtime.ask("Synthetic next turn", on_thinking=updates.append, cancel=Event())
    assert all("reasoning_content" not in m for call in requests for m in call["messages"])
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize("cancelled", [True, False])
def test_incomplete_reasoning_never_replays_as_protocol(tmp_path, monkeypatch, cancelled):
    model = _model(monkeypatch)
    token = Event()
    stream = _Stream(
        [
            _delta(reasoning_content="partial thought"),
            _delta(tool_calls=[_tool("unfinished", "{", 0)]),
        ],
        error=OSError("synthetic disconnected stream"),
    )

    async def complete(**kwargs):
        return stream

    monkeypatch.setattr(client, "_litellm", lambda: SimpleNamespace(acompletion=complete))
    runtime, _ = build_runtime(tmp_path, model)
    runtime.config.auto_recall = 0

    def observe(update):
        if cancelled and not update.record_id:
            token.set()

    expected = client.GenerationCancelled if cancelled else client.ChatModelError
    with pytest.raises(expected):
        runtime.ask("Synthetic interrupted turn", on_thinking=observe, cancel=token)
    records = runtime.sessions.load(runtime.current.id).active_records()
    assert records[-1]["interrupted"] and records[-1]["reasoning"]["text"] == "partial thought"
    # 取消保留的部分思考只存 Runtime 附件；Vault JSONL 与模型请求都不含推理原文。
    jsonl = runtime.current.path.read_text(encoding="utf-8")
    assert "partial thought" not in jsonl
    assert records[-1]["reasoning_local"] is True
    attachment = (
        runtime.sessions._reasoning_root / runtime.current.id
        / f"{records[-1]['id']}.json"
    )
    assert "partial thought" in attachment.read_text(encoding="utf-8")
    assert [msg["role"] for msg in records_to_messages(records)] == ["user"]
    assert not any(record.get("type") == "tool" for record in records)
    assert stream.closed


def test_compact_keeps_recent_reasoning_chain_whole_and_private(tmp_path, monkeypatch):
    runtime, _ = build_runtime(tmp_path, _model(monkeypatch))
    store = runtime.sessions
    session = store.create()
    store.append(
        session, {"type": "message", "role": "user", "id": "u1", "content": "old text " * 1200}
    )
    store.append(
        session,
        {
            "type": "assistant",
            "id": "a1",
            "content": "old answer",
            "reasoning": {"text": "private old thought", "source": "route"},
        },
    )
    store.append(session, {"type": "message", "role": "user", "id": "u2", "content": "recent"})
    store.append(
        session,
        {
            "type": "assistant",
            "id": "a2",
            "content": "",
            "tool_calls": [{"id": "call", "name": "recall", "arguments": {}}],
            "reasoning": {"text": "private recent thought", "source": "route"},
        },
    )
    store.append(session, {"type": "tool", "id": "t2", "tool_call_id": "call", "content": "result"})
    manager = ContextManager(ChatConfig(context_window=1400), FakeSummarizer(), store)
    plan = manager.prepare(session, "system", "continue", force=True)
    assert plan.compacted
    assert [m["role"] for m in plan.messages[-4:]] == ["user", "assistant", "tool", "user"]
    # 推理原文绝不回放进模型请求：无论新旧记录、source 是否非空。
    assert all("reasoning_content" not in m and "reasoning_source" not in m for m in plan.messages)
    assert "private recent thought" not in str(plan.messages)
    assert "private old thought" not in str(plan.messages)
    # 附件方案：推理文本只落 Runtime 附件，Vault JSONL 不含原文，重载仍可读。
    jsonl = session.path.read_text(encoding="utf-8")
    assert "private recent thought" not in jsonl and "private old thought" not in jsonl
    recent_attachment = store._reasoning_root / session.id / "a2.json"
    old_attachment = store._reasoning_root / session.id / "a1.json"
    assert "private recent thought" in recent_attachment.read_text(encoding="utf-8")
    assert "private old thought" in old_attachment.read_text(encoding="utf-8")
    reloaded = store.load(session.id)
    assert reloaded.records[-2]["content"] == "result"
    assert reloaded.records[-3]["reasoning"]["text"] == "private recent thought"


def test_reasoning_stored_in_runtime_attachment_never_in_vault(tmp_path):
    store = SessionStore(tmp_path / "vault")
    session = store.create()
    marker = "FOUNDATION_PRIVATE_THOUGHT_TEST"
    record = store.append(
        session,
        {
            "type": "assistant", "id": "a-privacy", "content": "可见正文",
            "reasoning": {"text": f"{marker}：私有思考", "fragments": [], "source": "", "seconds": None},
        },
    )
    jsonl = session.path.read_text(encoding="utf-8")
    # Vault JSONL 只留标记，绝无推理原文（因此也不会进入 Git 快照与摘要输入）。
    assert marker not in jsonl
    assert record["reasoning_local"] is True
    assert '"reasoning_local": true' in jsonl
    attachment = store._reasoning_root / session.id / "a-privacy.json"
    assert f"{marker}：私有思考" in attachment.read_text(encoding="utf-8")
    # 附件路径由会话 ID 与记录 ID 派生，且锁在 Runtime 内。
    assert attachment.resolve().is_relative_to(store._reasoning_root.resolve())
    # 内存记录保留原文供展示；重新加载由附件水合。
    assert record["reasoning"]["text"] == f"{marker}：私有思考"
    loaded = store.load(session.id)
    assert loaded.records[-1]["reasoning"]["text"] == f"{marker}：私有思考"
    assert loaded.records[-1]["content"] == "可见正文"
    # 推理原文不进模型消息；正文照常进入。
    assert records_to_messages(loaded.records) == [
        {"role": "assistant", "content": "可见正文"}
    ]
    # 删除会话必须一并清理推理附件。
    assert store.delete(session.id)
    assert not attachment.exists()
    assert not (store._reasoning_root / session.id).exists()


def test_missing_or_corrupt_attachment_still_loads_body(tmp_path):
    store = SessionStore(tmp_path / "vault")
    session = store.create()
    store.append(
        session,
        {
            "type": "assistant", "id": "a-gone", "content": "正文一",
            "reasoning": {"text": "丢失的思考", "fragments": [], "source": "", "seconds": None},
        },
    )
    store.append(
        session,
        {
            "type": "assistant", "id": "a-bad", "content": "正文二",
            "reasoning": {"text": "损坏的思考", "fragments": [], "source": "", "seconds": None},
        },
    )
    (store._reasoning_root / session.id / "a-gone.json").unlink()
    (store._reasoning_root / session.id / "a-bad.json").write_text("{broken", encoding="utf-8")

    loaded = store.load(session.id)
    # 附件缺失或损坏都不阻断历史加载：正文完整，思考展示缺席。
    assert [r["content"] for r in loaded.records if r.get("type") == "assistant"] == ["正文一", "正文二"]
    assert all("reasoning" not in r for r in loaded.records if r.get("type") == "assistant")
    assert records_to_messages(loaded.records) == [
        {"role": "assistant", "content": "正文一"},
        {"role": "assistant", "content": "正文二"},
    ]


def test_old_inline_reasoning_with_source_never_replays(tmp_path):
    # 旧版本把推理原文内联写进 JSONL；即使 source 非空也只可用于展示，绝不回放。
    store = SessionStore(tmp_path / "vault")
    session = store.create()
    legacy = {
        "type": "assistant", "id": "a-legacy", "content": "旧正文",
        "reasoning": {"text": "旧版内联思考", "fragments": [], "source": "route", "seconds": 1.5},
    }
    with session.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(legacy, ensure_ascii=False) + "\n")

    loaded = store.load(session.id)
    record = loaded.records[-1]
    assert record["reasoning"]["text"] == "旧版内联思考"
    assert record["reasoning"]["source"] == "route"
    assert record.get("reasoning_local") is not True
    messages = records_to_messages(loaded.records)
    assert messages == [{"role": "assistant", "content": "旧正文"}]
    assert all("reasoning_content" not in m and "reasoning_source" not in m for m in messages)


def test_missing_record_id_degrades_to_body_without_attachment(tmp_path):
    # 旧版本可能写入缺 id 的 reasoning_local 标记：缺 ID 走附件降级，正文必须能加载。
    store = SessionStore(tmp_path / "vault")
    session = store.create()
    raw = json.dumps(
        {"type": "assistant", "content": "synthetic body", "reasoning_local": True},
        ensure_ascii=False,
    ) + "\n"
    with session.path.open("a", encoding="utf-8") as handle:
        handle.write(raw)
    original = session.path.read_bytes()

    loaded = store.load(session.id)
    record = loaded.records[-1]
    # 正文完全一致；无推理附件；原始 JSONL 未被改写。
    assert record["content"] == "synthetic body"
    assert "reasoning" not in record
    assert record.get("reasoning_local") is True
    assert session.path.read_bytes() == original
    assert records_to_messages(loaded.records) == [{"role": "assistant", "content": "synthetic body"}]
