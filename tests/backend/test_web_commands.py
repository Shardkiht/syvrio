"""网页适配器的斜杠命令兼容性测试。

全部测试使用独立 Vault 和模拟模型，无需真实密钥或联网，
也不读取用户的配置和记忆文件。
"""

from __future__ import annotations

import json
from threading import Event
from uuid import uuid4

import keyring
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from syvrio.agent.context import ContextManager
from syvrio.application.commands import (
    CLIENT,
    SERVER,
    UNSUPPORTED,
    CommandUseCases,
    command_names,
    web_command_catalog,
)
from syvrio.application.contracts import ModelOption, SettingsResult
from syvrio.application.conversation import SAFE_COMMANDS
from syvrio.application.settings import SettingsUseCases
from syvrio.backend.api.channel import APIChannel as WebBackend
from syvrio.backend.client_contracts import SLASH_COMMANDS, slash_matches
from syvrio.infra.config import (
    ChatConfig,
    Config,
    LLMConfig,
    RetrievalConfig,
    SearchConfig,
)
from syvrio.infra.llm import AssistantTurn, LiteLLMChatModel, ToolCall
from syvrio.infra.time import utc_timestamp
from syvrio.memory.models import PendingItem
from syvrio.memory.service import MemoryService
from tests.support.resident_api import create_app

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
    # /model 会遍历供应商预设，通常会查询系统凭据存储。测试中须隔离此访问。
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
    service = MemoryService(config)
    service.initialize_files()
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


# 共享命令目录


def test_command_completion_matches_the_shared_catalog():
    assert slash_matches("mod") == [row for row in SLASH_COMMANDS if row[0].startswith("mod")]
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
    # 子命令随目录条目下发，使浏览器可以共用同一份列表。
    assert by_name["model"]["subcommands"][0]["name"] == "refresh"


def test_web_busy_policy_stays_within_tui_safe_commands():
    safe = {name.lstrip("/") for name in SAFE_COMMANDS}
    # 网页端专门补充了 /undo，用于同时取消当前回合。
    assert CommandUseCases.busy_safe() - {"undo"} <= safe
    assert "undo" in CommandUseCases.busy_safe()


# 路由与安全


def test_unknown_command_is_never_forwarded_to_the_llm(client, backend):
    response = run(client, "/this-does-not-exist")
    assert response.status_code == 200
    assert response.json()["status"] == "unknown"
    assert response.json()["message"] == "Invalid command."
    assert backend._runtime.model.calls == 0


def test_topic_directory_returns_metadata_and_opens_each_exact_path(client, backend, monkeypatch):
    assert run(client, "/topics").json()["data"] == {"count": 0, "topics": []}
    service = backend._app_service.memory.service
    first = service.note("A synthetic reading note.\nWith a second line.", topic="general/reading")
    second = service.note("A separate synthetic reading note.", topic="projects/reading")
    service.deprecate(service.note("An old synthetic note.", topic="general/reading").id)

    parsed = []
    original = service.store.entries

    def count_parse(topic):
        parsed.append(topic)
        return original(topic)

    monkeypatch.setattr(service.store, "entries", count_parse)
    listing = run(client, "/topics").json()
    assert parsed == []
    assert listing["status"] == "ok"
    data = listing["data"]
    assert data["count"] == 2
    assert [row["topic"] for row in data["topics"]] == [first.topic, second.topic]
    for row, fact in zip(data["topics"], (first, second), strict=True):
        assert row["title"] == "reading" and row["updated"]
        detail = run(client, f"/topics {row['topic']}").json()
        assert parsed == [row["topic"]]
        parsed.clear()
        assert detail["status"] == "ok"
        expected = fact.as_recall().model_dump(exclude_none=True)
        assert detail["data"] == {"topic": expected["topic"], "entries": [expected]}


