from __future__ import annotations

import math
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ..security.credentials import KEYRING_SERVICE as KEYRING_SERVICE
from ..security.credentials import stored_key
from ..storage.paths import SyvrioPaths, runtime_path, syvrio_home
from .files import (
    MEMORY_SECTIONS,
    ConfigError,
    ConfigFiles,
    file_for,
    merge,
    package_defaults,
)
from .files import (
    toml_literal as toml_literal,
)

DEFAULTS = package_defaults()
DEFAULT_BASE_URL = DEFAULTS["llm"]["base_url"]
DEFAULT_MODEL = DEFAULTS["llm"]["model"]
DEFAULT_API_KEY_ENV = DEFAULTS["llm"]["api_key_env"]
QIANWEN_BASE_URL = "https://maas.qianwenaiapi.com/compatible-mode/v1"
QIANWEN_API_KEY_ENV = "QIANWEN_API_KEY"
MIN_CONTEXT_WINDOW = 50_000
CONTEXT_WINDOW_PRESETS = (MIN_CONTEXT_WINDOW, 100_000, 200_000, 400_000, 600_000)


def context_window_options(maximum: int | None) -> list[int]:
    return [size for size in CONTEXT_WINDOW_PRESETS if not maximum or size <= maximum] or [
        MIN_CONTEXT_WINDOW
    ]


DEFAULT_CONTEXT_WINDOW = DEFAULTS["chat"]["context_window"]
DEFAULT_EMBEDDING_MODEL = DEFAULTS["retrieval"]["embedding"]["model"]
DEFAULT_EMBEDDING_BASE_URL = DEFAULTS["retrieval"]["embedding"]["base_url"]
SPARK_THINKING_MODES = ("auto", "disabled", "enabled")
CONFIG_FILENAME = "syvrio.toml"
RECENT_MODEL_LIMIT = 5
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(slots=True)
class LLMConfig:
    provider: str = DEFAULTS["llm"]["provider"]
    base_url: str = DEFAULTS["llm"]["base_url"]
    model: str = DEFAULTS["llm"]["model"]
    api_key_env: str = DEFAULTS["llm"]["api_key_env"]
    reasoning_effort: str | None = DEFAULTS["llm"]["reasoning_effort"] or None
    num_retries: int = DEFAULTS["llm"]["num_retries"]
    temperature: float = DEFAULTS["llm"]["temperature"]
    timeout: float = DEFAULTS["llm"]["timeout"]
    spark_thinking: str = DEFAULTS["llm"]["spark_thinking"]
    provider_urls: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULTS["llm"]["provider_urls"])
    )
    recent_models: list[tuple[str, str]] = field(
        default_factory=lambda: list(DEFAULTS["llm"]["recent_models"])
    )

    def recent_model_ids(self) -> list[tuple[str, str]]:
        models = [(self.provider, self.model), *self.recent_models]
        return list(
            dict.fromkeys(
                (provider, model)
                for provider, model in models
                if provider.strip() and model.strip()
            )
        )[:RECENT_MODEL_LIMIT]

    @property
    def is_spark(self) -> bool:
        return self.provider.strip().lower() == "spark"

    @property
    def uses_qianwen_api(self) -> bool:
        return (
            self.provider.strip().lower() == "qianwen"
            and self.base_url.rstrip("/") == QIANWEN_BASE_URL
        )

    def litellm_model(self) -> str:
        provider = (self.provider or "").strip().lower()
        model = (self.model or "").strip()
        # 这些接口遵循 OpenAI 的传输协议，包括带命名空间的模型 ID。
        if provider in ("", "custom", "openai-compatible", "siliconflow", "spark", "qianwen"):
            return model if model.startswith("openai/") else f"openai/{model}"
        if model.startswith(f"{provider}/"):
            return model
        return f"{provider}/{model}"

    def requires_api_key(self) -> bool:
        """本地模型可以匿名访问，远程模型使用凭据库保存的密钥。"""
        return (self.provider or "").strip().lower() not in {"ollama", "local", "lmstudio"}

    def api_key(self) -> str | None:
        return read_api_key(self.api_key_env) or None


