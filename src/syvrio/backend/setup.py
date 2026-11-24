"""本机离线配置用例，在后端独占目标 Vault 时处理配置与凭据。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from ..infra.config import (
    Config,
    LLMConfig,
    UsageLimit,
    context_window_options,
    default_usage_limits,
    read_api_key,
    write_config,
)
from ..infra.llm.providers import PRESETS as PRESETS
from ..infra.llm.providers import ProviderPreset as ProviderPreset
from ..infra.llm.providers import normalize_provider_url as normalize_provider_url
from ..infra.llm.providers import provider_base_url as provider_base_url
from ..infra.search.providers.catalog import PROVIDERS, supports_fetch
from ..infra.search.providers.searxng import normalize_base_url
from ..infra.search.usage import UsageLimiter
from ..infra.security.credentials import delete_api_key, environment_key, save_api_key
from .maintenance import configured_maintenance, prepare_vault_runtime

SEARCH_PROVIDER_LABELS = {name: spec.label for name, spec in PROVIDERS.items()}
SEARCH_KEY_ENVS = {name: spec.key_env for name, spec in PROVIDERS.items() if spec.key_env}
ANONYMOUS_SEARCH_PROVIDERS = {name for name, spec in PROVIDERS.items() if spec.optional_key}


def test_connection(llm: LLMConfig) -> tuple[bool, str]:
    api_key = llm.api_key()
    if not api_key:
        return False, "没有可用的 API Key"
    try:
        from ..infra.llm import complete

        complete(
            llm,
            [{"role": "user", "content": "ping"}],
            temperature=0,
            max_tokens=1,
        )
        return True, "连接成功"
    except Exception as exc:
        return False, str(exc)[:300]


@contextmanager
def local_setup(config_path: Path | None) -> Iterator[SetupSession]:
    with configured_maintenance(config_path) as (config, path):
        prepare_vault_runtime(config.paths(path))
        yield SetupSession(config, path)


@dataclass
class SetupSession:
    _config: Config
    path: Path

    def save(self) -> None:
        write_config(self.path, self._config)

    def has_key(self, name: str) -> bool:
        return bool(read_api_key(name))

    def key_from_environment(self, name: str) -> bool:
        return bool(environment_key(name))

    def set_key(self, name: str, value: str) -> bool:
        return save_api_key(name, value)

    def remove_key(self, name: str) -> bool:
        return delete_api_key(name)

    def set_search_endpoint(self, value: str) -> str:
        endpoint = normalize_base_url(value)
        self._config.search.searxng_base_url = endpoint
        return endpoint

    def usage(self) -> UsageLimiter:
        return UsageLimiter(
            self._config.search.usage_limits, self._config.paths(self.path).state_dir / "usage.json"
        )

    def usage_rows(self):
        limiter = self.usage()
        for name, default in default_usage_limits().items():
            yield (
                name,
                default,
                self._config.search.usage_limits.get(name, default),
                limiter.status(name),
            )

    def change_usage(self, name: str, action: str, value: int = 0) -> None:
        defaults = default_usage_limits()
        if action == "resume":
            self.usage().resume(name)
        elif action == "defaults":
            self._config.search.usage_limits[name] = defaults[name]
        elif action in ("daily", "monthly") and value >= 0:
            old = self._config.search.usage_limits.get(name, defaults.get(name, UsageLimit()))
            self._config.search.usage_limits[name] = replace(old, **{action: value})
        else:
            raise ValueError("请输入非负整数。")

    @property
    def search_endpoint(self) -> str:
        return self._config.search.searxng_base_url

    def sources(self, capability: str) -> tuple[str, ...]:
        return tuple(self._sources(capability))

    def _sources(self, capability: str) -> list[str]:
        return (
            self._config.search.providers
            if capability == "search"
            else self._config.fetch.providers
        )

    def can_toggle_fetch(self, name: str) -> bool:
        # 已勾选的厂商即使当前传输不支持抓取，也必须能从配置中移除。
        return name in self._config.fetch.providers or supports_fetch(
            name, self._config.search.transport
        )

    def toggle(self, name: str, capability: str) -> str:
        if name not in PROVIDERS or capability not in ("search", "fetch"):
            raise ValueError("Unsupported search provider or capability.")
        if capability == "fetch" and not self.can_toggle_fetch(name):
            raise ValueError("This provider cannot fetch using the configured transport.")
        names = self._sources(capability)
        if name in names:
            names.remove(name)
            return ""
        env = SEARCH_KEY_ENVS.get(name)
        available = not env or self.has_key(env)
        if not available and name not in ANONYMOUS_SEARCH_PROVIDERS:
            return f"{name} 未检测到有效 Key（环境变量或系统凭据），无法启用。"
        names.append(name)
        default = default_usage_limits().get(name)
        if env and default:
            self._config.search.usage_limits.setdefault(name, default)
        return "" if available else f"{name} 未配置 Key，将以匿名模式运行。"

    def move(self, capability: str, name: str, offset: int) -> None:
        names = self._sources(capability)
        index = names.index(name)
        target = index + offset
        if 0 <= target < len(names):
            names[index], names[target] = names[target], names[index]

    def model_settings(self) -> dict:
        cfg = self._config
        return {
            "provider": cfg.llm.provider,
            "model": cfg.llm.model,
            "base_url": cfg.llm.base_url,
            "api_key_env": cfg.llm.api_key_env,
            "reasoning_effort": cfg.llm.reasoning_effort,
            "context_window": cfg.chat.context_window,
            "vault": str(cfg.vault),
        }

    def default_url(self, preset: ProviderPreset) -> str:
        return provider_base_url(self._config.llm, preset) or "https://"

    def _model(self, provider: str, base_url: str, env_name: str, model: str = "") -> LLMConfig:
        return replace(
            self._config.llm,
            provider=provider,
            base_url=normalize_provider_url(base_url),
            api_key_env=env_name.strip(),
            model=model.strip(),
        )

    def model_list(self, provider: str, base_url: str, env_name: str, key: str) -> list[str]:
        source = self._model(provider, base_url, env_name)
        from ..infra.llm import fetch_provider_models

        return fetch_provider_models(
            source, refresh=True, api_key=read_api_key(source.api_key_env, proposed=key) or None
        )

    def model_limits(self, provider: str, base_url: str, env_name: str, model: str, key: str):
        source = self._model(provider, base_url, env_name, model)
        from ..infra.llm import model_metadata, reasoning_values

        try:
            maximum = (
                int(model_metadata(source.litellm_model()).get("max_input_tokens") or 0) or None
            )
        except (ValueError, TypeError):
            maximum = None
        return (
            maximum,
            reasoning_values(
                source, api_key=read_api_key(source.api_key_env, proposed=key) or None
            ),
            source.spark_thinking if source.is_spark else source.reasoning_effort,
            context_window_options(maximum),
        )

    def save_model(
        self,
        provider: str,
        base_url: str,
        env_name: str,
        model: str,
        selected: str | None,
        context_window: int,
    ) -> None:
        source = self._model(provider, base_url, env_name, model)
        config = self._config
        config.llm = replace(
            source,
            reasoning_effort=source.reasoning_effort if source.is_spark else selected,
            spark_thinking=(selected or "") if source.is_spark else source.spark_thinking,
            provider_urls={
                **({config.llm.provider: config.llm.base_url} if config.llm.base_url else {}),
                **config.llm.provider_urls,
                source.provider: source.base_url,
            },
            recent_models=config.llm.recent_model_ids(),
        )
        config.llm.recent_models = config.llm.recent_model_ids()
        config.chat.context_window = context_window
        self.save()

    def test_model(self) -> tuple[bool, str]:
        return test_connection(self._config.llm)

    def needs_initialization(self) -> bool:
        return not (self._config.vault / ".git").exists()

    def initialize(self) -> None:
        from .maintenance import initialize_owned_vault

        initialize_owned_vault(self._config, self.path)
