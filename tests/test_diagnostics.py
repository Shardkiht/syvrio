from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from syvrio.infra import diagnostics
from syvrio.infra.paths import runtime_path

pytestmark = pytest.mark.config


@pytest.fixture(autouse=True)
def close_diagnostics():
    diagnostics.stop_diagnostics()
    yield
    diagnostics.stop_diagnostics()


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_chained_error_retains_traceback_and_reason_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMPLE_API_KEY", "synthetic-credential-123")
    path = diagnostics.start_diagnostics()
    assert path == runtime_path() / "logs" / "syvrio.log"
    assert diagnostics.start_diagnostics() == path
    try:
        try:
            raise ValueError("api_key=synthetic-inline-456 bearer synthetic-auth-789")
        except ValueError as cause:
            raise RuntimeError("Request failed: synthetic-credential-123") from cause
    except RuntimeError as error:
        diagnostics.record_event("chat.failed", error, busy=True, queued=2,
                                 content="private conversation", follow="private memory")
    diagnostics.stop_diagnostics()
    events = read_events(path)
    assert [event["event"] for event in events] == ["app.start", "chat.failed", "app.stop"]
    detail = events[1]["traceback"]
    assert "ValueError" in detail and "RuntimeError" in detail
    assert "Request failed" in detail and "test_chained_error" in detail
    assert "synthetic-credential-123" not in detail
    assert "synthetic-inline-456" not in detail
    assert "synthetic-auth-789" not in detail
    assert events[1]["state"] == {"busy": True, "queued": 2}
    assert "private conversation" not in path.read_text(encoding="utf-8")


def test_rotation_limits_files_and_keeps_valid_json(tmp_path, monkeypatch):
    path = diagnostics.start_diagnostics()
    diagnostics._handler.maxBytes = 800
    for _ in range(40):
        diagnostics.record_event("reading.changed", follow=False, scroll_y=20)
    diagnostics.stop_diagnostics()
    files = list(path.parent.glob("syvrio.log*"))
    assert len(files) == 4
    assert all(file.stat().st_size < 800 for file in files)
    assert all(read_events(file) for file in files)
    assert read_events(path)[-1]["event"] == "app.stop"


def test_threaded_events_drain_at_shutdown():
    path = diagnostics.start_diagnostics()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: diagnostics.record_event("chat.failed"), range(100)))
    diagnostics.stop_diagnostics()
    assert len(read_events(path)) == 102


def test_chat_latency_separates_stream_and_tool_completion_without_payloads(tmp_path, monkeypatch):
    from threading import Event
    from types import SimpleNamespace

    from syvrio.agent.runtime import AgentRuntime
    from syvrio.agent.session import SessionStore
    from syvrio.infra.config import ChatConfig, Config, LLMConfig, RetrievalConfig
    from syvrio.llm import client
    from syvrio.memory.service import MemoryService

    class Stream:
        def __init__(self, deltas):
            self.deltas = deltas

        async def __aiter__(self):
            for delta in self.deltas:
                yield {"choices": [{"delta": delta}]}

        async def aclose(self):
            pass

    tool_call = {
        "index": 0, "id": "test-tool-id",
        "function": {"name": "memorize", "arguments": '{"text": "private synthetic memory"}'},
    }
    rounds = iter([
        [{"reasoning_content": "private synthetic reasoning"}, {"tool_calls": [tool_call]}],
        [{"content": "private synthetic answer"}, {"content": " again"}],
    ])
    tool_requests = []

    async def acompletion(**kwargs):
        return Stream(next(rounds))

    def completion(**kwargs):
        tool_requests.append(True)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"memories": []}'))])

    monkeypatch.setattr(client, "_litellm", lambda: SimpleNamespace(acompletion=acompletion, completion=completion))
    path = diagnostics.start_diagnostics()
    config = Config(
        vault=tmp_path / "vault", llm=LLMConfig(provider="local"),
        chat=ChatConfig(auto_recall=0), retrieval=RetrievalConfig(method="keyword"),
    )
    service = MemoryService(config)
    service.init_vault()
    runtime = AgentRuntime(service, client.LiteLLMChatModel(config.llm), SessionStore(config.paths()))
    reply = runtime.ask("private synthetic prompt", on_delta=lambda _: None, cancel=Event())
    assert reply.steps == 2 and tool_requests == [True]
    diagnostics.stop_diagnostics()

    latency_path = path.with_name("latency.log")
    events = read_events(latency_path)
    names = [event["event"].removeprefix("latency.") for event in events]
    assert names == [
        "turn.started", "recall.started", "recall.finished", "context.started", "context.finished",
        "model.started", "model.request", "model.stream_open", "model.first_chunk",
        "model.first_reasoning", "model.first_tool", "model.finished", "tools.started",
        "completion.started", "completion.finished", "tool.finished", "tools.finished",
        "model.started", "model.request", "model.stream_open", "model.first_chunk",
        "model.first_text", "model.finished", "turn.finished",
    ]
    assert len({event["state"]["turn"] for event in events}) == 1
    assert events[names.index("completion.finished")]["state"]["step"] == 1
    assert events[names.index("model.first_text")]["state"]["step"] == 2
    assert events[-1]["state"]["ok"]
    assert "private synthetic" not in latency_path.read_text(encoding="utf-8")
    assert all(not event["event"].startswith("latency.") for event in read_events(path))


def test_unavailable_file_does_not_break_application(monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("synthetic denial")

    monkeypatch.setattr(diagnostics, "_QuietFileHandler", denied)
    assert diagnostics.start_diagnostics() is None
    diagnostics.record_event("chat.failed", RuntimeError("ignored"))
    diagnostics.stop_diagnostics()


def test_unknown_events_and_unconfigured_logging_are_ignored():
    diagnostics.record_event("chat.failed")
    path = diagnostics.start_diagnostics()
    diagnostics.record_event("arbitrary user text")
    diagnostics.stop_diagnostics()
    assert len(read_events(path)) == 2


def test_every_source_event_is_allowlisted():
    # 事件名不在白名单会被静默丢弃，曾让画像摘要失败原因无法排查。
    import re

    source_root = Path(diagnostics.__file__).resolve().parents[1]
    used = set()
    for path in source_root.rglob("*.py"):
        used.update(re.findall(r'record_event\("([a-z._]+)"', path.read_text(encoding="utf-8")))
    assert used
    assert used <= diagnostics._EVENTS, f"未登记的事件: {sorted(used - diagnostics._EVENTS)}"


def test_full_queue_does_not_block_producer(monkeypatch):
    from queue import Full

    path = diagnostics.start_diagnostics()

    def full(*args, **kwargs):
        raise Full

    with monkeypatch.context() as patch:
        patch.setattr(diagnostics._queue, "put_nowait", full)
        diagnostics.record_event("reading.changed", follow=False)
    diagnostics.stop_diagnostics()
    assert len(read_events(path)) == 2


def test_cli_entrypoint_logs_unhandled_error(monkeypatch):
    from syvrio.app import cli

    def fail():
        raise RuntimeError("synthetic command failure")

    monkeypatch.setattr(cli, "app", fail)
    with pytest.raises(RuntimeError, match="synthetic command failure"):
        cli.main()
    path = runtime_path() / "logs" / "syvrio.log"
    events = read_events(path)
    assert [event["event"] for event in events] == ["app.start", "cli.failed", "app.stop"]
    assert "synthetic command failure" in events[1]["traceback"]
