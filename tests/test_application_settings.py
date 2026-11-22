"""Application Settings 用例：不访问真实 Keyring/API。"""

from __future__ import annotations

import json
import tomllib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import httpx
import pytest

from syvrio.application import settings as settings_module
from syvrio.application.settings import SettingsUseCases
from syvrio.infra.config import Config, LLMConfig, RetrievalConfig, load_config
from syvrio.infra.providers import PRESETS
from syvrio.llm import fetch_provider_models
from syvrio.llm.client import effective_context_window
from syvrio.memory.service import MemoryService

pytestmark = pytest.mark.config


class DummyRuntime:
    def __init__(self, service):
        self.service = service
        self.reloads = 0

    @property
    def model_label(self):
        return f"{self.service.config.llm.provider}/{self.service.config.llm.model}"

    def reload_llm(self, config=None):
        self.reloads += 1


def build(tmp_path):
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(provider="deepseek", model="deepseek-flash"),
        retrieval=RetrievalConfig(method="keyword"),
    )
    service = MemoryService(config)
    service.init_vault()
    runtime = DummyRuntime(service)
    return service, runtime, SettingsUseCases(service, runtime)


def test_model_options_only_show_discovered_models(tmp_path):
    service, runtime, settings = build(tmp_path)

    options = settings.model_options(["deepseek-flash"])

    assert options[0].current
    assert options[0].provider == "deepseek"
    assert {option.model for option in options} == {"deepseek-flash"}


