"""Slash-command compatibility tests for the Web adapter.

Everything runs against an isolated Vault and a fake model: no real API keys,
no network, no reads of the user's configuration or memory files.
"""

from __future__ import annotations

import json
from threading import Event
from uuid import uuid4

import keyring
import pytest
from fastapi.testclient import TestClient

from syvrio.agent.context import ContextManager
from syvrio.app.command_catalog import SLASH_COMMANDS as TUI_COMMANDS
from syvrio.app.command_catalog import SLASH_SUBCOMMANDS as TUI_SUBCOMMANDS
from syvrio.app.web.backend import WebBackend
from syvrio.app.web.server import create_app
from syvrio.application.commands import (
    CLIENT,
    SERVER,
    UNSUPPORTED,
    CommandUseCases,
    command_names,
    slash_matches,
    web_command_catalog,
)
from syvrio.application.contracts import ModelOption, SettingsResult
from syvrio.application.conversation import SAFE_COMMANDS
from syvrio.application.settings import SettingsUseCases
from syvrio.infra.config import (
    ChatConfig,
    Config,
    LLMConfig,
    RetrievalConfig,
    SearchConfig,
)
from syvrio.llm import AssistantTurn, LiteLLMChatModel, ToolCall
from syvrio.memory.extractor import StaticExtractor
from syvrio.memory.models import ExtractionResult, PendingItem
from syvrio.memory.service import MemoryService
from syvrio.time import utc_timestamp

pytestmark = pytest.mark.web

BASE_URL = "http://127.0.0.1:8765"


class FakeModel(LiteLLMChatModel):
    def __init__(self):
        super().__init__(LLMConfig())
        self.calls = 0

    def chat(self, messages, tools, on_delta=None, cancel=None, *, on_reasoning=None):
        self.calls += 1
        return AssistantTurn(content="(unused)")


@pytest.fixture
def backend(tmp_path, monkeypatch):
    # /model walks the provider presets, which would otherwise query the real
    # system credential store. Tests must stay fully isolated.
    monkeypatch.setattr(keyring, "get_password", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        SettingsUseCases,
        "model_options",
        lambda self, *args, **kwargs: [ModelOption("custom", "fake", "Fake model", current=True)],
    )
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
    owner._runtime.model = FakeModel()
    owner.startup()
    yield owner
    owner.shutdown()


@pytest.fixture
def client(backend):
    return TestClient(create_app(backend, autostart=False), base_url=BASE_URL)


def run(client, line, **extra):
    payload = {"line": line, **extra}
    return client.post("/api/commands", json=payload)


# ---------- shared catalog ----------


def test_tui_catalog_is_a_thin_forward_of_the_shared_definition():
    from syvrio.application.commands import SLASH_COMMANDS, SLASH_SUBCOMMANDS

    assert list(SLASH_COMMANDS) == list(TUI_COMMANDS)
    assert dict(SLASH_SUBCOMMANDS) == dict(TUI_SUBCOMMANDS)
    # The TUI passes the text after the slash (chat_app.py: palette.open(value[1:])).
    assert slash_matches("mod") == [row for row in TUI_COMMANDS if row[0].startswith("mod")]
    assert slash_matches("model re")[0][0] == "model refresh"


def test_every_command_has_a_capability_marker(client):
    rows = client.get("/api/commands").json()["items"]
    assert {row["name"] for row in rows} == set(command_names())
    assert len(rows) == len(web_command_catalog())
    assert {row["support"] for row in rows} <= {SERVER, CLIENT, UNSUPPORTED}
    by_name = {row["name"]: row for row in rows}
    assert by_name["import"]["support"] == CLIENT
    assert by_name["import"]["usage"] == "/import"
    assert by_name["setup"]["support"] == CLIENT
    assert by_name["setup"]["intent"] == "open_settings"
    assert by_name["exit"]["support"] == CLIENT
    assert by_name["quit"]["support"] == CLIENT
    assert by_name["undo"]["confirm"] is False
    # Sub-commands travel with the row so the browser never owns a second list.
    assert by_name["model"]["subcommands"][0]["name"] == "refresh"