def test_dream_exposes_only_run_and_status_and_manual_run_advances_retry(client, backend):
    background = backend._background
    background.stop()
    background.service.config.chat.default_budget = 7
    menu = run(client, "/dream status").json()
    assert menu["data"]["state"] == "idle"
    assert menu["data"]["error_type"] is None and menu["data"]["failures"] == 0
    assert menu["data"]["rows"] == [{"id": "run", "text": "Run Dream"}]
    catalog = client.get("/api/commands").json()["items"]
    dream = next(row for row in catalog if row["name"] == "dream")
    assert {item["name"] for item in dream["subcommands"]} == {"run", "status"}
    assert run(client, "/dream run").json()["status"] == "confirm_required"
    confirmation = run(client, "/dream").json()
    assert confirmation["status"] == "confirm_required"
    assert confirmation["message"] == (
        "Use your saved memories to update your profile and propose hypotheses for your review?"
    )
    assert background.dream_status()["state"] == "idle"
    submitted = run(client, "/dream", confirmed=True).json()
    assert submitted["status"] == "ok" and submitted["data"]["state"] == "queued"
    assert "queued" in submitted["message"]
    assert background.dream_status()["remaining_requests"] == 7

    job = background.dream.status()
    background.dream.failed(TimeoutError(), job["id"], job["generation"])
    retry = background.dream.status()
    background._signals["dream"].clear()
    for line in ("/dream", "/dream run"):
        assert run(client, line).json()["status"] == "confirm_required"
    menu = run(client, "/dream status").json()["data"]
    assert menu["error_type"] == "TimeoutError" and menu["failures"] == 1
    assert menu["error_at"] == retry["updated_at"]
    assert menu["retry_at"] == retry["retry_at"] and background.dream.status() == retry
    assert menu["rows"] == [{"id": "run", "text": "Run Dream"}]
    assert not background._signals["dream"].is_set()
    override = run(client, "/dream run", confirmed=True).json()
    assert background._signals["dream"].is_set()
    assert override["data"]["retry_at"] is None and "retry now" in override["message"]
    assert background.dream.status() == {**retry, "retry_at": 0}


def test_invalid_command_forms_are_rejected_before_confirmation_or_work(client, backend):
    background = backend._background
    background.stop()
    for line in (
        "/missing",
        "/dream 1",
        "/dream cancel",
        "/dream resume",
        "/dream typo",
        "/dream run 2",
        "/dream status extra",
        "/git sync extra",
        "/git typo",
        "/pending typo id",
        "/inferences typo id",
        "/new typo",
        "/status extra",
        "/setup typo",
        "/model typo",
        "/profile typo",
    ):
        for confirmed in (False, True):
            result = run(client, line, confirmed=confirmed).json()
            assert (result["status"], result["message"]) == ("unknown", "Invalid command."), line
            assert (
                backend._commands.execute(line, busy=True, confirmed=confirmed).status == "unknown"
            )
    assert background.dream.status()["state"] == "idle"
    assert backend._runtime.model.calls == 0


def test_manual_dream_retries_one_checkpoint_while_worker_applies_normal_backoff(
    client, backend, monkeypatch
):
    from syvrio.memory.background import dream

    background = backend._background
    background.service.config.chat.default_budget = 5
    background.service.note("Synthetic fact for a Dream retry.", topic="example")
    calls = []
    failed_once, failed_twice = Event(), Event()
    retrying, release = Event(), Event()
    record_failure = background.dream.failed
    # 此用例测重试检查点，假模型的输入和输出容量也固定，避免冷加载 SDK 占用等待时间。
    monkeypatch.setattr(dream, "effective_context_window", lambda _, configured: configured)
    monkeypatch.setattr(dream, "dream_output_ceiling", lambda _: dream.MAX_DREAM_OUTPUT_TOKENS)

    def signal_failure(error, job_id, generation):
        record_failure(error, job_id, generation)
        if background.dream.status()["failures"] == 1:
            failed_once.set()
        else:
            failed_twice.set()

    def generate(_, prompt, payload, schema):
        calls.append(prompt)
        if prompt == dream.PROFILE:
            return schema(summary="Synthetic profile.")
        if calls.count(dream.INFER) == 2:
            retrying.set()
            assert release.wait(8)
        raise TimeoutError("Synthetic retry")

    monkeypatch.setattr(background.dream, "failed", signal_failure)
    monkeypatch.setattr(dream, "ask", generate)
    assert run(client, "/dream", confirmed=True).json()["status"] == "ok"
    assert failed_once.wait(5)
    checkpoint = background.dream.status()
    assert checkpoint["phase"] == "hypotheses" and checkpoint["failures"] == 1
    assert not background.dream.due()
    background.schedule.tick(background.schedule.next_at())
    assert background.dream.status() == checkpoint
    try:
        override = run(client, "/dream", confirmed=True).json()
        assert override["data"]["retry_at"] is None and override["data"]["failures"] == 1
        assert retrying.wait(5)
        active = background.dream.status()
        for key in ("id", "generation", "phase", "batch", "failures"):
            assert active[key] == checkpoint[key]
        assert active["requests"] == checkpoint["requests"] + 1
        assert active["remaining_requests"] == checkpoint["remaining_requests"] - 1
        again = run(client, "/dream run", confirmed=True).json()
        assert again["data"]["state"] == "running" and "already running" in again["message"]
        assert run(client, "/dream status").json()["data"]["state"] == "running"
        assert background.dream.status() == active
    finally:
        release.set()
    assert failed_twice.wait(5)
    failed = background.dream.status()
    assert failed["failures"] == 2
    assert failed["retry_at"] > checkpoint["retry_at"] and not background.dream.due()
    assert calls == [dream.PROFILE, dream.INFER, dream.INFER]


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (ValidationError.from_exception_data("Synthetic private response", []), "ValidationError"),
        (RuntimeError("Synthetic private response"), "DreamError"),
    ],
)
def test_dream_failed_status_exposes_only_safe_error_details(client, backend, error, category):
    background = backend._background
    background.stop()
    run(client, "/dream", confirmed=True)
    job = background.dream.status()
    for _ in range(3):
        background.dream.failed(error, job["id"], job["generation"])
    receipt = run(client, "/dream status").json()
    status = receipt["data"]
    assert status["state"] == "failed" and status["retry_at"] is None
    assert status["error_type"] == category and status["failures"] == 3
    assert status["error_at"] == background.dream.status()["updated_at"]
    assert category in receipt["message"] and "Synthetic private response" not in str(receipt)


