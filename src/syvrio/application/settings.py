"""模型、供应商、推理与上下文设置用例。"""
from __future__ import annotations

from dataclasses import replace

from ..infra.config import SPARK_THINKING_MODES
from ..infra.providers import (
    PRESETS,
    delete_api_key,
    display_model,
    has_api_key,
    provider_label,
    save_api_key,
    write_config,
)
from ..infra.tokens import format_tokens
from ..llm import model_metadata, supported_params
from ..memory.service import MemoryService
from .contracts import ModelOption, ProviderOption, SettingsResult


class SettingsUseCases:
    def __init__(self, service: MemoryService, runtime) -> None:
        self.service = service
        self.runtime = runtime

    # ---------- 查询 ----------

    def model_options(
        self, fetched: list[str] | None = None, capabilities: bool = True
    ) -> list[ModelOption]:
        current = self.service.config.llm
        entries: list[tuple[str, str]] = []
        for preset in PRESETS:
            if preset.key == "custom":
                continue
            if preset.key != current.provider and not has_api_key(preset):
                continue
            models = list(preset.models)
            if preset.key == current.provider and fetched:
                # 线上 /models 可能只返回当前一个模型（如 Spark）；与预设合并，
                # 避免选择器里只剩一项、方向键看起来失效。
                models = list(fetched) + [model for model in preset.models if model not in fetched]
            entries.extend((preset.key, model) for model in models)
        if current.provider not in {preset.key for preset in PRESETS}:
            entries = [(current.provider, model) for model in (fetched or [current.model])] + entries
        if (current.provider, current.model) not in entries:
            entries.insert(0, (current.provider, current.model))

        seen: set[tuple[str, str]] = set()
        options: list[ModelOption] = []
        for provider, model in entries:
            if (provider, model) in seen:
                continue
            seen.add((provider, model))
            key = replace(current, provider=provider, model=model).litellm_model()
            caps: list[str] = []
            if capabilities:
                info = model_metadata(key)
                max_input = info.get("max_input_tokens")
                if max_input:
                    try:
                        caps.append(format_tokens(int(max_input)))
                    except (TypeError, ValueError):
                        pass
                if "reasoning_effort" in supported_params(key):
                    caps.append("reasoning")
            label = f"{provider_label(provider)} · {display_model(model)}"
            if caps:
                label += f"  ({', '.join(caps)})"
            options.append(
                ModelOption(
                    provider,
                    model,
                    label,
                    current=(provider, model) == (current.provider, current.model),
                )
            )
        options.sort(key=lambda option: not option.current)
        return options

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
        llm = self.service.config.llm
        return llm.is_spark or "reasoning_effort" in supported_params(llm.litellm_model())

    def reasoning_options(self) -> list[tuple[str, str]]:
        if self.service.config.llm.is_spark:
            return [(mode, mode) for mode in SPARK_THINKING_MODES]
        if not self.supports_reasoning():
            return []
        return [("", "default"), ("low", "low"), ("medium", "medium"), ("high", "high")]

    def reasoning_selection(self) -> tuple[str, str]:
        llm = self.service.config.llm
        if llm.is_spark:
            return "Thinking mode", llm.spark_thinking or "enabled"
        return "Reasoning effort", llm.reasoning_effort or ""

    def context_options(self) -> list[int]:
        max_input = self._max_input()
        presets = [8_000, 16_000, 32_000, 64_000, 128_000, 200_000]
        if max_input:
            presets = [size for size in presets if size <= max_input]
            if max_input not in presets:
                presets.append(max_input)
        current = self.service.config.chat.context_window
        if current not in presets:
            presets.insert(0, current)
        return sorted(presets)

    @property
    def context_window(self) -> int:
        return self.service.config.chat.context_window

    @property
    def model_label(self) -> str:
        return self.runtime.model_label

    # ---------- 应用 ----------

    def configure_provider(self, provider: str, api_key: str) -> SettingsResult:
        preset = self._preset(provider)
        if preset is None:
            return SettingsResult(False, f"Unknown provider: {provider}")
        key = api_key.strip()
        if not key:
            return SettingsResult(False, "API key is empty; nothing changed.")
        if not save_api_key(preset.api_key_env, key):
            return SettingsResult(
                False,
                "Could not save the API key to system credentials. "
                f"Set {preset.api_key_env} in your environment instead.",
            )
        applied = self.apply_model(preset.key, preset.model)
        return SettingsResult(
            applied.ok,
            f"{provider_label(preset.key)} API key saved. {applied.message}",
            applied.config_changed,
        )

    def update_provider_key(self, provider: str, api_key: str) -> SettingsResult:
        preset = self._preset(provider)
        if preset is None:
            return SettingsResult(False, f"Unknown provider: {provider}")
        key = api_key.strip()
        if not key:
            return SettingsResult(False, "API key is empty; nothing changed.")
        if not save_api_key(preset.api_key_env, key):
            return SettingsResult(
                False,
                "Could not save the API key to system credentials. "
                f"Set {preset.api_key_env} in your environment instead.",
            )
        return SettingsResult(True, f"{provider_label(preset.key)} API key updated.")

    def remove_provider(self, provider: str) -> SettingsResult:
        import os

        preset = self._preset(provider)
        if preset is None:
            return SettingsResult(False, f"Unknown provider: {provider}")
        label = provider_label(preset.key)
        if provider == self.service.config.llm.provider:
            return SettingsResult(False, f"{label} is the current provider; switch to another model first.")
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
            base_url=preset.base_url if preset and provider != current.provider else current.base_url,
            api_key_env=preset.api_key_env if preset and provider != current.provider else current.api_key_env,
        )
        if new_llm.provider != current.provider and not new_llm.api_key():
            return SettingsResult(
                False,
                f"{provider} has no API key yet; run syvrio setup in a terminal to configure it.",
            )

        info = model_metadata(new_llm.litellm_model())
        if new_llm.reasoning_effort and "reasoning_effort" not in supported_params(new_llm.litellm_model()):
            new_llm.reasoning_effort = None
        context_window = self.service.config.chat.context_window
        max_input = info.get("max_input_tokens")
        if max_input:
            try:
                max_input = int(max_input)
            except (TypeError, ValueError):
                max_input = None
        if max_input and context_window > max_input:
            context_window = max_input

        self.service.config.llm = new_llm
        self.service.config.chat.context_window = context_window
        if self.service.config_path:
            write_config(self.service.config_path, self.service.config)
        self.runtime.reload_llm(self.service.config)
        return SettingsResult(
            True,
            f"Model switched: {self.runtime.model_label}"
            f" · context {format_tokens(context_window)}"
            f" · reasoning {self.reasoning_selection()[1] or 'default'}",
            True,
        )

    def apply_reasoning(self, value: str) -> SettingsResult:
        llm = self.service.config.llm
        if llm.is_spark:
            if value not in SPARK_THINKING_MODES:
                return SettingsResult(False, "Thinking mode must be enabled, auto, or disabled.")
            new_llm = replace(llm, spark_thinking=value)
        else:
            new_llm = replace(llm, reasoning_effort=value or None)
        self.service.config.llm = new_llm
        if self.service.config_path:
            write_config(self.service.config_path, self.service.config)
        if self.runtime is not None:
            self.runtime.reload_llm(self.service.config)
        label, current = self.reasoning_selection()
        return SettingsResult(True, f"{label}: {current or 'default'}", True)

    def apply_context(self, value: int) -> SettingsResult:
        max_input = self._max_input()
        if value < 2000:
            return SettingsResult(False, "Context window must be at least 2000 tokens.")
        if max_input and value > max_input:
            value = max_input
            note = f"Exceeds the model limit; capped at {format_tokens(max_input)}."
        else:
            note = ""
        if value == self.service.config.chat.context_window:
            return SettingsResult(True, "Context window unchanged.")
        self.service.config.chat.context_window = value
        if self.service.config_path:
            write_config(self.service.config_path, self.service.config)
        return SettingsResult(
            True,
            f"Context window set to {format_tokens(value)} · applies next turn. {note}".strip(),
            True,
        )

    # ---------- 内部 ----------

    def _preset(self, provider: str):
        preset = next((item for item in PRESETS if item.key == provider), None)
        return None if preset is None or preset.key == "custom" else preset

    def _max_input(self) -> int | None:
        max_input = model_metadata(self.service.config.llm.litellm_model()).get("max_input_tokens")
        try:
            return int(max_input) if max_input else None
        except (TypeError, ValueError):
            return None
