"""Application Settings 用例：不访问真实 Keyring/API。"""
from __future__ import annotations

import pytest

from syvrio.application import settings as settings_module
from syvrio.application.settings import SettingsUseCases
from syvrio.infra.config import Config, LLMConfig, RetrievalConfig, load_config
from syvrio.infra.providers import PRESETS
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


def test_apply_model_reloads_runtime_and_persists(tmp_path):
    service, runtime, settings = build(tmp_path)
    service.config_path = tmp_path / "syvrio.toml"

    result = settings.apply_model("deepseek", "deepseek-v4-pro")

    assert result.ok is True
    assert runtime.reloads == 1
    assert service.config.llm.model == "deepseek-v4-pro"
    assert service.config_path.exists()


def test_switch_model_preserves_chat_configuration(tmp_path, monkeypatch):
    from dataclasses import replace
    from syvrio.infra.providers import write_config

    service, _, settings = build(tmp_path)
    service.config_path = tmp_path / "config.toml"
    chat = replace(service.config.chat, auto_reflect=False, reflect_idle_seconds=91,
                   reflect_interval_seconds=301, profile_auto_interval_hours=171,
                   profile_min_cooldown_hours=28, profile_min_changes=21, dream_auto_interval_hours=25)
    service.config.chat = chat
    monkeypatch.setattr(settings_module, "model_metadata", lambda *_: {})
    assert settings.apply_model("deepseek", "synthetic-model").ok
    saved = load_config(service.config_path)
    assert saved.chat == chat
    write_config(service.config_path, saved)
    assert load_config(service.config_path) == saved


def test_recent_models_survive_reload_and_prioritize_matching_catalog_entries(tmp_path, monkeypatch):
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
        ("deepseek", "two"), ("dashscope", "shared"), ("deepseek", "three"),
        ("deepseek", "shared"), ("deepseek", "unlisted"),
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


def test_apply_model_caps_context_to_model_limit(tmp_path, monkeypatch):
    service, runtime, settings = build(tmp_path)
    service.config.chat.context_window = 64_000
    monkeypatch.setattr(settings_module, "model_metadata", lambda *_: {"max_input_tokens": 8000})

    result = settings.apply_model("deepseek", "deepseek-v4-pro")

    assert result.ok is True
    assert service.config.chat.context_window == 8000


def test_context_options_and_apply_limits(tmp_path, monkeypatch):
    service, runtime, settings = build(tmp_path)
    monkeypatch.setattr(settings_module, "model_metadata", lambda *_: {"max_input_tokens": 32_000})
    options = settings.context_options()
    assert all(size <= 32_000 for size in options)

    invalid = settings.apply_context(1000)
    assert invalid.ok is False and "2000" in invalid.message

    capped = settings.apply_context(64_000)
    assert capped.ok is True
    assert service.config.chat.context_window == 32_000


def test_reasoning_options_follow_model_support(tmp_path, monkeypatch):
    service, runtime, settings = build(tmp_path)
    monkeypatch.setattr(settings_module, "supported_params", lambda *_: ("reasoning_effort",))
    assert [value for value, _ in settings.reasoning_options()] == ["", "low", "medium", "high"]

    monkeypatch.setattr(settings_module, "supported_params", lambda *_: ())
    assert settings.reasoning_options() == []


def test_spark_thinking_modes_persist_without_overwriting_reasoning_effort(tmp_path):
    service, _, settings = build(tmp_path)
    service.config_path = tmp_path / "config.toml"
    llm = service.config.llm
    llm.provider = "spark"
    llm.model = "spark-x2.6"
    llm.reasoning_effort = "medium"
    assert settings.reasoning_selection() == ("Thinking mode", "enabled")
    assert [value for value, _ in settings.reasoning_options()] == ["enabled", "auto", "disabled"]
    assert not settings.apply_reasoning("high").ok
    assert settings.apply_reasoning("auto").ok
    assert settings.reasoning_selection() == ("Thinking mode", "auto")
    saved = load_config(service.config_path).llm
    assert saved.spark_thinking == "auto"
    assert saved.reasoning_effort == "medium"