def test_dream_zero_budget_pauses_and_resume_keeps_its_checkpoint(client, backend):
    background = backend._background
    background.stop()
    config = background.service.config.chat
    config.default_budget = 0
    paused = run(client, "/dream run", confirmed=True).json()
    assert paused["data"]["state"] == "paused"
    assert "dream.default_budget is 0" in paused["message"] and "/dream" not in paused["message"]
    checkpoint = background.dream.status()
    assert checkpoint["remaining_requests"] == 0
    menu = run(client, "/dream status").json()
    assert menu["data"]["rows"] == []
    assert menu["data"]["pause_reason"] == "budget_disabled" and not menu["data"]["can_run"]
    assert menu["data"]["run_hint"] in menu["message"]
    assert run(client, "/dream", confirmed=True).json()["data"]["state"] == "paused"
    assert background.dream.status() == checkpoint
    config.default_budget = 3
    menu = run(client, "/dream status").json()["data"]
    assert menu["pause_reason"] == "budget_exhausted" and menu["can_run"]
    assert menu["rows"] == [{"id": "run", "text": "Run Dream"}]
    assert run(client, "/dream run", confirmed=True).json()["data"]["state"] == "queued"
    resumed = background.dream.status()
    assert (resumed["id"], resumed["phase"]) == (checkpoint["id"], checkpoint["phase"])
    assert resumed["remaining_requests"] == 3


def test_dream_status_describes_paused_truncated_output(client, backend, monkeypatch):
    background = backend._background
    status = {
        **background.dream_status(),
        "state": "paused",
        "phase": "profile",
        "pause_reason": "output_limit",
        "error": "DreamOutputTruncated",
        "failures": 1,
        "updated_at": utc_timestamp(),
    }
    monkeypatch.setattr(background, "dream_status", lambda: status)
    receipt = run(client, "/dream status").json()
    assert receipt["data"]["error_type"] == "DreamOutputTruncated"
    assert receipt["data"]["retry_at"] is None
    assert "output limit" in receipt["message"] and "current batch" in receipt["message"]


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
        command, load = f"/inferences confirm {item.id}", "load_inference_review"

    def unavailable(*args, **kwargs):
        raise OSError("Synthetic receipt failure")

    with monkeypatch.context() as patch:
        patch.setattr(memory, load, unavailable)
        assert run(client, command, confirmed=True).json()["status"] == "error"
    if kind == "pending":
        assert service.pending() == [item]
        from tests.support.storage import rejected

        assert not rejected(service.queue)
    else:
        assert service.list_inferences() == [item]
    assert run(client, command, confirmed=True).json()["status"] == "ok"


def test_inference_list_and_review_receipt_read_only_the_displayed_data(
    client, backend, monkeypatch
):
    memory = backend._app_service.memory
    source = memory.service.note("Synthetic source")
    item = memory.service.propose_inference("Synthetic inference", [source.id], kind="profile")

    def unexpected_fact_read(*args, **kwargs):
        pytest.fail("Inference list and confirmation must not read unused source facts")

    monkeypatch.setattr(memory.service.store, "entries", unexpected_fact_read)
    assert run(client, "/inferences").json()["data"]["count"] == 1
    result = run(client, f"/inferences confirm {item.id}", confirmed=True).json()
    assert result["status"] == "ok" and result["data"]["rows"][0]["status"] == "confirmed"


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


