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


def test_model_options_current_first_and_presets_merged(tmp_path):
    service, runtime, settings = build(tmp_path)

    options = settings.model_options(["deepseek-flash"])

    assert options[0].current
    assert options[0].provider == "deepseek"
    assert "deepseek-v4-pro" in {option.model for option in options}


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
