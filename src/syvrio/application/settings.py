"""模型、供应商、推理与上下文设置用例。"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from ..infra.config import (
    CONTEXT_WINDOW_PRESETS,
    MIN_CONTEXT_WINDOW,
    SPARK_THINKING_MODES,
    LLMConfig,
)
from ..infra.providers import (
    PRESETS,
    delete_api_key,
    has_api_key,
    normalize_provider_url,
    provider_base_url,
    provider_label,
    save_api_key,
    write_config,
)
from ..infra.tokens import format_tokens
from ..llm import (
    cached_provider_models,
    fetch_provider_models,
    model_metadata,
    provider_reasoning_levels,
    supported_params,
)
from ..memory.service import MemoryService
from .contracts import ModelOption, ProviderOption, SettingsResult


class SettingsUseCases:
    def __init__(self, service: MemoryService, runtime) -> None:
        self.service = service
        self.runtime = runtime

    # ---------- 查询 ----------

    def model_sources(self) -> dict[str, LLMConfig]:
        current = self.service.config.llm
        sources = {}
        if current.api_key() or not current.requires_api_key():
            sources[current.provider] = replace(
                current, base_url=self.provider_endpoint(current.provider)
            )
        for preset in PRESETS:
            if preset.key not in {"custom", current.provider} and has_api_key(preset):
                sources[preset.key] = replace(
                    current,
                    provider=preset.key,
                    base_url=provider_base_url(current, preset),
                    api_key_env=preset.api_key_env,
                )
        return sources

    def model_catalog(
        self, *, cached_only: bool = False, refresh: bool = False
    ) -> dict[str, list[str]]:
        sources = self.model_sources()
        if cached_only:
            return {key: cached_provider_models(source) or [] for key, source in sources.items()}
        if not sources:
            return {}
        with ThreadPoolExecutor(
            max_workers=len(sources), thread_name_prefix="syvrio-models"
        ) as pool:
            requests = {
                key: pool.submit(fetch_provider_models, source, refresh=refresh)
                for key, source in sources.items()
            }
            return {key: request.result() for key, request in requests.items()}

    def model_options(
        self,
        fetched: dict[str, list[str]] | list[str] | None = None,
        capabilities: bool = True,
    ) -> list[ModelOption]:
        current = self.service.config.llm
        catalog = {current.provider: fetched} if isinstance(fetched, list) else fetched
        if catalog is None:
            catalog = self.model_catalog(cached_only=True)
        entries = [(provider, model) for provider, models in catalog.items() for model in models]
        recent = {key: index for index, key in enumerate(current.recent_model_ids())}

        seen: set[tuple[str, str]] = set()
        options: list[ModelOption] = []
        for provider, model in entries:
            if (provider, model) in seen:
                continue
            seen.add((provider, model))
            caps: list[str] = []
            if capabilities:
                key = replace(current, provider=provider, model=model).litellm_model()
                info = model_metadata(key)
                max_input = info.get("max_input_tokens")
                if max_input:
                    try:
                        caps.append(format_tokens(int(max_input)))
                    except (TypeError, ValueError):
                        pass
                if "reasoning_effort" in supported_params(key):
                    caps.append("reasoning")
            label = f"{provider_label(provider)} · {model}"
            if caps:
                label += f"  ({', '.join(caps)})"
            options.append(
                ModelOption(
                    provider,
                    model,
                    label,
                    current=(provider, model) == (current.provider, current.model),
                    recent=(provider, model) in recent,
                )
            )
        options.sort(key=lambda option: recent.get((option.provider, option.model), len(recent)))
        return options

    def search_models(
        self, query: str = "", *, refresh: bool = False
    ) -> tuple[list[ModelOption], list[str]]:
        catalog = self.model_catalog(refresh=refresh)
        options = self.model_options(catalog, capabilities=False)
        current = self.service.config.llm
        if not any(option.current for option in options):
            label = f"{current.provider}/{current.model}"
            options.insert(0, ModelOption(current.provider, current.model, label, current=True))
        needle = query.strip().casefold()
        matches = [
            option
            for option in options
            if needle in f"{option.provider}/{option.model}".casefold()
            or needle in option.label.casefold()
        ]
        return matches, [provider for provider, models in catalog.items() if not models]

    def provider_options(self) -> list[ProviderOption]:
        current = self.service.config.llm.provider
        return [
            ProviderOption(
                preset.key,
                provider_label(preset.key),
                has_api_key(preset),
                preset.key == current,
            )
            for preset in PRESETS
            if preset.key != "custom"
        ]

    def supports_reasoning(self) -> bool:
        return bool(self.reasoning_options())

    def reasoning_options(self) -> list[tuple[str, str]]:
        return [
            (value, value or "default") for value in self._reasoning_values(self.service.config.llm)
        ]

    @staticmethod
    def _reasoning_values(llm: LLMConfig) -> tuple[str, ...]:
        levels = provider_reasoning_levels(llm)
        if llm.is_spark:
            if levels is not None and all(value in SPARK_THINKING_MODES for value in levels):
                return tuple(value for value in SPARK_THINKING_MODES if value in levels)
            return SPARK_THINKING_MODES
        if levels is not None:
            # 选择空项时省略请求参数，恢复供应商的默认设置。
            return ("", *(level for level in levels if level != "default")) if levels else ()
        if "reasoning_effort" in supported_params(llm.litellm_model()):
            return ("", "low", "medium", "high")
        return ()

    def reasoning_selection(self) -> tuple[str, str]:
        llm = self.service.config.llm
        if llm.is_spark:
            return "Thinking mode", llm.spark_thinking or "enabled"
        return "Reasoning effort", llm.reasoning_effort or ""

    def context_options(self) -> list[int]:
        max_input = self._max_input()
        return [size for size in CONTEXT_WINDOW_PRESETS if not max_input or size <= max_input] or [
            MIN_CONTEXT_WINDOW
        ]

    @property
    def context_window(self) -> int:
        return self.service.config.chat.context_window

    @property
    def model_label(self) -> str:
        return self.runtime.model_label

    # ---------- 应用 ----------

    def configure_provider(self, provider: str, api_key: str) -> SettingsResult:
        return self._save_provider_key(provider, api_key, updating=False)

    def update_provider_key(self, provider: str, api_key: str) -> SettingsResult:
        return self._save_provider_key(provider, api_key, updating=True)

    def _save_provider_key(self, provider: str, api_key: str, *, updating: bool) -> SettingsResult:
        preset = self._preset(provider)
        if preset is None:
            return SettingsResult(False, f"Unknown provider: {provider}")
        key = api_key.strip()
        if not key:
            return SettingsResult(False, "API key is empty; nothing changed.")
        if os.environ.get(preset.api_key_env):
            return SettingsResult(
                False,
                f"{provider_label(preset.key)} API key is provided by {preset.api_key_env}. "
                "Update the environment variable and restart the backend, "
                "or unset it and restart to manage the key here.",
            )
        if not save_api_key(preset.api_key_env, key):
            return SettingsResult(
                False,
                "Could not save the API key to system credentials. "
                f"Set {preset.api_key_env} in your environment instead.",
            )
        if updating:
            return SettingsResult(True, f"{provider_label(preset.key)} API key updated.")
        return SettingsResult(
            True,
            f"{provider_label(preset.key)} API key saved. Choose an API-listed model to switch.",
        )

    def provider_endpoint(self, provider: str) -> str:
        current = self.service.config.llm
        preset = self._preset(provider)
        return (
            provider_base_url(current, preset)
            if preset
            else current.provider_urls.get(provider, current.base_url)
        )

    def set_provider_endpoint(self, provider: str, endpoint: str) -> SettingsResult:
        current = self.service.config.llm
        if provider != current.provider and self._preset(provider) is None:
            return SettingsResult(False, f"Unknown provider: {provider}")
        try:
            endpoint = normalize_provider_url(endpoint)
        except ValueError as exc:
            return SettingsResult(False, str(exc))
        active = provider == current.provider
        llm = replace(
            current,
            base_url=endpoint if active else current.base_url,
            provider_urls={**current.provider_urls, provider: endpoint},
        )
        config = self.service.config
        if self.service.config_path:
            pending = replace(config, llm=llm)
            write_config(self.service.config_path, pending)
            config._baseline = pending._baseline
        config.llm = llm
        if active and self.runtime is not None:
            self.runtime.reload_llm(config)
        message = f"{provider_label(provider)} Base URL saved."
        if active:
            message += " Applies next turn."
        return SettingsResult(True, message, True)

    def remove_provider(self, provider: str) -> SettingsResult:
        preset = self._preset(provider)
        if preset is None:
            return SettingsResult(False, f"Unknown provider: {provider}")
        label = provider_label(preset.key)
        if provider == self.service.config.llm.provider:
            return SettingsResult(
                False, f"{label} is the current provider; switch to another model first."
            )
        if os.environ.get(preset.api_key_env):
            return SettingsResult(
                False,
                f"{label} API key is provided by {preset.api_key_env}; "
                "unset it in your environment to remove this provider.",
            )
        if not has_api_key(preset):
            return SettingsResult(False, f"{label} has no saved API key.")
        if not delete_api_key(preset.api_key_env):
            return SettingsResult(False, f"Could not remove {label} from system credentials.")
        return SettingsResult(True, f"{label} provider removed; its saved API key was deleted.")

    def apply_model(self, provider: str, model: str) -> SettingsResult:
        current = self.service.config.llm
        preset = self._preset(provider)
        new_llm = replace(
            current,
            provider=provider,
            model=model,
            base_url=self.provider_endpoint(provider),
            provider_urls={
                **({current.provider: current.base_url} if current.base_url else {}),
                **current.provider_urls,
            },
            api_key_env=preset.api_key_env
            if preset and provider != current.provider
            else current.api_key_env,
            recent_models=current.recent_model_ids(),
        )
        if new_llm.provider != current.provider and not new_llm.api_key():
            return SettingsResult(
                False,
                f"{provider} has no API key yet; run syvrio setup in a terminal to configure it.",
            )

        if (
            not new_llm.is_spark
            and new_llm.reasoning_effort
            and new_llm.reasoning_effort not in self._reasoning_values(new_llm)
        ):
            new_llm.reasoning_effort = None
        context_window = self.service.config.chat.context_window

        new_llm.recent_models = new_llm.recent_model_ids()
        self._save_turn_settings(new_llm, context_window)
        return SettingsResult(
            True,
            f"Model switched: {self.runtime.model_label}"
            f" · context {format_tokens(context_window)}"
            f" · reasoning {self.reasoning_selection()[1] or 'default'} · applies next turn.",
            True,
        )

    def apply_reasoning(self, value: str) -> SettingsResult:
        llm = self.service.config.llm
        choices = self._reasoning_values(llm)
        if value not in choices:
            allowed = ", ".join(option or "default" for option in choices)
            return SettingsResult(False, f"Allowed thinking options: {allowed or '(none)'}.")
        if self.service.config.llm is not llm:
            return SettingsResult(False, "The active model changed. Reopen Thinking and try again.")
        if llm.is_spark:
            new_llm = replace(llm, spark_thinking=value)
        else:
            new_llm = replace(llm, reasoning_effort=value or None)
        self._save_turn_settings(new_llm, self.context_window)
        label, current = self.reasoning_selection()
        return SettingsResult(True, f"{label}: {current or 'default'} · applies next turn.", True)

    def apply_context(self, value: int) -> SettingsResult:
        max_input = self._max_input()
        if value < MIN_CONTEXT_WINDOW:
            return SettingsResult(
                False, f"Context window must be at least {MIN_CONTEXT_WINDOW:,} tokens."
            )
        if max_input and value > max_input:
            note = f"This model can use up to {format_tokens(max_input)} tokens."
        else:
            note = ""
        if value == self.service.config.chat.context_window:
            return SettingsResult(True, "Context window unchanged.")
        self._save_turn_settings(self.service.config.llm, value)
        return SettingsResult(
            True,
            f"Context window set to {format_tokens(value)} · applies next turn. {note}".strip(),
            True,
        )

    # ---------- 内部 ----------

    def _save_turn_settings(self, llm: LLMConfig, context_window: int) -> None:
        config = self.service.config
        if self.service.config_path:
            pending = replace(
                config, llm=llm, chat=replace(config.chat, context_window=context_window)
            )
            write_config(self.service.config_path, pending)
            config._baseline = pending._baseline
        model_changed = config.llm is not llm
        config.llm = llm
        config.chat.context_window = context_window
        if model_changed and self.runtime is not None:
            self.runtime.reload_llm(config)

    def _preset(self, provider: str):
        preset = next((item for item in PRESETS if item.key == provider), None)
        return None if preset is None or preset.key == "custom" else preset

    def _max_input(self) -> int | None:
        max_input = model_metadata(self.service.config.llm.litellm_model()).get("max_input_tokens")
        try:
            return int(max_input) if max_input else None
        except (TypeError, ValueError):
            return None