def test_review_actions_reject_extra_text_except_when_editing(client, backend):
    from syvrio.memory.models import MemoryDraft

    service = backend._app_service.memory.service
    candidate = service.create_pending(
        MemoryDraft(text="Synthetic candidate", topic="review"), "Keep?", []
    )
    source = service.note("Synthetic evidence.")
    inference = service.propose_inference("Synthetic hypothesis", [source.id], kind="profile")
    for line in (
        f"/pending approve {candidate.id} extra",
        f"/pending reject {candidate.id} extra",
        f"/inferences confirm {inference.id} extra",
        f"/inferences reject {inference.id}\nextra",
    ):
        for confirmed in (False, True):
            result = run(client, line, confirmed=confirmed).json()
            assert (result["status"], result["message"]) == ("unknown", "Invalid command."), line
    assert service.pending() == [candidate]
    assert service.current_facts() == [source]
    assert service.list_inferences() == [inference]

    revised = "Revised synthetic text.\nSecond line with spaces."
    for line in (
        f"/pending edit {candidate.id} {revised}",
        f"/inferences edit {inference.id} {revised}",
    ):
        assert run(client, line).json()["status"] == "confirm_required"
        assert run(client, line, confirmed=True).json()["status"] == "ok"
    assert any(entry.text == revised for entry in service.current_facts())
    assert any(
        item.status == "confirmed" and item.text == revised for item in service.list_inferences()
    )


