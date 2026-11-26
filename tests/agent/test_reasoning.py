from __future__ import annotations

import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from syvrio.agent.context import ContextManager, records_to_messages
from syvrio.infra.config import ChatConfig, LLMConfig
from syvrio.infra.llm import client
from syvrio.infra.security.credentials import save_api_key
from tests.agent.test_agent import FakeSummarizer, build_runtime

pytestmark = pytest.mark.agent


def _model(provider="deepseek", **kwargs):
    assert save_api_key("SYVRIO_TEST_REASONING_KEY", "synthetic-test-only")
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
    model = _model()
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
    assert turn.reasoning_source
    assert requests[0]["messages"] == [{"role": "user", "content": "synthetic"}]
    message["tool_calls"][0]["function"]["arguments"] = "{"
    with pytest.raises(client.ChatModelError, match="malformed"):
        model.chat([], [])


def test_stream_multi_tool_reasoning_survives_session_reload(tmp_path, monkeypatch):
    model = _model()
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
    second = requests[1]["messages"][-3:]
    assert second[0]["reasoning_content"] == "first phase"
    assert [msg["tool_call_id"] for msg in second[1:]] == ["a", "b"]
    assert json.loads(second[0]["tool_calls"][0]["function"]["arguments"]) == {"query": "synthetic"}
    third = requests[2]["messages"]
    assert [m["reasoning_content"] for m in third if "reasoning_content" in m] == [
        "first phase",
        "second phase",
    ]
    session_id = runtime.current.id
    assert "first phase" not in runtime.current.path.read_text(encoding="utf-8")
    runtime.resume(session_id)
    runtime.ask("Synthetic next turn", on_thinking=updates.append, cancel=Event())
    assert [
        m["reasoning_content"] for m in requests[3]["messages"] if "reasoning_content" in m
    ] == [
        "first phase",
        "second phase",
        "final thought",
    ]
    assert all("reasoning_source" not in m for call in requests for m in call["messages"])
    assert all(stream.closed for stream in streams)
    assert "reasoning" not in runtime.current.records[-1]


@pytest.mark.parametrize(
    ("provider", "model_id", "base", "expected"),
    [
        (
            "dashscope",
            "deepseek-v4.1-flash-1017",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ["current"],
        ),
        ("dashscope", "qwen-plus", "https://dashscope.aliyuncs.com/compatible-mode/v1", []),
        (
            "qianwen",
            "qwen3.8-max",
            "https://maas.qianwenaiapi.com/compatible-mode/v1",
            ["earlier", "current"],
        ),
        ("qianwen", "qwen3.8-max", "https://example.test/v1", []),
        ("qianwen", "glm-5.3", "https://maas.qianwenaiapi.com/compatible-mode/v1", ["current"]),
        ("openai", "gpt-4o-mini", "https://api.openai.com/v1", []),
        ("siliconflow", "deepseek-ai/DeepSeek-V4-Flash", "https://api.siliconflow.cn/v1", []),
        ("deepseek", "deepseek-v4-flash", "https://example.test/v1", []),
        ("custom", "synthetic", "https://example.test/v1", []),
    ],
)
def test_reasoning_replay_respects_route_and_turn(monkeypatch, provider, model_id, base, expected):
    model = _model(provider, model=model_id, base_url=base)
    origin = client._reasoning_route(model.config)[0] or "different-model-or-endpoint"
    messages = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "earlier",
            "reasoning_source": origin,
        },
        {"role": "user", "content": "next turn"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "current",
            "reasoning_source": origin,
        },
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "foreign",
            "reasoning_source": "other",
        },
    ]
    sent = []

    def complete(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message={"content": "normal"})], usage=None)

    monkeypatch.setattr(client, "_litellm", lambda: SimpleNamespace(completion=complete))
    assert model.chat(messages, []).reasoning_content == ""
    wire = sent[0]
    assert [
        m["reasoning_content"] for m in wire["messages"] if "reasoning_content" in m
    ] == expected
    assert "fallbacks" not in wire
    assert messages[-1]["reasoning_content"] == "foreign"


