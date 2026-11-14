"""Web adapter tests: isolated Vault, fake model, no network and no real keys."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from syvrio.app.web.backend import WebBackend, WebError
from syvrio.app.web.server import create_app
from syvrio.infra.config import (
    ChatConfig,
    Config,
    LLMConfig,
    RetrievalConfig,
    SearchConfig,
)
from syvrio.infra.locking import LockTimeout
from syvrio.llm import (
    AssistantTurn,
    ChatModelError,
    GenerationCancelled,
    LiteLLMChatModel,
    ToolCall,
)
from syvrio.memory.extractor import StaticExtractor
from syvrio.memory.models import ExtractionResult
from syvrio.memory.service import MemoryService

pytestmark = pytest.mark.web

BASE_URL = "http://127.0.0.1:8765"


class FakeModel(LiteLLMChatModel):
    """Subclass so AgentRuntime takes the native streaming/reasoning path."""

    def __init__(self, turns):
        super().__init__(LLMConfig())
        self.turns = list(turns)
        self.calls = 0

    def chat(self, messages, tools, on_delta=None, cancel=None, *, on_reasoning=None):
        self.calls += 1
        turn = self.turns.pop(0) if self.turns else AssistantTurn(content="(empty)")
        if isinstance(turn, Exception):
            raise turn
        if on_reasoning and turn.reasoning_content:
            on_reasoning(turn.reasoning_content)
        if on_delta and turn.content:
            middle = max(1, len(turn.content) // 2)
            on_delta(turn.content[:middle])
            on_delta(turn.content[middle:])
        return turn


class GatedModel(FakeModel):
    def __init__(self):
        super().__init__([])
        self.entered = threading.Event()
        self.release = threading.Event()

    def chat(self, messages, tools, on_delta=None, cancel=None, *, on_reasoning=None):
        self.entered.set()
        if on_delta:
            on_delta("partial ")
        assert self.release.wait(timeout=10), "test model was never released"
        if cancel is not None and cancel.is_set():
            raise GenerationCancelled("Stopped by user")
        return AssistantTurn(content="partial done")


@pytest.fixture
def backend(tmp_path):
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(
            provider="custom", model="fake", api_key_env="SYVRIO_WEB_TEST_KEY", base_url=""
        ),
        chat=ChatConfig(
            auto_recall=0, recall_rewrite=False, extract_on_exit=False, max_tool_steps=4
        ),
        retrieval=RetrievalConfig(method="keyword"),
        search=SearchConfig(enabled=False),
    )
    service = MemoryService(config, extractor=StaticExtractor(ExtractionResult(memories=[])))
    service.init_vault()
    owner = WebBackend(service=service)
    owner._runtime.model = FakeModel([])
    owner.startup()
    yield owner
    owner.shutdown()


@pytest.fixture
def client(backend):
    return TestClient(create_app(backend, autostart=False), base_url=BASE_URL)


def collect(client, message="hi", session_id=None):
    with client.stream(
        "POST",
        "/api/chat",
        json={"message": message, "session_id": session_id},
    ) as response:
        assert response.status_code == 200, response.text
        events = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        return events


def types(events):
    return [event["type"] for event in events]


def wait_active(backend):
    for _ in range(200):
        if backend.active_turn_id() is not None:
            return backend.active_turn_id()
        time.sleep(0.01)
    raise AssertionError("turn never started")


# ---------- health, sessions, history ----------


def test_health_and_openapi(client, backend):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["provider"] == "custom"
    assert body["busy"] is False
    assert body["single_client"] is True
    assert client.get("/openapi.json").status_code == 200


def test_session_lifecycle(client, backend):
    before = len(backend._sessions.list())
    created = client.post("/api/sessions")
    assert created.status_code == 201
    body = created.json()
    # A new chat only opens the empty view; the file appears with message one.
    assert body["session_id"] is None
    assert body["draft"] is True
    assert len(backend._sessions.list()) == before

    session = backend._sessions.create()
    listing = client.get("/api/sessions").json()
    assert any(item["id"] == session.id for item in listing["items"])

    history = client.get(f"/api/sessions/{session.id}").json()
    assert history["session_id"] == session.id
    assert history["items"] == []

    switched = client.post(f"/api/sessions/{session.id}/switch")
    assert switched.status_code == 200
    assert switched.json()["session_id"] == session.id
    assert client.get(f"/api/sessions/{session.id}").json()["current"] is True


def test_first_message_persists_the_session(client, backend):
    backend._runtime.model = FakeModel([AssistantTurn(content="hello")])
    before = len(backend._sessions.list())
    events = collect(client)
    assert len(backend._sessions.list()) == before + 1
    # ``start`` is emitted before the worker creates the file, so the id shows
    # up on the following frames; the client adopts it from there.
    ids = {event["session_id"] for event in events if event.get("session_id")}
    assert len(ids) == 1
    session_id = ids.pop()
    assert backend._sessions.exists(session_id)
    rows = client.get("/api/sessions").json()["items"]
    assert [row["id"] for row in rows if row["current"]] == [session_id]


# ---------- session deletion ----------


def test_delete_session_requires_confirmation_then_removes_file(client, backend):
    store = backend._sessions
    target = store.create()
    store.append(
        target,
        {
            "type": "message",
            "id": "m-1",
            "role": "user",
            "content": "to be deleted",
            "at": "2026-11-09T12:00:00+08:00",
        },
    )
    url = f"/api/sessions/{target.id}"

    unconfirmed = client.delete(url)
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["error"]["code"] == "confirm_required"
    assert store.exists(target.id)

    removed = client.delete(url, params={"confirmed": "true"})
    assert removed.status_code == 200
    assert removed.json() == {"session_id": target.id, "deleted": True}
    assert not store.exists(target.id)
    assert target.id not in [row["id"] for row in client.get("/api/sessions").json()["items"]]
    assert client.get(url).status_code == 404


def test_current_session_is_protected_from_deletion(client, backend):
    session_id = backend._sessions.create().id
    client.post(f"/api/sessions/{session_id}/switch")
    response = client.delete(f"/api/sessions/{session_id}", params={"confirmed": "true"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_protected"
    assert backend._sessions.exists(session_id)


def test_delete_rejects_unknown_traversal_and_cross_site(client):
    assert client.delete("/api/sessions/s-missing", params={"confirmed": "true"}).status_code == 404
    traversal = client.delete("/api/sessions/../vault", params={"confirmed": "true"})
    # Normalised to /api/vault: the GET-only SPA fallback answers 405 for
    # DELETE; either way the traversal never reaches the session store.
    assert traversal.status_code in (404, 405, 422)
    assert (
        client.delete(
            "/api/sessions/s-missing",
            params={"confirmed": "true"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )


def test_delete_is_blocked_while_a_turn_runs(backend):
    store = backend._sessions
    target = store.create()
    model = GatedModel()
    backend._runtime.model = model
    app = create_app(backend, autostart=False)
    streamer = TestClient(app, base_url=BASE_URL)
    controller = TestClient(app, base_url=BASE_URL)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(collect, streamer, "first")
        wait_active(backend)
        response = controller.delete(f"/api/sessions/{target.id}", params={"confirmed": "true"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "busy"
        assert store.exists(target.id)
        model.release.set()
        future.result(timeout=15)


def test_sidebar_orders_by_last_message_not_control_records(client, backend):
    store = backend._sessions
    older = store.create()
    store.append(
        older,
        {
            "type": "message",
            "id": "m-older",
            "role": "user",
            "content": "older message",
            "at": "2026-11-09T10:00:00+08:00",
        },
    )
    # A rename (or summary / distill / undo) must not promote an older chat.
    store.append(
        older,
        {
            "type": "title",
            "id": "t-older",
            "title": "renamed later",
            "at": "2999-01-01T00:00:00+08:00",
        },
    )
    newer = store.create()
    store.append(
        newer,
        {
            "type": "message",
            "id": "m-newer",
            "role": "user",
            "content": "newer message",
            "at": "2026-11-09T12:00:00+08:00",
        },
    )

    rows = client.get("/api/sessions").json()["items"]
    by_id = {row["id"]: row for row in rows}
    assert by_id[older.id]["updated_at"] == "2998-12-31T16:00:00+00:00"
    assert by_id[older.id]["last_message_at"] == "2026-11-09T02:00:00+00:00"
    assert by_id[newer.id]["last_message_at"] == "2026-11-09T04:00:00+00:00"
    stamps = [row["last_message_at"] for row in rows]
    assert stamps == sorted(stamps, reverse=True)
    ids = [row["id"] for row in rows if row["id"] in (older.id, newer.id)]
    assert ids == [newer.id, older.id]


def test_invalid_session_id_is_rejected(client):
    for bad in ("../vault", "s-1/../../x", "..", "%2e%2e", "a" * 200):
        response = client.get(f"/api/sessions/{bad}")
        assert response.status_code in (404, 422), bad
        assert "vault" not in response.text.lower() or response.status_code != 200
        if response.status_code == 422:
            assert response.json()["error"]["code"] == "invalid_session_id"


def test_history_restores_reasoning_and_content(client, backend):
    backend._runtime.model = FakeModel(
        [
            AssistantTurn(
                content="Hello 世界",
                reasoning_content="先想一步\n再想一步",
                reasoning_source="local",
            ),
        ]
    )
    events = collect(client, "hi")
    assert "done" in types(events)
    session_id = next(event["session_id"] for event in events if event.get("session_id"))
    history = client.get(f"/api/sessions/{session_id}").json()
    assistant = next(item for item in history["items"] if item["role"] == "assistant")
    assert assistant["content"] == "Hello 世界"
    assert assistant["reasoning"]["text"] == "先想一步\n再想一步"


# ---------- streaming ----------


def test_stream_events_are_ordered_and_utf8(client, backend):
    backend._runtime.model = FakeModel(
        [
            AssistantTurn(content="你好，世界 🌍", reasoning_content="思考中"),
        ]
    )
    events = collect(client, "hi")
    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "done"
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert "reasoning_delta" in types(events)
    assert "reasoning_end" in types(events)
    body = "".join(event["text"] for event in events if event["type"] == "content_delta")
    assert body == "你好，世界 🌍"
    assert events[-1]["text"] == "你好，世界 🌍"
    assert events[-1]["session_id"]


def test_thinking_folds_when_answer_starts(client, backend):
    backend._runtime.model = FakeModel(
        [
            AssistantTurn(content="answer", reasoning_content="thinking text"),
        ]
    )
    events = collect(client, "hi")
    order = types(events)
    # Runtime order: reasoning deltas -> body deltas -> finalised reasoning
    # record. The frontend folds thinking on the first content_delta.
    assert order.index("reasoning_delta") < order.index("content_delta")
    assert order.index("content_delta") < order.index("reasoning_end")
    assert order[-1] == "done"


def test_tool_call_rounds_keep_reasoning_phases_apart(client, backend):
    backend._runtime.model = FakeModel(
        [
            AssistantTurn(
                content="",
                reasoning_content="first round",
                tool_calls=[ToolCall(id="call-1", name="recall", arguments={"query": "note"})],
            ),
            AssistantTurn(content="final answer", reasoning_content="second round"),
        ]
    )
    events = collect(client, "check my note")
    assert "tool_start" in types(events)
    assert "tool_end" in types(events)
    # The outcome rides along so the client can label a failure without
    # waiting for a history reload.
    tool_end = next(event for event in events if event["type"] == "tool_end")
    assert tool_end["name"] == "recall"
    assert tool_end["ok"] is True
    tool_index = types(events).index("tool_start")
    phases = [event["phase"] for event in events if event["type"] == "reasoning_delta"]
    assert len(phases) == 2 and phases[1] > phases[0]
    assert types(events).index("reasoning_delta") < tool_index
    assert any(
        event["type"] == "reasoning_delta" and event["phase"] == phases[1]
        for event in events[tool_index:]
    )
    assert events[-1]["type"] == "done"
    assert events[-1]["text"] == "final answer"


def test_tool_failure_is_reported_on_the_stream(client, backend):
    backend._runtime.model = FakeModel(
        [
            AssistantTurn(
                content="",
                reasoning_content="",
                tool_calls=[ToolCall(id="call-1", name="no_such_tool", arguments={})],
            ),
            AssistantTurn(content="done"),
        ]
    )
    events = collect(client, "try something odd")
    ends = [event for event in events if event["type"] == "tool_end"]
    assert ends, "a failed call must still close its tool row"
    assert ends[0]["name"] == "no_such_tool"
    assert ends[0]["ok"] is False


def test_failed_turn_reports_error_and_recovers(client, backend):
    backend._runtime.model = FakeModel([ChatModelError("model unavailable")])
    events = collect(client, "hi")
    assert events[-1]["type"] == "failed"
    assert "model unavailable" in events[-1]["message"]
    assert client.get("/api/health").json()["busy"] is False

    backend._runtime.model = FakeModel([AssistantTurn(content="recovered")])
    assert collect(client, "again", events[-1]["session_id"])[-1]["type"] == "done"


# ---------- cancellation, FIFO, disconnect ----------


def test_cancel_only_the_active_turn(backend):
    model = GatedModel()
    backend._runtime.model = model
    app = create_app(backend, autostart=False)
    streamer = TestClient(app, base_url=BASE_URL)
    controller = TestClient(app, base_url=BASE_URL)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(collect, streamer, "long answer")
        turn_id = wait_active(backend)
        response = controller.post("/api/chat/cancel", json={"turn_id": turn_id})
        assert response.status_code == 200
        assert response.json()["cancelled"] is True
        # A stale turn id must not cancel anything.
        assert (
            controller.post("/api/chat/cancel", json={"turn_id": turn_id + 999}).json()["cancelled"]
            is False
        )
        model.release.set()
        events = future.result(timeout=15)
    assert any(event["type"] == "cancelled" for event in events)
    assert backend.active_turn_id() is None
    assert client_health_idle(app)


def client_health_idle(app):
    probe = TestClient(app, base_url=BASE_URL)
    return probe.get("/api/health").json()["busy"] is False


def test_second_chat_request_gets_busy_error(backend):
    model = GatedModel()
    backend._runtime.model = model
    app = create_app(backend, autostart=False)
    streamer = TestClient(app, base_url=BASE_URL)
    controller = TestClient(app, base_url=BASE_URL)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(collect, streamer, "first")
        wait_active(backend)
        response = controller.post("/api/chat", json={"message": "second", "session_id": None})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "busy"
        model.release.set()
        assert future.result(timeout=15)[-1]["type"] == "done"
    assert backend.active_turn_id() is None


def test_disconnect_cancels_but_waits_for_worker(backend):
    model = GatedModel()
    backend._runtime.model = model

    async def scenario():
        loop = asyncio.get_running_loop()
        turn = backend.start_turn("hi", None, loop)
        generator = backend.stream(turn)
        await generator.__anext__()  # first frame
        await generator.aclose()  # browser went away
        return turn

    turn = asyncio.run(scenario())
    # Disconnect cancels the turn but must not release it while the worker is
    # still inside runtime.ask, otherwise a new request could overlap it.
    assert backend.active_turn_id() == turn.turn_id
    with pytest.raises(WebError) as busy:
        backend.start_turn("second", None, turn.loop)
    assert busy.value.code == "busy"
    model.release.set()
    assert turn.finished.wait(timeout=10)
    assert backend.active_turn_id() is None
    assert client_health_idle(create_app(backend, autostart=False)) is True


def test_switch_rejected_while_generating(backend):
    model = GatedModel()
    backend._runtime.model = model
    app = create_app(backend, autostart=False)
    streamer = TestClient(app, base_url=BASE_URL)
    controller = TestClient(app, base_url=BASE_URL)
    created = backend._sessions.create().id
    controller.post(f"/api/sessions/{created}/switch")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(collect, streamer, "first", created)
        wait_active(backend)
        response = controller.post(f"/api/sessions/{created}/switch")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "busy"
        model.release.set()
        future.result(timeout=15)


# ---------- security ----------


def test_host_and_origin_are_checked(client):
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 403
    assert (
        client.post("/api/sessions", headers={"Origin": "https://evil.example"}).status_code == 403
    )
    assert client.post("/api/sessions", headers={"Origin": BASE_URL}).status_code == 201
    # Proxy headers must not widen trust.
    assert (
        client.get("/api/health", headers={"X-Forwarded-Host": "evil.example"}).status_code == 200
    )


def test_custom_local_domain_is_trusted_for_host_and_origin(backend):
    app = create_app(backend, autostart=False, allowed_hosts=["syvrio.local"])
    probe = TestClient(app, base_url="http://syvrio.local:8765")
    assert probe.get("/api/health").status_code == 200
    assert (
        probe.post("/api/sessions", headers={"Origin": "http://syvrio.local:8765"}).status_code
        == 201
    )
    assert (
        probe.post("/api/sessions", headers={"Origin": "https://evil.example"}).status_code == 403
    )
    assert probe.get("/api/health", headers={"Host": "evil.example"}).status_code == 403


def test_consistent_error_shape_and_no_stacktrace(backend, monkeypatch):
    # raise_server_exceptions=False matches uvicorn: the ServerErrorMiddleware
    # returns our JSON response and the server keeps running.
    probe = TestClient(
        create_app(backend, autostart=False),
        base_url=BASE_URL,
        raise_server_exceptions=False,
    )
    response = probe.get("/api/sessions/no-such-session")
    assert response.status_code == 404
    assert set(response.json()) == {"error"}
    assert set(response.json()["error"]) == {"code", "message"}

    def explode(*args, **kwargs):
        raise RuntimeError("SECRET INTERNAL DETAIL")

    monkeypatch.setattr(backend, "list_sessions", explode)
    failed = probe.get("/api/sessions")
    assert failed.status_code == 500
    assert "SECRET" not in failed.text
    assert "Traceback" not in failed.text


def test_invalid_payload_and_empty_message(client):
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
    assert client.post("/api/chat", json={}).status_code == 422
    assert client.post("/api/chat/cancel", json={"turn_id": "x"}).status_code == 422


def test_client_busy_is_reported(backend, monkeypatch):
    def denied(*args, **kwargs):
        raise LockTimeout("occupied")

    monkeypatch.setattr(backend._client_lock, "acquire", denied)
    with pytest.raises(WebError) as error:
        WebBackend(service=backend._service).startup()
    assert error.value.code == "client_busy"


# ---------- static assets ----------


def test_missing_assets_are_reported_explicitly(backend, tmp_path, monkeypatch):
    # Point at an empty directory: a locally built static/ bundle must not
    # change this test's outcome.
    monkeypatch.delenv("SYVRIO_WEB_STATIC", raising=False)
    app = create_app(backend, autostart=False, static_dir=tmp_path / "no-assets")
    probe = TestClient(app, base_url=BASE_URL)
    assert probe.get("/api/health").json()["static_ready"] is False
    response = probe.get("/")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "assets_missing"


def test_static_files_and_spa_fallback(backend, tmp_path):
    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>syvrio</html>", encoding="utf-8")
    (static_dir / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    app = create_app(backend, autostart=False, static_dir=static_dir)
    probe = TestClient(app, base_url=BASE_URL)
    assert probe.get("/api/health").json()["static_ready"] is True
    assert "syvrio" in probe.get("/").text
    assert "console.log" in probe.get("/assets/app.js").text
    assert probe.get("/some/client/route").status_code == 200
    assert probe.get("/api/unknown").status_code == 404


# ---------- command concurrency ----------


def test_long_command_keeps_health_and_cancel_responsive(backend, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    cancelled = []
    monkeypatch.setattr(backend._app_service.reflection, "run", lambda *a, **k: None)
    monkeypatch.setattr(
        backend._app_service.reflection, "cancel", lambda *a, **k: cancelled.append(True)
    )
    real_execute = backend._commands.execute

    def slow_execute(line, *, busy=False, confirmed=False):
        if line.strip() == "/dream 1":
            entered.set()
            assert release.wait(timeout=10), "command was never released"
        return real_execute(line, busy=busy, confirmed=confirmed)

    monkeypatch.setattr(backend._commands, "execute", slow_execute)
    app = create_app(backend, autostart=False)
    command_client = TestClient(app, base_url=BASE_URL)
    controller = TestClient(app, base_url=BASE_URL)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            lambda: command_client.post(
                "/api/commands", json={"line": "/dream 1", "confirmed": True}
            )
        )
        assert entered.wait(timeout=10)
        # Health reflects the running command and must not block behind its lock.
        assert controller.get("/api/health").json()["busy"] is True
        # A chat request is rejected busy instead of overlapping the command.
        chat = controller.post("/api/chat", json={"message": "hi"})
        assert chat.status_code == 409
        assert chat.json()["error"]["code"] == "busy"
        # /dream cancel bypasses the command mutex and interrupts the run.
        cancel = controller.post("/api/commands", json={"line": "/dream cancel"})
        assert cancel.status_code == 200
        assert cancelled
        release.set()
        assert future.result(timeout=10).status_code == 200
    assert backend._operation is None
    assert backend.active_turn_id() is None


def test_chat_rejects_a_stale_session_id(client, backend):
    current = backend._sessions.create().id
    client.post(f"/api/sessions/{current}/switch")
    other = backend._sessions.create().id
    response = client.post("/api/chat", json={"message": "hi", "session_id": other})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_mismatch"
    assert backend.active_turn_id() is None


class GatedExtractor:
    def __init__(self, *, fail=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.fail = fail

    def extract(self, text, context):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=10)
        if self.fail:
            raise RuntimeError("synthetic extraction failure")
        return ExtractionResult(memories=[])


def seed_distillation(backend):
    session = backend._runtime.ensure_session()
    for index in range(3):
        backend._sessions.append(
            session,
            {"type": "message", "role": "user", "id": f"m-seed-{index}", "content": "synthetic"},
        )
    backend._runtime.config.extract_on_exit = True
    return session


def wait_for_distillation(backend):
    with backend._lock:
        worker = backend._distill_worker
    if worker is not None:
        worker.join(timeout=5)
        assert not worker.is_alive()


@pytest.mark.parametrize("entry", ["create", "switch", "command"])
def test_navigation_and_chat_finish_while_distillation_waits(client, backend, entry):
    session = seed_distillation(backend)
    target = backend._sessions.create()
    extractor = GatedExtractor()
    backend._service.extractor = extractor
    try:
        if entry == "create":
            assert client.post("/api/sessions").status_code == 201
        elif entry == "switch":
            assert client.post(f"/api/sessions/{target.id}/switch").status_code == 200
        else:
            response = client.post("/api/commands", json={"line": "/new", "session_id": session.id})
            assert response.json()["intent"] == "new_session"
        assert extractor.entered.wait(timeout=3)
        assert client.get("/api/health").json()["busy"] is False
        assert client.post(f"/api/sessions/{target.id}/switch").status_code == 200
        assert client.post("/api/sessions").status_code == 201
        assert "done" in types(collect(client, "synthetic next conversation"))
        assert extractor.calls == 1
    finally:
        extractor.release.set()
    wait_for_distillation(backend)
    loaded = backend._sessions.load(session.id)
    assert loaded.user_texts_after_distilled() == []
    assert [row["up_to"] for row in loaded.records if row["type"] == "distilled"] == ["m-seed-2"]
    assert extractor.calls == 1


def test_background_does_not_publish_stale_snapshot_after_resuming(client, backend):
    session = seed_distillation(backend)
    extractor = GatedExtractor()
    backend._service.extractor = extractor
    try:
        assert client.post("/api/sessions").status_code == 201
        assert extractor.entered.wait(timeout=3)
        assert client.post(f"/api/sessions/{session.id}/switch").status_code == 200
        assert "done" in types(collect(client, "new synthetic message", session.id))
    finally:
        extractor.release.set()
    wait_for_distillation(backend)
    assert len(backend._sessions.load(session.id).undistilled_messages()) == 4
    assert client.post("/api/sessions").status_code == 201
    wait_for_distillation(backend)
    loaded = backend._sessions.load(session.id)
    assert not loaded.undistilled_messages()
    assert [row["up_to"] for row in loaded.records if row["type"] == "distilled"] == [
        loaded.last_user_message_id()
    ]


def test_background_failure_leaves_cursor_for_startup_catchup(client, backend):
    session = seed_distillation(backend)
    extractor = GatedExtractor(fail=True)
    backend._service.extractor = extractor
    extractor.release.set()
    assert client.post("/api/sessions").status_code == 201
    wait_for_distillation(backend)
    loaded = backend._sessions.load(session.id)
    assert any(row["type"] == "extraction_error" for row in loaded.records)
    assert len(loaded.undistilled_messages()) == 3
    backend._service.extractor = StaticExtractor(ExtractionResult(memories=[]))
    assert backend._app_service.startup_catchup()["consolidated"] == 1
    assert backend._app_service.startup_catchup()["consolidated"] == 0


def test_shutdown_stops_background_publication_and_keeps_client_lock(client, backend):
    session = seed_distillation(backend)
    extractor = GatedExtractor()
    backend._service.extractor = extractor
    assert client.post("/api/sessions").status_code == 201
    try:
        assert extractor.entered.wait(timeout=3)

        def release_on_shutdown():
            assert backend._distill_stop.wait(timeout=3)
            assert backend._client_lock.locked
            assert client.post("/api/sessions").status_code == 503
            extractor.release.set()

        with ThreadPoolExecutor() as pool:
            future = pool.submit(release_on_shutdown)
            backend.shutdown()
            future.result(timeout=5)
    finally:
        extractor.release.set()
    assert not backend._client_lock.locked
    assert len(backend._sessions.load(session.id).undistilled_messages()) == 3


def test_chat_rejects_stale_empty_view(client, backend):
    session = backend._sessions.create()
    assert client.post(f"/api/sessions/{session.id}/switch").status_code == 200
    response = client.post("/api/chat", json={"message": "synthetic", "session_id": None})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_mismatch"
    assert not backend._sessions.load(session.id).messages


def test_stopping_runtime_catchup_does_not_write_after_release(backend):
    session = seed_distillation(backend)
    backend._conversation.switch(None)
    extractor = GatedExtractor(fail=True)
    backend._service.extractor = extractor

    with ThreadPoolExecutor(max_workers=2) as pool:
        catchup = pool.submit(backend._runtime.consolidate_pending)
        try:
            assert extractor.entered.wait(timeout=3)
            shutdown = pool.submit(backend._runtime.stop_catchup)
            assert backend._runtime._catchup_stopped.wait(timeout=3)
            assert not shutdown.done()
        finally:
            extractor.release.set()
        shutdown.result(timeout=5)
        catchup.result(timeout=5)

    loaded = backend._sessions.load(session.id)
    assert len(loaded.undistilled_messages()) == 3
    assert not any(row["type"] == "extraction_error" for row in loaded.records)
    assert backend._runtime.consolidate_pending()["scanned"] == 0


def test_empty_view_accepts_first_message_after_new_and_refresh(client, backend):
    previous = collect(client, "synthetic first")[-1]["session_id"]
    assert previous
    response = client.post("/api/commands", json={"line": "/new", "session_id": previous})
    assert response.json()["status"] == "ok"
    assert not any(row["current"] for row in client.get("/api/sessions").json()["items"])
    fresh = collect(client, "synthetic after refresh", None)[-1]["session_id"]
    assert fresh and fresh != previous
    assert [row["content"] for row in backend._sessions.load(fresh).messages] == [
        "synthetic after refresh"
    ]