def test_chat_endpoint_rejects_slash_lines(client, backend):
    response = client.post("/api/chat", json={"message": "/new"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "command_required"
    assert backend._runtime.model.calls == 0


def test_import_never_reads_a_browser_supplied_path(client, tmp_path):
    secret = tmp_path / "private.txt"
    secret.write_text("SENTINEL-DO-NOT-LEAK", encoding="utf-8")
    body = run(client, f"/import {secret} --apply").json()
    assert body["status"] == "unknown"
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


def test_remove_key_keeps_provider_and_endpoint(client, monkeypatch, isolate_llm_credentials):
    path = "/api/settings/providers/dashscope"
    endpoint = "https://workspace.example.invalid/v1"
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-environment-value")
    assert client.post(path, json={"action": "set_endpoint", "endpoint": endpoint}).json()["ok"]
    assert client.post(path, json={"action": "set_key", "api_key": "synthetic-saved"}).json()["ok"]

    def provider():
        rows = client.get("/api/settings/providers").json()["items"]
        return next(row for row in rows if row["key"] == "dashscope")

    assert provider()["configured"] and provider()["can_remove_key"]
    assert client.post(path, json={"action": "remove_key"}).status_code == 409
    assert isolate_llm_credentials["DASHSCOPE_API_KEY"] == "synthetic-saved"
    result = client.post(path, json={"action": "remove_key", "confirmed": True}).json()
    assert result["ok"] and result["message"] == "百炼 API key deleted."
    row = provider()
    assert not row["configured"] and not row["can_remove_key"]
    assert row["endpoint"] == endpoint
    assert "DASHSCOPE_API_KEY" not in isolate_llm_credentials


# 忙碌期间的命令策略


def test_running_tool_turn_keeps_settings_and_queued_and_new_sessions_pick_up_changes(
    client, backend, monkeypatch
):
    config = backend._service.config
    old_endpoint, new_endpoint = "https://old.example.invalid/v1", "https://new.example.invalid/v1"
    config.llm = LLMConfig(
        provider="spark", model="first-model", spark_thinking="enabled", base_url=old_endpoint
    )
    config.chat.context_window = 96000
    config.chat.reserve_output_tokens = 6000
    config.chat.compact_threshold = 0.8
    backend._runtime.reload_llm(config)
    monkeypatch.setattr("syvrio.application.settings.model_metadata", lambda _: {})
    monkeypatch.setattr(
        "syvrio.application.settings.reasoning_values", lambda _: ("enabled", "disabled")
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
        assert meter.json()["compaction_trigger_tokens"] == 72000
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
    meter = client.get("/api/context-usage", headers=view, params={"session_id": sid}).json()
    assert meter["limit"] == 64000
    assert meter["compaction_trigger_tokens"] == 46400


def test_busy_allows_read_only_commands_and_blocks_mutating_ones(backend):
    commands = backend._commands
    result = commands.execute("/status", busy=True)
    assert result.status == "ok"
    assert result.data["status"]["generating"] is True
    assert isinstance(result.data["status"]["context_window"], int)
    assert "Generating: yes" in result.message
    assert commands.execute("/help", busy=True).status == "ok"
    assert commands.execute("/model", busy=True).status == "ok"
    assert commands.execute("/new", busy=True).status == "busy"
    assert commands.execute("/model custom/fake", busy=True).status == "ok"
    assert commands.execute("/context 54321", busy=True).status == "ok"
    assert commands.execute("/compact", busy=True).status == "busy"


def test_summary_offers_compaction_for_an_existing_idle_session(backend):
    commands = backend._commands
    commands.conversation.switch(None)
    assert commands.execute("/summary").data["can_compact"] is False
    commands.conversation.switch(backend._sessions.create().id)
    assert commands.execute("/summary").data["can_compact"] is True
    assert commands.execute("/summary", busy=True).data["can_compact"] is False


def test_busy_blocks_mutating_forms_of_otherwise_safe_names(backend):
    commands = backend._commands
    # 单看命令名可能以为只读，这些具体用法却会写数据或启动任务，
    # 因此不能与流式回复并发执行。
    for line in (
        "/dream",
        "/dream run",
        "/model refresh",
        "/git sync",
        "/pending approve x",
        "/inferences confirm x",
    ):
        assert commands.execute(line, busy=True, confirmed=True).status == "busy", line
    # 同名命令的只读用法在生成期间仍可使用。即使分发出错，
    # 也不应是忙碌检查拒绝了请求。
    assert commands.execute("/dream status", busy=True).status != "busy"
    assert commands.execute("/pending", busy=True).status != "busy"
    assert commands.execute("/git", busy=True).status != "busy"
    assert commands.execute("/undo", busy=True, confirmed=True).status != "busy"


def test_is_mutating_distinguishes_command_forms():
    assert CommandUseCases.is_mutating("dream", "run") is True
    assert CommandUseCases.is_mutating("dream", "status") is False
    assert CommandUseCases.is_mutating("git", "sync") is True
    assert CommandUseCases.is_mutating("git", "") is False
    assert CommandUseCases.is_mutating("pending", "approve x") is True
    assert CommandUseCases.is_mutating("pending", "") is False
    assert CommandUseCases.is_mutating("model", "refresh") is True
    assert CommandUseCases.is_mutating("model", "") is False
    assert CommandUseCases.is_mutating("undo", "") is True


# 选项面板


def test_listing_produces_options_but_applying_does_not(client):
    listing = run(client, "/model").json()
    assert listing["status"] == "ok"
    assert isinstance(listing["data"].get("options"), list)
    # 应用所选项后，浏览器不能再次打开选择器。
    applied = run(client, "/model no-such-provider/no-such-model").json()
    assert applied["status"] == "error"
    assert "options" not in (applied["data"] or {})
    assert run(client, "/think impossible").json()["status"] == "error"
    assert "options" not in (run(client, "/think impossible").json()["data"] or {})
    assert run(client, "/context abc").json()["status"] == "error"


def test_empty_model_catalog_still_lists_the_current_model(client, monkeypatch):
    # 过去进程缓存为空或供应商没有 /models 时，只会显示空的模型列表和选择提示，缺少解释。
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
    assert "No provider model list" in body["message"]


def test_model_refresh_reports_an_empty_provider_catalog(client, monkeypatch):
    monkeypatch.setattr(SettingsUseCases, "model_catalog", lambda self, *a, **k: {"custom": []})
    monkeypatch.setattr(SettingsUseCases, "model_options", lambda self, *a, **k: [])
    body = run(client, "/model refresh").json()
    assert body["status"] == "ok"
    assert "local terminal" not in body["message"]
    assert "did not return a model list" in body["message"]
    assert "did not return a model list" in body["data"]["refresh_message"]
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
    # 应用选项后应关闭选择器。
    assert "options" not in (body["data"] or {})


def test_new_session_command_only_opens_the_empty_view(client, backend):
    backend._sessions.create()
    before = len(backend._sessions.list())
    body = run(client, "/new").json()
    assert body["status"] == "ok"
    # 复用运行时的延迟创建机制，此处尚无数据落盘。
    assert body["session_id"] is None
    assert body["refresh_sessions"] is True
    assert body["refresh_history"] is True
    assert len(backend._sessions.list()) == before
    assert not any(item["current"] for item in client.get("/api/sessions").json()["items"])