@pytest.mark.parametrize("provider", ["spark", "deepseek", "qianwen"])
def test_history_budget_matches_wire_after_switch_and_tool_calls(tmp_path, monkeypatch, provider):
    from syvrio.agent.context import estimate_tool_tokens
    from syvrio.infra.llm.tokens import estimate_messages_tokens

    base = {
        "deepseek": "https://api.deepseek.com",
        "qianwen": "https://maas.qianwenaiapi.com/compatible-mode/v1",
        "spark": "https://example.test/v1",
    }
    model_id = {"deepseek": "deepseek-v4-flash", "qianwen": "glm-5.3", "spark": "spark-x2.6"}
    model = _model(provider, model=model_id[provider], base_url=base[provider])
    runtime, _ = build_runtime(tmp_path, model)
    runtime.config.auto_recall = 0
    session = runtime.ensure_session()
    source = client._reasoning_route(model.config)[0]
    runtime.sessions.append(
        session, {"type": "message", "role": "user", "id": "u-prior", "content": "prior"}
    )
    runtime.sessions.append(
        session,
        {
            "type": "assistant",
            "content": "old answer",
            "id": "a-prior",
            "reasoning": {
                "text": "不可重放" * 18000 if provider != "deepseek" else "matching origin",
                "source": source or "previous model",
            },
        },
    )
    sent = []

    def respond(**kwargs):
        sent.append(kwargs)
        message = {"content": "answer"}
        if len(sent) == 1:
            message = {
                "reasoning_content": "active chain",
                "tool_calls": [
                    {
                        "id": "tool1",
                        "function": {"name": "recall", "arguments": '{"query":"sample"}'},
                    }
                ],
            }
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    monkeypatch.setattr(client, "_litellm", lambda: SimpleNamespace(completion=respond))
    reply = runtime.ask("next turn")
    assert len(sent) == 2 and not reply.compacted
    first = sent[0]["messages"]
    earlier = [m["reasoning_content"] for m in first if "reasoning_content" in m]
    assert earlier == (["matching origin"] if provider == "deepseek" else [])
    wire = sent[-1]["messages"]
    assert any(m.get("reasoning_content") == "active chain" for m in wire) == bool(source)
    assert reply.estimated_tokens == estimate_messages_tokens(wire) + estimate_tool_tokens(
        sent[-1]["tools"]
    )


def test_qianwen_max_preserves_reasoning_for_follow_up_without_tools(monkeypatch):
    model = _model(
        "qianwen",
        model="qwen4-max",
        base_url="https://maas.qianwenaiapi.com/compatible-mode/v1",
    )
    sent = []

    def complete(**kwargs):
        sent.append(kwargs["messages"])
        message = {"content": "answer", "reasoning_content": "synthetic reasoning"}
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    monkeypatch.setattr(client, "_litellm", lambda: SimpleNamespace(completion=complete))
    first = model.chat([{"role": "user", "content": "example"}], [])
    assert first.reasoning_source
    model.chat(
        [
            {
                "role": "assistant",
                "content": first.content,
                "reasoning_source": first.reasoning_source,
                "reasoning_content": first.reasoning_content,
            },
            {"role": "user", "content": "follow-up"},
        ],
        [],
    )
    assert sent[1][0]["reasoning_content"] == "synthetic reasoning"


@pytest.mark.parametrize(
    ("adapter", "model"),
    [
        ("DeepSeekChatConfig", "deepseek-v4-flash"),
        ("DashScopeChatConfig", "deepseek-v4.1-flash-1017"),
    ],
)
def test_installed_adapter_keeps_reasoning_in_request_body(adapter, model):
    config = getattr(client._litellm(), adapter)()
    messages = [
        {"role": "user", "content": "Synthetic question"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "Synthetic thought",
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "recall", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "Synthetic result"},
    ]
    body = config.transform_request(model, messages, {}, {}, {})
    assert body["messages"][1]["reasoning_content"] == "Synthetic thought"
    assert body["messages"][2]["tool_call_id"] == "a"


@pytest.mark.parametrize("cancelled", [True, False])
def test_incomplete_reasoning_never_replays_as_protocol(tmp_path, monkeypatch, cancelled):
    model = _model()
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
    assert [msg["role"] for msg in records_to_messages(records)] == ["user"]
    assert not any(record.get("type") == "tool" for record in records)
    assert stream.closed


def test_compaction_summarizes_tool_turns_without_disclosing_private_reasoning(
    tmp_path, monkeypatch
):
    runtime, _ = build_runtime(tmp_path, _model())
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
    store.append(session, {"type": "assistant", "id": "answer", "content": "recent answer"})
    store.append(session, {"type": "message", "role": "user", "id": "now", "content": "continue"})
    summarizer = Mock(wraps=FakeSummarizer())
    manager = ContextManager(ChatConfig(context_window=1400), summarizer, store)
    plan = manager.prepare(session, "system", "continue", current_id="now")
    assert plan.compacted and plan.evicted_records == 6
    summarizer.summarize.assert_called_once()
    transcript = summarizer.summarize.call_args.args[1]
    assert all(
        text in transcript for text in ("old answer", "recent", "recall", "result", "recent answer")
    )
    assert "private" not in transcript and "private" not in str(plan.messages)
    assert [m["role"] for m in plan.messages] == ["system", "system", "user"]
    assert plan.messages[-1]["content"] == "continue"
    assert "private" not in session.path.read_text(encoding="utf-8")
    restored = store.load(session.id)
    assert [r["id"] for r in restored.records_after_summary()] == ["now"]
    replay = records_to_messages(restored.active_records())
    assert [m["reasoning_content"] for m in replay if "reasoning_content" in m] == [
        "private old thought",
        "private recent thought",
    ]
    assert replay[3]["tool_calls"][0]["id"] == replay[4]["tool_call_id"] == "call"
    assert replay[4]["content"] == "result"