@dataclass(slots=True)
class ChatConfig:
    context_window: int = DEFAULTS["chat"]["context_window"]
    reserve_output_tokens: int = DEFAULTS["chat"]["reserve_output_tokens"]
    compact_threshold: float = DEFAULTS["chat"]["compact_threshold"]
    max_tool_steps: int = DEFAULTS["chat"]["max_tool_steps"]
    tool_timeout: float = DEFAULTS["chat"]["tool_timeout"]
    auto_recall: int = DEFAULTS["recall"]["auto_recall"]
    recall_rewrite: bool = DEFAULTS["recall"]["recall_rewrite"]
    recall_context_turns: int = DEFAULTS["recall"]["recall_context_turns"]
    auto_reflect: bool = DEFAULTS["dream"]["auto_reflect"]
    dream_auto_interval_hours: float = DEFAULTS["dream"]["dream_auto_interval_hours"]
    extract_on_exit: bool = DEFAULTS["distillation"]["extract_on_exit"]
    cue_limit: int = DEFAULTS["recall"]["cue_limit"]
    rewrite_timeout: float = DEFAULTS["recall"]["rewrite_timeout"]
    recall_limit: int = DEFAULTS["distillation"]["recall_limit"]
    default_budget: int = DEFAULTS["dream"]["default_budget"]


@dataclass(slots=True)
class GitConfig:
    auto_sync: bool = DEFAULTS["git"]["auto_sync"]
    sync_time: str = DEFAULTS["git"]["sync_time"]
    retry_interval_minutes: int = DEFAULTS["git"]["retry_interval_minutes"]


@dataclass(slots=True)
class EmbeddingConfig:
    backend: str = DEFAULTS["retrieval"]["embedding"]["backend"]
    model: str = DEFAULTS["retrieval"]["embedding"]["model"]
    provider: str = DEFAULTS["retrieval"]["embedding"]["provider"]
    base_url: str = DEFAULTS["retrieval"]["embedding"]["base_url"]
    api_key_env: str = DEFAULTS["retrieval"]["embedding"]["api_key_env"]


@dataclass(slots=True)
class RetrievalConfig:
    default_limit: int = DEFAULTS["retrieval"]["default_limit"]
    method: str = DEFAULTS["retrieval"]["method"]
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)


@dataclass(slots=True)
class SearchConfig:
    enabled: bool = DEFAULTS["search"]["enabled"]
    providers: list[str] = field(default_factory=lambda: list(DEFAULTS["search"]["providers"]))
    base_url: str = DEFAULTS["search"]["base_url"]
    timeout: float = DEFAULTS["search"]["timeout"]
    max_results: int = DEFAULTS["search"]["max_results"]
    cache_ttl: int = DEFAULTS["search"]["cache_ttl"]
    transport: str = DEFAULTS["search"]["transport"]
    auto_start: bool = DEFAULTS["search"]["auto_start"]
    # 上游搜索引擎；空列表表示沿用 daemon 的 DEFAULT_SEARCH_ENGINE（默认 bing）。
    engines: list[str] = field(default_factory=lambda: list(DEFAULTS["search"]["engines"]))
    fetch_timeout: float = DEFAULTS["search"]["fetch_timeout"]
    # 首选引擎空结果或失败时的免费候补引擎序列（仍经 open-websearch）。
    fallback_engines: list[str] = field(
        default_factory=lambda: list(DEFAULTS["search"]["fallback_engines"])
    )
    # 每次自动搜索最多尝试的上游引擎数（含首选）；显式指定引擎时不回退。
    max_engines: int = DEFAULTS["search"]["max_engines"]
    # 显式 search_engine 数组允许的最大引擎数；0 表示不限制。
    max_search_engines: int = DEFAULTS["search"]["max_search_engines"]
    # 一次搜索所有引擎回退共享的超时预算，秒；超出即停止候补。
    timeout_budget: float = DEFAULTS["search"]["timeout_budget"]
    # 一次搜索跨所有 provider 共享的请求次数上限；超出即返回当前最佳候选。
    max_requests: int = DEFAULTS["search"]["max_requests"]
    # AnySearch 的检索区域与语言；匿名和 Key 模式共用。
    anysearch_zone: str = DEFAULTS["search"]["anysearch_zone"]
    anysearch_language: str = DEFAULTS["search"]["anysearch_language"]
    # 自托管 SearXNG 实例；与 open-websearch 的 base_url 相互独立。
    searxng_base_url: str = DEFAULTS["search"]["searxng_base_url"]
    searxng_timeout: float = DEFAULTS["search"]["searxng_timeout"]
    # 每厂商调用上限（Search/Fetch 共池）；键为 provider 名。
    usage_limits: dict[str, UsageLimit] = field(default_factory=lambda: default_usage_limits())


