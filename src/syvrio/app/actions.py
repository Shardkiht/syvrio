"""兼容适配：设置用例已迁至 application/settings.py，这里只做旧签名映射。"""

from __future__ import annotations

from ..agent.runtime import AgentRuntime
from ..application.contracts import ModelOption
from ..application.settings import SettingsUseCases
from ..memory.service import MemoryService

__all__ = [
    "ModelOption",
    "apply_context",
    "apply_model",
    "apply_reasoning",
    "configure_provider",
    "context_options",
    "model_options",
    "provider_options",
    "reasoning_options",
    "supports_reasoning",
    "update_provider_key",
    "remove_provider",
]


def _settings(service: MemoryService, runtime: AgentRuntime | None = None) -> SettingsUseCases:
    return SettingsUseCases(service, runtime)


def model_options(
    service: MemoryService,
    fetched: list[str] | None = None,
    capabilities: bool = True,
) -> list[ModelOption]:
    return _settings(service).model_options(fetched, capabilities)


def provider_options(service: MemoryService) -> list[tuple[str, tuple[str, str, bool], bool]]:
    return [
        (
            f"{option.label} · {'configured' if option.configured else 'add provider'}",
            (option.key, option.label, option.configured),
            option.current,
        )
        for option in _settings(service).provider_options()
    ]


def configure_provider(
    service: MemoryService,
    runtime: AgentRuntime,
    provider: str,
    api_key: str,
) -> str:
    return _settings(service, runtime).configure_provider(provider, api_key).message


def update_provider_key(service: MemoryService, provider: str, api_key: str) -> str:
    return _settings(service).update_provider_key(provider, api_key).message


def remove_provider(service: MemoryService, provider: str) -> str:
    return _settings(service).remove_provider(provider).message


def apply_model(
    service: MemoryService,
    runtime: AgentRuntime,
    provider: str,
    model: str,
) -> str:
    return _settings(service, runtime).apply_model(provider, model).message


def supports_reasoning(service: MemoryService) -> bool:
    return _settings(service).supports_reasoning()


def reasoning_options(service: MemoryService) -> list[tuple[str, str]]:
    return _settings(service).reasoning_options()


def apply_reasoning(service: MemoryService, value: str) -> str:
    return _settings(service).apply_reasoning(value).message


def context_options(service: MemoryService) -> list[int]:
    return _settings(service).context_options()


def apply_context(service: MemoryService, value: int) -> str:
    return _settings(service).apply_context(value).message