def test_qianwen_catalog_and_chat_use_dedicated_credentials_on_the_wire(tmp_path, monkeypatch):
    from syvrio.llm.client import LiteLLMChatModel

    service, _, settings = build(tmp_path)
    service.config_path = tmp_path / "config.toml"
    monkeypatch.setenv("QIANWEN_API_KEY", "synthetic-qianwen")
    model_id = "qwen3.8-max"
    bodies = []
    base = "https://maas.qianwenaiapi.com/compatible-mode/v1"

    def transport(_transport, request):
        assert request.headers["authorization"] == "Bearer synthetic-qianwen"
        if request.method == "GET":
            assert str(request.url) == base + "/models"
            return httpx.Response(
                200, json={"object": "list", "data": [{"id": model_id, "object": "model"}]}
            )
        assert str(request.url) == base + "/chat/completions"
        body = json.loads(request.content)
        bodies.append(body)
        common = {"id": "chatcmpl-test", "created": 1, "model": model_id}
        if body.get("stream"):
            chunk = {
                **common,
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Done"},
                        "finish_reason": "stop",
                    }
                ],
            }
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
            )
        message = {
            "role": "assistant",
            "content": "",
            "reasoning_content": "synthetic reasoning",
            "tool_calls": [
                {
                    "id": "lookup",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        }
        return httpx.Response(
            200,
            json={
                **common,
                "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    options, unavailable = settings.search_models(model_id, refresh=True)
    assert [(item.provider, item.model) for item in options] == [("qianwen", model_id)]
    assert not unavailable
    assert settings.apply_model("qianwen", model_id).ok
    config = load_config(service.config_path).llm
    assert (config.provider, config.base_url, config.api_key_env) == (
        "qianwen",
        base,
        "QIANWEN_API_KEY",
    )
    chat = LiteLLMChatModel(config)
    messages = [{"role": "user", "content": "Test"}]
    tools = [
        {
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}},
        }
    ]
    first = chat.chat(messages, tools)
    assert first.tool_calls[0].name == "lookup" and first.reasoning_source
    messages.extend(
        [
            {
                "role": "assistant",
                "content": first.content,
                "reasoning_content": first.reasoning_content,
                "reasoning_source": first.reasoning_source,
                "tool_calls": [
                    {
                        "id": "lookup",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "lookup", "content": "Synthetic result"},
        ]
    )
    deltas = []
    assert chat.chat(messages, tools, on_delta=deltas.append).content == "Done"
    assert deltas == ["Done"]
    assert [body["model"] for body in bodies] == [model_id, model_id]
    assert bodies[1]["messages"][1]["reasoning_content"] == "synthetic reasoning"
    assert bodies[1]["messages"][2]["tool_call_id"] == "lookup"
    assert "synthetic-qianwen" not in service.config_path.with_name("models.toml").read_text()


def test_qianwen_config_does_not_reuse_another_provider_key(tmp_path, monkeypatch):
    from syvrio.infra.config import infer_provider

    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-bailian")
    path = tmp_path / "config.toml"
    path.write_text('[llm]\nprovider = "qianwen"\nmodel = "vendor/model"\n', encoding="utf-8")
    llm = load_config(path).llm
    assert llm.base_url == "https://maas.qianwenaiapi.com/compatible-mode/v1"
    assert infer_provider(llm.base_url + "/") == "qianwen"
    assert llm.api_key_env == "QIANWEN_API_KEY" and llm.api_key() is None
    assert llm.litellm_model() == "openai/vendor/model"


def test_qianwen_glm_efforts_use_catalog_or_platform_defaults(monkeypatch):
    from syvrio.llm import client

    model = "glm-5.3"
    config = LLMConfig(
        provider="qianwen", model=model, base_url="https://maas.qianwenaiapi.com/compatible-mode/v1"
    )
    catalog = {model: None}
    monkeypatch.setattr(client, "_provider_catalog", lambda *args, **kwargs: catalog)
    assert client.provider_reasoning_levels(config) == ("low", "high", "max")
    catalog[model] = ("high",)
    assert client.provider_reasoning_levels(config) == ("high",)
    catalog.clear()
    config.base_url = "https://other.invalid/v1"
    assert client.provider_reasoning_levels(config) is None


def test_discover_models_for_inactive_provider_at_saved_endpoint(tmp_path, monkeypatch):
    from syvrio.infra.config import load_config

    service, runtime, settings = build(tmp_path)
    service.config_path = tmp_path / "config.toml"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-deepseek-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-workspace-key")
    endpoint = "https://workspace.example.invalid/compatible-mode/v1"
    assert settings.set_provider_endpoint("dashscope", endpoint).ok
    assert service.config.llm.provider == "deepseek" and runtime.reloads == 0
    assert load_config(service.config_path).llm.provider_urls == {"dashscope": endpoint}

    seen = {}

    def fetch(source, *, refresh=False):
        seen[source.provider] = (source.base_url, source.api_key_env, refresh)
        return ["deepseek-v4.1-flash-1017"] if source.provider == "dashscope" else []

    monkeypatch.setattr(settings_module, "fetch_provider_models", fetch)
    options = settings.model_options(settings.model_catalog(refresh=True), capabilities=False)
    assert seen["dashscope"] == (endpoint, "DASHSCOPE_API_KEY", True)
    assert set(seen) == {"deepseek", "dashscope"}
    assert [(option.provider, option.model) for option in options] == [
        ("dashscope", "deepseek-v4.1-flash-1017")
    ]
    assert settings.apply_model(options[0].provider, options[0].model).ok
    assert service.config.llm.base_url == endpoint
    assert settings.apply_model("deepseek", "deepseek-flash").ok
    assert settings.apply_model("dashscope", "deepseek-v4.1-flash-1017").ok
    assert service.config.llm.base_url == endpoint


def test_provider_options_mark_configured_and_current(tmp_path, monkeypatch):
    service, runtime, settings = build(tmp_path)
    preset = next(item for item in PRESETS if item.key == "spark")
    monkeypatch.setenv(preset.api_key_env, "sk-synthetic")

    options = settings.provider_options()

    spark = next(option for option in options if option.key == "spark")
    deepseek = next(option for option in options if option.key == "deepseek")
    assert spark.configured is True and spark.current is False
    assert deepseek.current is True


@pytest.mark.parametrize("provider", ["custom", "spark"])
def test_active_endpoint_is_used_by_chat_and_survives_restart(tmp_path, monkeypatch, provider):
    from syvrio.llm import LiteLLMChatModel

    service, runtime, settings = build(tmp_path)
    service.config_path = tmp_path / "config.toml"
    old, new = "https://old.example.invalid/v1", "https://new.example.invalid/v1"
    config = LLMConfig(provider=provider, base_url=old, api_key_env="SYNTHETIC_ENDPOINT_TEST_KEY")
    service.config.llm = config
    monkeypatch.setenv(config.api_key_env, "synthetic-key")
    requests = []

    def completion(**request):
        requests.append(request["api_base"])
        return [{"choices": [{"delta": {"content": "ok"}}]}]

    monkeypatch.setattr(
        "syvrio.llm.client._litellm", lambda: SimpleNamespace(completion=completion)
    )
    running = LiteLLMChatModel(config)
    assert settings.set_provider_endpoint(provider, new + "/").ok
    assert runtime.reloads == 1
    with service.config_path.with_name("models.toml").open("rb") as handle:
        written = tomllib.load(handle)["llm"]
    assert written["base_url"] == written["provider_urls"][provider] == new
    saved = load_config(service.config_path).llm
    assert settings.provider_endpoint(provider) == new
    assert saved.base_url == saved.provider_urls[provider] == new
    assert saved.api_key_env == config.api_key_env
    for model in (running, LiteLLMChatModel(service.config.llm), LiteLLMChatModel(saved)):
        reply = model.chat([{"role": "user", "content": "test"}], [], on_delta=lambda _: None)
        assert reply.content == "ok"
    assert requests == [old, new, new]


def test_older_config_uses_saved_provider_endpoint_for_chat(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[llm]\nprovider = "custom"\nbase_url = "https://old.example.invalid/v1"\n'
        'provider_urls = { custom = "https://new.example.invalid/v1" }\n',
        encoding="utf-8",
    )
    assert load_config(path).llm.base_url == "https://new.example.invalid/v1"


def test_update_provider_key_keyring_failure_is_reported(tmp_path, monkeypatch):
    service, runtime, settings = build(tmp_path)
    monkeypatch.setattr(settings_module, "save_api_key", lambda env, key: False)

    result = settings.update_provider_key("spark", "sk-synthetic")

    assert result.ok is False
    assert "Could not save" in result.message


def test_environment_key_takes_priority_and_remove_refuses(tmp_path, monkeypatch):
    service, runtime, settings = build(tmp_path)
    monkeypatch.setenv("SPARK_API_KEY", "sk-synthetic")

    result = settings.remove_provider("spark")

    assert result.ok is False
    assert "provided by SPARK_API_KEY" in result.message


@pytest.mark.parametrize("action", ["configure_provider", "update_provider_key"])
def test_environment_key_cannot_be_shadowed_by_settings(tmp_path, monkeypatch, action):
    _, _, settings = build(tmp_path)
    monkeypatch.setenv("SPARK_API_KEY", "synthetic-environment-value")
    monkeypatch.setattr(settings_module, "save_api_key", lambda *_: pytest.fail("unexpected write"))

    result = getattr(settings, action)("spark", "synthetic-new-value")

    assert not result.ok and "provided by SPARK_API_KEY" in result.message
    assert "synthetic" not in result.message


def test_apply_model_reloads_runtime_and_persists(tmp_path):
    service, runtime, settings = build(tmp_path)
    service.config_path = tmp_path / "syvrio.toml"

    result = settings.apply_model("deepseek", "deepseek-v4-pro")

    assert result.ok is True
    assert runtime.reloads == 1
    assert service.config.llm.model == "deepseek-v4-pro"
    assert service.config_path.exists()


def test_failed_save_keeps_the_current_model_and_context(tmp_path, monkeypatch):
    service, runtime, settings = build(tmp_path)
    service.config_path = tmp_path / "config.toml"
    current = service.config.llm
    window = service.config.chat.context_window
    monkeypatch.setattr(settings_module, "model_metadata", lambda *_: {})

    def fail_write(path, config):
        raise OSError("write failed")

    monkeypatch.setattr(settings_module, "write_config", fail_write)
    with pytest.raises(OSError, match="write failed"):
        settings.apply_model("deepseek", "new-model")
    with pytest.raises(OSError, match="write failed"):
        settings.apply_context(75_000)
    for provider in (current.provider, "spark"):
        with pytest.raises(OSError, match="write failed"):
            settings.set_provider_endpoint(provider, "https://new.example.invalid/v1")
    assert service.config.llm is current
    assert service.config.chat.context_window == window
    assert runtime.reloads == 0


def test_switch_model_preserves_chat_configuration(tmp_path, monkeypatch):
    from dataclasses import replace

    from syvrio.infra.providers import write_config

    service, _, settings = build(tmp_path)
    service.config_path = tmp_path / "config.toml"
    chat = replace(
        service.config.chat,
        auto_reflect=False,
        dream_auto_interval_hours=25,
    )
    service.config.chat = chat
    monkeypatch.setattr(settings_module, "model_metadata", lambda *_: {})
    assert settings.apply_model("deepseek", "synthetic-model").ok
    saved = load_config(service.config_path)
    assert saved.chat == chat
    write_config(service.config_path, saved)
    assert load_config(service.config_path) == saved


def test_recent_models_survive_reload_and_prioritize_matching_catalog_entries(
    tmp_path, monkeypatch
):
    service, _, settings = build(tmp_path)
    service.config_path = tmp_path / "config.toml"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-key")
    for model in ("oldest", "unlisted", "shared", "two", "three"):
        assert settings.apply_model("deepseek", model).ok
    assert settings.apply_model("dashscope", "shared").ok
    assert settings.apply_model("deepseek", "two").ok

    service.config.llm = load_config(service.config_path).llm
    assert service.config.llm.recent_models == [
        ("deepseek", "two"),
        ("dashscope", "shared"),
        ("deepseek", "three"),
        ("deepseek", "shared"),
        ("deepseek", "unlisted"),
    ]
    options = settings.model_options(
        {"deepseek": ["oldest", "shared", "two", "three", "shared"], "dashscope": ["shared"]},
        capabilities=False,
    )
    assert [(row.provider, row.model, row.recent, row.current) for row in options] == [
        ("deepseek", "two", True, True),
        ("dashscope", "shared", True, False),
        ("deepseek", "three", True, False),
        ("deepseek", "shared", True, False),
        ("deepseek", "oldest", False, False),
    ]


def test_apply_model_without_key_does_not_switch(tmp_path):
    service, runtime, settings = build(tmp_path)
    before = service.config.llm

    result = settings.apply_model("spark", "spark-x2.6")

    assert result.ok is False
    assert "no API key" in result.message
    assert service.config.llm is before
    assert runtime.reloads == 0


def test_switching_to_a_small_model_preserves_preferred_context(tmp_path, monkeypatch):
    service, runtime, settings = build(tmp_path)
    service.config.chat.context_window = 64_000
    monkeypatch.setattr(settings_module, "model_metadata", lambda *_: {"max_input_tokens": 8000})
    monkeypatch.setattr("syvrio.llm.client.model_metadata", lambda *_: {"max_input_tokens": 8000})

    result = settings.apply_model("deepseek", "deepseek-v4-pro")

    assert result.ok is True
    assert settings.context_window == 64_000
    assert effective_context_window(service.config.llm, settings.context_window) == 8_000


def test_context_options_and_apply_limits(tmp_path, monkeypatch):
    service, runtime, settings = build(tmp_path)
    monkeypatch.setattr(settings_module, "model_metadata", lambda *_: {"max_input_tokens": 32_000})
    assert settings.context_options() == [50_000]

    invalid = settings.apply_context(49_999)
    assert invalid.ok is False and "50,000" in invalid.message
    assert settings.apply_context(50_000).ok

    capped = settings.apply_context(64_000)
    assert capped.ok is True
    assert service.config.chat.context_window == 64_000
    assert "32K" in capped.message


def test_context_presets_skip_model_overflow_without_promoting_custom_values(tmp_path, monkeypatch):
    service, runtime, settings = build(tmp_path)
    service.config.chat.context_window = 250_000
    monkeypatch.setattr(settings_module, "model_metadata", lambda *_: {"max_input_tokens": 500_000})
    assert settings.context_options() == [50_000, 100_000, 200_000, 400_000]
    monkeypatch.setattr(settings_module, "model_metadata", lambda *_: {})
    assert settings.context_options() == [50_000, 100_000, 200_000, 400_000, 600_000]


def test_reasoning_options_follow_model_support(tmp_path, monkeypatch):
    service, runtime, settings = build(tmp_path)
    monkeypatch.setattr(settings_module, "supported_params", lambda *_: ("reasoning_effort",))
    assert [value for value, _ in settings.reasoning_options()] == ["", "low", "medium", "high"]

    monkeypatch.setattr(settings_module, "supported_params", lambda *_: ())
    assert settings.reasoning_options() == []


def test_thinking_levels_use_provider_model_metadata_before_presets(tmp_path, monkeypatch):
    service, runtime, settings = build(tmp_path)
    requests = []
    status = 200
    rows = [
        ("deep-model", {"effort": {"supported_levels": ["low", "high", "max"]}}),
        ("router-model", {"reasoning": {"supported_efforts": ["high", "xhigh"]}}),
        ("no-metadata", {}),
        ("invalid", {"effort": {"supported_levels": ["high", {}]}}),
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            body = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "object": "model",
                            "id": model,
                            "created": 0,
                            "owned_by": "fixture",
                            **fields,
                        }
                        for model, fields in rows
                    ],
                }
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            monkeypatch.setenv("SYVRIO_TEST_CATALOG_KEY", "fixture")
            llm = LLMConfig(
                provider="custom",
                model="deep-model",
                api_key_env="SYVRIO_TEST_CATALOG_KEY",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
            )
            service.config.llm = llm
            monkeypatch.setattr(settings_module, "supported_params", lambda _: ())
            with ThreadPoolExecutor(max_workers=2) as pool:
                listing = pool.submit(fetch_provider_models, llm)
                options = settings.reasoning_options()
            assert listing.result() == [model for model, _ in rows]
            assert [value for value, _ in options] == ["", "low", "high", "max"]
            assert settings.apply_reasoning("max").ok
            assert not settings.apply_reasoning("medium").ok
            assert runtime.model_label == "custom/deep-model"
            assert service.config.llm.reasoning_effort == "max"
            service.config.llm.model = "router-model"
            assert [value for value, _ in settings.reasoning_options()] == ["", "high", "xhigh"]
            monkeypatch.setattr(
                settings_module, "supported_params", lambda _: ("reasoning_effort",)
            )
            for model in ("no-metadata", "invalid", "unlisted"):
                service.config.llm.model = model
                assert [value for value, _ in settings.reasoning_options()] == [
                    "",
                    "low",
                    "medium",
                    "high",
                ]
            assert requests == ["/v1/models"]
            status = 503
            assert fetch_provider_models(llm, refresh=True) == []
            service.config.llm.model = "deep-model"
            assert [value for value, _ in settings.reasoning_options()] == [
                "",
                "low",
                "medium",
                "high",
            ]
            assert requests == ["/v1/models"] * 2
        finally:
            server.shutdown()
            worker.join()


def test_spark_thinking_modes_persist_without_overwriting_reasoning_effort(tmp_path):
    service, _, settings = build(tmp_path)
    service.config_path = tmp_path / "config.toml"
    llm = service.config.llm
    llm.provider = "spark"
    llm.model = "spark-x2.6"
    llm.reasoning_effort = "medium"
    assert settings.reasoning_selection() == ("Thinking mode", "enabled")
    assert [value for value, _ in settings.reasoning_options()] == ["auto", "disabled", "enabled"]
    assert not settings.apply_reasoning("high").ok
    assert settings.apply_reasoning("auto").ok
    assert settings.reasoning_selection() == ("Thinking mode", "auto")
    saved = load_config(service.config_path).llm
    assert saved.spark_thinking == "auto"
    assert saved.reasoning_effort == "medium"