def test_web_busy_policy_stays_within_tui_safe_commands():
    safe = {name.lstrip("/") for name in SAFE_COMMANDS}
    # /undo is the one deliberate addition: in the Web it also cancels the turn.
    assert CommandUseCases.busy_safe() - {"undo"} <= safe
    assert "undo" in CommandUseCases.busy_safe()


# ---------- routing and safety ----------


def test_unknown_command_is_never_forwarded_to_the_llm(client, backend):
    response = run(client, "/this-does-not-exist")
    assert response.status_code == 200
    assert response.json()["status"] == "unknown"
    assert "available commands" in response.json()["message"]
    assert backend._runtime.model.calls == 0


def test_pending_review_preserves_full_text_and_requires_confirmation(client, backend):
    service = backend._app_service.memory.service
    original = ("I am testing the synthetic review text. " * 60).rstrip()
    service.queue.add(
        PendingItem(
            id="p-example",
            created_at=utc_timestamp(),
            text=original,
            topic="preferences",
            question="Which preference applies?",
        )
    )
    rows = run(client, "/pending").json()["data"]["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert (row["id"], row["text"], row["topic"]) == ("p-example", original, "preferences")
    assert row["question"] == "Which preference applies?"
    assert row["related_ids"] == []
    assert row.get("session_id") is None and row.get("message_id") is None
    for confirmed in (False, True):
        editor = run(client, "/pending edit p-example", confirmed=confirmed).json()
        assert editor["status"] == "ok" and editor["data"]["edit_id"] == "p-example"
        assert editor["data"]["rows"][0]["text"] == original
        assert len(service.pending()) == 1 and not service.current_facts()
    assert run(client, "/pending edit missing").json()["status"] == "error"

    edited = "I revised the synthetic review text. " * 60 + "\n\nA separate note."
    response = run(client, "/pending edit p-example", pending_text=edited).json()
    assert response["status"] == "confirm_required"
    assert edited not in response["message"]
    assert len(service.pending()) == 1
    assert run(client, "/status", pending_text=edited).status_code == 422

    response = run(client, "/pending edit p-example", pending_text=edited, confirmed=True).json()
    assert response["status"] == "ok", response
    assert response["data"]["rows"] == []
    assert not service.pending()
    assert [entry.text for entry in service.store.entries()] == [edited]


def test_review_covers_candidates_and_inferences_beyond_twenty(client, backend):
    from syvrio.memory.models import MemoryDraft

    service = backend._app_service.memory.service
    source = service.add(MemoryDraft(text="Synthetic evidence", topic="review"))
    candidates, inferences = set(), set()
    for index in range(23):
        draft = MemoryDraft(text=f"Review candidate {index}", topic="review")
        candidates.add(service.create_pending(draft, "Keep this?", []).id)
        inferences.add(
            service.propose_inference(f"Inference {index}", [source.id], kind="profile").id
        )
    for command, expected in (("pending", candidates), ("inferences", inferences)):
        data = run(client, f"/{command}").json()["data"]
        assert {r["id"] for r in data["rows"]} == expected
        assert data["count"] == len(expected) == 23


@pytest.mark.parametrize("kind", ["pending", "inferences"])
def test_review_receipt_failure_preserves_decision_for_retry(client, backend, monkeypatch, kind):
    from syvrio.memory.models import MemoryDraft

    memory = backend._app_service.memory
    service = memory.service
    if kind == "pending":
        item = service.create_pending(
            MemoryDraft(text="Synthetic draft", topic="review"), "Keep it?", []
        )
        command, load = f"/pending reject {item.id}", "pending"
    else:
        source = service.note("Synthetic source")
        item = service.propose_inference("Synthetic hypothesis", [source.id], kind="profile")
        command, load = f"/inferences confirm {item.id}", "_sources"

    def unavailable(*args):
        raise OSError("Synthetic receipt failure")

    with monkeypatch.context() as patch:
        patch.setattr(memory, load, unavailable)
        assert run(client, command, confirmed=True).json()["status"] == "error"
    if kind == "pending":
        assert service.pending() == [item]
        assert not service.queue.resolved_path.exists()
    else:
        assert service.list_inferences() == [item]
    assert run(client, command, confirmed=True).json()["status"] == "ok"


def test_inference_command_edit_keeps_full_text_and_requires_confirmation(client, backend):
    service = backend._app_service.memory.service
    source = service.note("Synthetic reading preference.")
    original = service.propose_inference("Synthetic hypothesis. " * 20, [source.id], kind="profile")
    assert run(client, "/inferences").json()["data"]["rows"][0]["text"] == original.text
    line = f"/inferences correct {original.id}"
    edited = "Edited synthetic hypothesis. " * 100 + "\nA second line."
    response = run(client, line, inference_text=edited).json()
    assert response["status"] == "confirm_required"
    assert edited not in response["message"]
    assert service.list_inferences() == [original]
    assert run(client, "/status", inference_text=edited).status_code == 422
    response = run(client, line, inference_text=edited, confirmed=True).json()
    assert response["status"] == "ok", response
    assert response["message"] == "Edit saved as a confirmed inference."
    rows = {row["status"]: row for row in response["data"]["rows"]}
    assert (rows["deprecated"]["id"], rows["deprecated"]["text"]) == (original.id, original.text)
    assert rows["confirmed"]["id"] != original.id and rows["confirmed"]["text"] == edited
    assert set(row["id"] for row in rows.values()) == {
        item.id for item in service.list_inferences()
    }


def test_chat_endpoint_rejects_slash_lines(client, backend):
    response = client.post("/api/chat", json={"message": "/new"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "command_required"
    assert backend._runtime.model.calls == 0


def test_import_never_reads_a_browser_supplied_path(client, tmp_path):
    secret = tmp_path / "private.txt"
    secret.write_text("SENTINEL-DO-NOT-LEAK", encoding="utf-8")
    body = run(client, f"/import {secret} --apply").json()
    assert body["status"] == "error"
    assert run(client, "/import").json()["intent"] == "pick_attachments"
    assert "SENTINEL-DO-NOT-LEAK" not in str(body)
    assert str(secret) not in str(body)
    setup = run(client, "/setup").json()
    assert setup["status"] == "client"
    assert setup["intent"] == "open_settings"


def test_undo_runs_without_a_confirmation_request(client):
    result = run(client, "/undo").json()
    assert result["status"] == "ok"
    assert result["intent"] == "undo"
    assert result["data"]["restored"] is None


def test_exit_is_client_only_and_never_stops_the_server(client):
    body = run(client, "/exit").json()
    assert body["status"] == "client"
    assert body["intent"] == "leave"
    assert "server" in body["message"].lower() or "entry" in body["message"].lower()
    assert client.get("/api/health").status_code == 200
    assert run(client, "/quit").json()["intent"] == "leave"


def test_help_lists_commands_and_browser_shortcut_notes(client):
    body = run(client, "/help").json()
    assert body["status"] == "ok"
    assert "/import" in body["message"]
    assert "/import <path>" not in body["message"]
    for name in ("/new", "/model", "/undo"):
        assert name in body["message"]
    assert "Ctrl+Shift+P" in body["message"]
    assert "Ctrl+P" in body["message"]


def test_session_mismatch_rejects_a_stale_tab(client, backend):
    current = backend._sessions.create().id
    client.post(f"/api/sessions/{current}/switch")
    response = run(client, "/status", session_id="another-session")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_mismatch"
    assert backend.active_turn_id() is None


def test_command_api_rejects_cross_site_writes(client):
    response = client.post(
        "/api/commands",
        json={"line": "/status"},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "untrusted_origin"


# ---------- busy policy ----------


def test_running_tool_turn_keeps_settings_and_queued_and_new_sessions_pick_up_changes(
    client, backend, monkeypatch
):
    config = backend._service.config
    old_endpoint, new_endpoint = "https://old.example.invalid/v1", "https://new.example.invalid/v1"
    config.llm = LLMConfig(
        provider="spark", model="first-model", spark_thinking="enabled", base_url=old_endpoint
    )
    config.chat.context_window = 96000
    backend._runtime.reload_llm(config)
    monkeypatch.setattr("syvrio.application.settings.model_metadata", lambda _: {})
    monkeypatch.setattr(
        SettingsUseCases, "_reasoning_values", staticmethod(lambda _: ("enabled", "disabled"))
    )
    entered, release = Event(), Event()
    calls, budgets = [], {}
    prepare = ContextManager.prepare

    def record_budget(manager, session, system_prompt, current_text, **kwargs):
        budgets[current_text] = manager.config.context_window
        return prepare(manager, session, system_prompt, current_text, **kwargs)

    def respond(model, messages, tools, on_delta=None, cancel=None, *, on_reasoning=None):
        text = next(msg["content"] for msg in reversed(messages) if msg["role"] == "user")
        calls.append((text, model.config.model, model.config.spark_thinking, model.config.base_url))
        if text == "running" and messages[-1]["role"] == "user":
            entered.set()
            assert release.wait(15)
            return AssistantTurn(tool_calls=[ToolCall("local", "recall", {"query": "synthetic"})])
        return AssistantTurn(content="ok")

    monkeypatch.setattr(ContextManager, "prepare", record_budget)
    monkeypatch.setattr(LiteLLMChatModel, "chat", respond)
    view = {"X-Syvrio-View": str(uuid4())}

    def assert_finished(response):
        response.raise_for_status()
        events = [
            json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")
        ]
        assert events[-1]["type"] == "done", events[-1]

    running = client.post("/api/chat?receipt=1", headers=view, json={"message": "running"}).json()
    sid = running["session_id"]
    try:
        assert entered.wait(5)
        queued = client.post(
            "/api/chat?receipt=1", headers=view, json={"message": "queued", "session_id": sid}
        ).json()
        for command in ("/model spark/second-model", "/think disabled", "/context 64000"):
            result = client.post(
                "/api/commands", headers=view, json={"line": command, "session_id": sid}
            ).json()
            assert result["status"] == "ok", result
        changed = client.post(
            "/api/settings/providers/spark",
            headers=view,
            json={"action": "set_endpoint", "endpoint": new_endpoint},
        )
        changed.raise_for_status()
        assert changed.json()["ok"]
        meter = client.get("/api/context-usage", headers=view, params={"session_id": sid})
        assert meter.json()["limit"] == 96000
        other = client.post(
            "/api/chat", headers={"X-Syvrio-View": str(uuid4())}, json={"message": "fresh"}
        )
        assert_finished(other)
    finally:
        release.set()
    for turn in (running, queued):
        assert_finished(client.get(f"/api/chat/turns/{turn['turn_id']}", headers=view))
    assert calls == [
        ("running", "first-model", "enabled", old_endpoint),
        ("fresh", "second-model", "disabled", new_endpoint),
        ("running", "first-model", "enabled", old_endpoint),
        ("queued", "second-model", "disabled", new_endpoint),
    ]
    assert budgets == {"running": 96000, "queued": 64000, "fresh": 64000}
    assert (
        client.get("/api/context-usage", headers=view, params={"session_id": sid}).json()["limit"]
        == 64000
    )


def test_busy_allows_read_only_commands_and_blocks_mutating_ones(backend):
    commands = backend._commands
    assert commands.execute("/status", busy=True).status == "ok"
    assert commands.execute("/help", busy=True).status == "ok"
    assert commands.execute("/model", busy=True).status == "ok"
    assert commands.execute("/new", busy=True).status == "busy"
    assert commands.execute("/model custom/fake", busy=True).status == "ok"
    assert commands.execute("/context 54321", busy=True).status == "ok"
    assert commands.execute("/compact", busy=True).status == "busy"


def test_busy_blocks_mutating_forms_of_otherwise_safe_names(backend):
    commands = backend._commands
    # The name alone is read-only-ish, but these forms write data or run jobs and
    # must not run concurrently with a streaming reply.
    for line in (
        "/dream 1",
        "/model refresh",
        "/dream resume",
        "/git sync",
        "/pending approve x",
        "/inferences confirm x",
    ):
        assert commands.execute(line, busy=True, confirmed=True).status == "busy", line
    # Read-only forms of the same names stay available (even if dispatch errors,
    # the busy gate must not be what rejects them).
    assert commands.execute("/dream status", busy=True).status != "busy"
    assert commands.execute("/pending", busy=True).status != "busy"
    assert commands.execute("/git", busy=True).status != "busy"
    assert commands.execute("/undo", busy=True, confirmed=True).status != "busy"


def test_is_mutating_distinguishes_command_forms():
    assert CommandUseCases.is_mutating("dream", "1") is True
    assert CommandUseCases.is_mutating("dream", "status") is False
    assert CommandUseCases.is_mutating("dream", "cancel") is False
    assert CommandUseCases.is_mutating("git", "sync") is True
    assert CommandUseCases.is_mutating("git", "") is False
    assert CommandUseCases.is_mutating("pending", "approve x") is True
    assert CommandUseCases.is_mutating("pending", "") is False
    assert CommandUseCases.is_mutating("model", "refresh") is True
    assert CommandUseCases.is_mutating("model", "") is False
    assert CommandUseCases.is_mutating("undo", "") is True


# ---------- option panels ----------


def test_listing_produces_options_but_applying_does_not(client):
    listing = run(client, "/model").json()
    assert listing["status"] == "ok"
    assert isinstance(listing["data"].get("options"), list)
    # An applied choice must not reopen the picker in the browser.
    applied = run(client, "/model no-such-provider/no-such-model").json()
    assert applied["status"] == "error"
    assert "options" not in (applied["data"] or {})
    assert run(client, "/think impossible").json()["status"] == "error"
    assert "options" not in (run(client, "/think impossible").json()["data"] or {})
    assert run(client, "/context abc").json()["status"] == "error"


def test_empty_model_catalog_still_lists_the_current_model(client, monkeypatch):
    # A cold process cache (or a provider without /models) previously produced
    # "Models:\n\n\nSelect with ..." with no explanation at all.
    monkeypatch.setattr(SettingsUseCases, "model_options", lambda self, *a, **k: [])
    body = run(client, "/model").json()
    assert body["status"] == "ok"
    assert "Current model: custom/fake" in body["message"]
    assert "Models:\n\n" not in body["message"]
    assert body["data"]["options"][0] == {
        "provider": "custom",
        "model": "fake",
        "label": "custom/fake",
        "current": True,
    }
    assert "No cached model list" in body["message"]


def test_model_refresh_reports_an_empty_provider_catalog(client, monkeypatch):
    monkeypatch.setattr(SettingsUseCases, "model_catalog", lambda self, *a, **k: {"custom": []})
    monkeypatch.setattr(SettingsUseCases, "model_options", lambda self, *a, **k: [])
    body = run(client, "/model refresh").json()
    assert body["status"] == "ok"
    assert "local terminal" not in body["message"]
    assert "did not return a model list" in body["message"]
    assert body["data"]["options"][0]["model"] == "fake"


def test_explicit_model_id_is_switchable_without_a_catalog(client, monkeypatch):
    monkeypatch.setattr(SettingsUseCases, "model_options", lambda self, *a, **k: [])
    monkeypatch.setattr(
        SettingsUseCases,
        "apply_model",
        lambda self, provider, model: SettingsResult(True, f"Model switched: {provider}/{model}"),
    )
    body = run(client, "/model custom/other-model").json()
    assert body["status"] == "ok"
    assert "Model switched: custom/other-model" in body["message"]
    # Applying a choice must not reopen the picker.
    assert "options" not in (body["data"] or {})


def test_new_session_command_only_opens_the_empty_view(client, backend):
    backend._sessions.create()
    before = len(backend._sessions.list())
    body = run(client, "/new").json()
    assert body["status"] == "ok"
    # Reuses the runtime's lazy start: nothing is written to disk here.
    assert body["session_id"] is None
    assert body["refresh_sessions"] is True
    assert body["refresh_history"] is True
    assert len(backend._sessions.list()) == before
    assert not any(item["current"] for item in client.get("/api/sessions").json()["items"])