def read_api_key(name: str) -> str:
    """按条目名读取系统凭据库中的密钥。"""
    return (stored_key(name) or "").strip()


@dataclass(slots=True)
class UsageLimit:
    daily: int = 0
    monthly: int = 0


DEFAULT_USAGE_LIMITS = {
    name: (limit["daily"], limit["monthly"])
    for name, limit in DEFAULTS["search"]["usage_limits"].items()
}


def default_usage_limits() -> dict[str, UsageLimit]:
    return {
        name: UsageLimit(daily=day, monthly=month)
        for name, (day, month) in DEFAULT_USAGE_LIMITS.items()
    }


@dataclass(slots=True)
class FetchConfig:
    # 网页抓取的 provider 顺序；与 search.providers 相互独立。
    providers: list[str] = field(default_factory=lambda: list(DEFAULTS["fetch"]["providers"]))


@dataclass(slots=True)
class WebShareConfig:
    domain: str = DEFAULTS["web_share"]["domain"]
    github_user_id: str = DEFAULTS["web_share"]["github_user_id"]


@dataclass(slots=True)
class DreamConfig:
    context_window: int = DEFAULTS["dream"]["context_window"]


@dataclass(slots=True)
class Config:
    vault: Path
    llm: LLMConfig = field(default_factory=LLMConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    git: GitConfig = field(default_factory=GitConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    auto_push: bool = DEFAULTS["auto_push"]
    runtime: str = DEFAULTS["runtime"]
    web_share: WebShareConfig = field(default_factory=WebShareConfig, kw_only=True)
    dream: DreamConfig = field(default_factory=DreamConfig, kw_only=True)
    _baseline: dict | None = field(default=None, repr=False, compare=False, kw_only=True)

    def paths(self, config_file: Path | None = None) -> SyvrioPaths:
        return SyvrioPaths(
            self.vault, runtime_path(self.runtime), config_file or default_config_path()
        )


def default_config_path() -> Path:
    env_path = os.environ.get("SYVRIO_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    local = Path.cwd() / CONFIG_FILENAME
    if local.exists():
        return local
    return syvrio_home() / "config.toml"


def default_vault_path() -> Path:
    env_vault = os.environ.get("SYVRIO_VAULT")
    if env_vault:
        return Path(env_vault).expanduser()
    return Path(DEFAULTS["vault"]).expanduser()


def _validate_types(data: dict, defaults: dict, prefix: str = "") -> None:
    # 厂商端点和用量策略允许使用动态供应商名，其余键由默认配置描述。
    if prefix not in {"llm.provider_urls", "search.usage_limits"}:
        for key in data.keys() - defaults.keys():
            name = f"{prefix}.{key}" if prefix else key
            raise ConfigError(f"未知配置字段 {name}。")
    for key, default in defaults.items():
        if key not in data:
            continue
        name = f"{prefix}.{key}" if prefix else key
        value = data[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise ConfigError(f"配置字段 {name} 必须是表（section）。")
            _validate_types(value, default, name)
        elif isinstance(default, list):
            if name == "llm.recent_models":
                if not isinstance(value, list) or any(
                    not isinstance(pair, list)
                    or len(pair) != 2
                    or any(not isinstance(part, str) or not part.strip() for part in pair)
                    for pair in value
                ):
                    raise ConfigError(
                        "配置字段 llm.recent_models 必须是 [供应商, 模型] 字符串对的数组。"
                    )
                continue
            if not isinstance(value, list):
                raise ConfigError(f"配置字段 {name} 必须是字符串数组。")
            for index, item in enumerate(value):
                if not isinstance(item, str):
                    raise ConfigError(f"配置字段 {name}[{index}] 必须是字符串。")
        elif isinstance(default, bool):
            if type(value) is not bool:
                raise ConfigError(f"配置字段 {name} 必须是布尔值（true/false）。")
        elif isinstance(default, int):
            if type(value) is not int:
                raise ConfigError(f"配置字段 {name} 必须是整数。")
        elif isinstance(default, float):
            if type(value) not in (int, float):
                raise ConfigError(f"配置字段 {name} 必须是数值。")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ConfigError(f"配置字段 {name} 必须是有限数值。")
        elif not isinstance(value, str):
            # vault 在默认对象中是 Path；reasoning_effort 的默认值为 None。
            raise ConfigError(f"配置字段 {name} 必须是字符串。")


def _validate_range(
    data: dict, name: str, *, positive: bool = False, unit_interval: bool = False
) -> None:
    value = data
    for key in name.split("."):
        if key not in value:
            return
        value = value[key]
    if unit_interval:
        if not 0 <= value <= 1:
            raise ConfigError(f"配置字段 {name} 必须在 0 到 1 之间（含边界）。")
    elif positive:
        if value <= 0:
            raise ConfigError(f"配置字段 {name} 必须大于 0。")
    elif value < 0:
        raise ConfigError(f"配置字段 {name} 必须大于或等于 0。")


def _validate_config(data: dict) -> None:
    _validate_types(data, DEFAULTS)
    if any(
        not isinstance(url, str) or not url.strip()
        for url in data.get("llm", {}).get("provider_urls", {}).values()
    ):
        raise ConfigError("配置字段 llm.provider_urls 的值必须是非空字符串。")
    if data.get("llm", {}).get("spark_thinking", "") not in ("", *SPARK_THINKING_MODES):
        raise ConfigError("配置字段 llm.spark_thinking 必须为空或为 enabled、auto、disabled。")
    for name in (
        "llm.timeout",
        "chat.max_tool_steps",
        "chat.tool_timeout",
        "search.timeout",
        "search.max_results",
        "search.fetch_timeout",
        "recall.rewrite_timeout",
        "retrieval.default_limit",
        "distillation.recall_limit",
        "dream.context_window",
        "search.max_engines",
        "search.timeout_budget",
        "search.max_requests",
        "git.retry_interval_minutes",
    ):
        _validate_range(data, name, positive=True)
    for name in (
        "llm.num_retries",
        "llm.temperature",
        "chat.reserve_output_tokens",
        "recall.auto_recall",
        "recall.recall_context_turns",
        "search.cache_ttl",
        "search.max_search_engines",
        "recall.cue_limit",
        "dream.default_budget",
    ):
        _validate_range(data, name)
    _validate_range(data, "chat.compact_threshold", unit_interval=True)
    if data.get("dream", {}).get("default_budget", 0) > 20:
        raise ConfigError("dream.default_budget 必须在 0 到 20 之间。")
    git_raw = data.get("git", {})
    if isinstance(git_raw, dict) and "sync_time" in git_raw:
        value = git_raw["sync_time"]
        if not isinstance(value, str) or not _TIME_RE.match(value):
            raise ConfigError("配置字段 git.sync_time 必须是 HH:MM（24 小时制）。")


def _settings_data(config: Config) -> dict:
    data = {
        name: asdict(getattr(config, name))
        for name in ("llm", "chat", "git", "search", "fetch", "retrieval", "web_share")
    }
    data.update(vault=config.vault.as_posix(), runtime=config.runtime, auto_push=config.auto_push)
    llm = data["llm"]
    llm["reasoning_effort"] = llm["reasoning_effort"] or ""
    llm["recent_models"] = [list(pair) for pair in llm["recent_models"]]
    for section, names in MEMORY_SECTIONS.items():
        data[section] = {name: data["chat"].pop(name) for name in names}
    data["dream"].update(asdict(config.dream))
    return data


def _options(cls, data: dict) -> dict:
    return {f.name: data[f.name] for f in fields(cls) if f.name in data}


def _build(overrides: dict, defaults: dict, paths: ConfigFiles) -> Config:
    for key, value in overrides.items():
        try:
            _validate_config({key: value})
        except ConfigError as exc:
            raise ConfigError(f"{paths.paths[file_for(key)]} ({key}): {exc}") from exc
    data = merge(defaults, overrides)
    llm = _options(LLMConfig, data["llm"])
    if llm["provider"] == "qianwen":
        configured = overrides.get("llm", {})
        llm["base_url"] = configured.get("base_url", QIANWEN_BASE_URL)
        llm["api_key_env"] = configured.get("api_key_env", QIANWEN_API_KEY_ENV)
    llm["base_url"] = llm["provider_urls"].get(llm["provider"], llm["base_url"])
    llm["reasoning_effort"] = llm["reasoning_effort"].strip() or None
    llm["recent_models"] = [tuple(pair) for pair in llm["recent_models"]]
    chat = dict(data["chat"])
    for section, names in MEMORY_SECTIONS.items():
        chat.update({name: data[section][name] for name in names})
    chat["context_window"] = max(MIN_CONTEXT_WINDOW, chat["context_window"])
    retrieval = _options(RetrievalConfig, data["retrieval"])
    retrieval["embedding"] = EmbeddingConfig(**_options(EmbeddingConfig, retrieval["embedding"]))
    search = _options(SearchConfig, data["search"])
    for key in ("engines", "fallback_engines", "providers"):
        values = [s for s in search[key] if s.strip()]
        if key != "providers":
            values = list(dict.fromkeys(s.strip() for s in values))
        search[key] = values or defaults["search"][key]
    limits = {}
    for key, values in search["usage_limits"].items():
        if not isinstance(values, dict):
            raise ConfigError(
                f"{paths.paths['config.toml']}: search.usage_limits.{key} must be a table"
            )
        if set(values) - {"daily", "monthly"}:
            raise ConfigError(
                f"{paths.paths['config.toml']}: search.usage_limits.{key} accepts only daily and monthly"
            )
        item = UsageLimit(**values)
        if any(type(n) is not int or n < 0 for n in (item.daily, item.monthly)):
            raise ConfigError(
                f"{paths.paths['config.toml']}: search.usage_limits.{key} must use nonnegative integers"
            )
        limits[key] = item
    search["usage_limits"] = limits
    config = Config(
        vault=Path(
            overrides.get("vault") or os.environ.get("SYVRIO_VAULT") or defaults["vault"]
        ).expanduser(),
        runtime=data["runtime"],
        auto_push=data["auto_push"],
        llm=LLMConfig(**llm),
        chat=ChatConfig(**_options(ChatConfig, chat)),
        dream=DreamConfig(**_options(DreamConfig, data["dream"])),
        git=GitConfig(**_options(GitConfig, data["git"])),
        retrieval=RetrievalConfig(**retrieval),
        search=SearchConfig(**search),
        web_share=WebShareConfig(**_options(WebShareConfig, data["web_share"])),
        fetch=FetchConfig(providers=[p for p in data["fetch"]["providers"] if p.strip()]),
    )
    config._baseline = _settings_data(config)
    return config


def load_config(path: Path | str | None = None) -> Config:
    entry = Path(path).expanduser() if path else default_config_path()
    layout = ConfigFiles(entry)
    defaults = package_defaults()
    required = path is not None or bool(os.environ.get("SYVRIO_CONFIG"))
    if required and not entry.exists() and not layout.journal.exists():
        raise ConfigError(f"指定的入口配置文件不存在：{entry}")
    with layout.lock:
        overrides = layout.read()
        if required and not entry.exists():
            raise ConfigError(f"指定的入口配置文件不存在：{entry}")
        return _build(overrides, defaults, layout)


def _apply_changes(local: dict, before: dict, after: dict, defaults: dict) -> None:
    """只更新本次调用编辑过的字段，保留并发写入的其他字段。"""
    for key in before.keys() | after.keys():
        if key in before and key in after and before[key] == after[key]:
            continue
        value = after.get(key)
        if isinstance(value, dict) and isinstance(before.get(key), dict):
            child = local.setdefault(key, {})
            _apply_changes(child, before[key], value, defaults.get(key, {}))
            if not child:
                local.pop(key, None)
        elif key not in after or value == defaults.get(key):
            local.pop(key, None)
        else:
            local[key] = value


def write_config(path: Path, config: Config) -> None:
    layout = ConfigFiles(path)
    defaults = package_defaults()
    current = _settings_data(config)
    with layout.lock:
        local = layout.read()
        if config._baseline is None:
            before = _settings_data(_build(local, defaults, layout))
            _apply_changes(local, before, current, defaults)
            local["vault"] = config.vault.as_posix()
        else:
            if not path.exists():
                raise ConfigError(f"指定的入口配置文件不存在：{path}")
            _apply_changes(local, config._baseline, current, defaults)
        _build(local, defaults, layout)
        layout.write(local, initialize=not path.exists())
    config._baseline = current


def write_example_config(path: Path, vault: Path) -> None:
    write_config(path, Config(vault=vault))


def resolve_config_path(config_path: Path | None) -> Path:
    if config_path:
        return config_path.expanduser()
    env_path = os.environ.get("SYVRIO_CONFIG")
    if env_path:
        # 环境变量优先于 CWD：测试隔离与显式选择配置都不应被仓库里的本地配置抢走。
        return Path(env_path).expanduser()
    local = Path.cwd() / CONFIG_FILENAME
    if local.exists():
        return local
    return default_config_path()
