from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import questionary
from prompt_toolkit.styles import Style
from rich.console import Console

from .config import (
    CONFIG_FILENAME,
    KEYRING_SERVICE,
    ChatConfig,
    Config,
    LLMConfig,
    default_config_path,
    default_vault_path,
    load_config,
    read_api_key,
    toml_literal,
    usage_limits_toml,
    write_example_config,
)
from .dialogs import ask
from .tokens import format_tokens

console = Console()

_MENU_STYLE = Style.from_dict(
    {
        "qmark": "bold #d0d0d0",
        "question": "bold #e0e0e0",
        "pointer": "bold #d0d0d0",
        "highlighted": "bg:#3a3a3a #ffffff",
        "selected": "#d0d0d0",
        "separator": "#808080",
        "instruction": "#808080",
        "text": "#d8d8d8",
    }
)


@dataclass(frozen=True)
class ProviderPreset:
    key: str
    label: str
    base_url: str
    model: str
    api_key_env: str
    models: tuple[str, ...] = ()


PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        "deepseek",
        "DeepSeek（国内，便宜）",
        "https://api.deepseek.com/v1",
        "deepseek-flash",
        "DEEPSEEK_API_KEY",
        ("deepseek-flash", "deepseek-v4-pro"),
    ),
    ProviderPreset(
        "siliconflow",
        "硅基流动 SiliconFlow",
        "https://api.siliconflow.cn/v1",
        "deepseek-ai/DeepSeek-V3",
        "SILICONFLOW_API_KEY",
        ("deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct", "Qwen/Qwen3-32B"),
    ),
    ProviderPreset(
        "dashscope",
        "阿里云百炼",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-plus",
        "DASHSCOPE_API_KEY",
        ("qwen-plus", "qwen-max", "qwen-turbo"),
    ),
    ProviderPreset(
        "spark",
        "讯飞星火 Spark（MaaS）",
        "https://maas-api.cn-huabei-1.xf-yun.com/v2",
        "spark-x2.6",
        "SPARK_API_KEY",
        ("spark-x2.6", "spark-x2.6-4b", "spark-x2.6-preview", "spark-x2.5"),
    ),
    ProviderPreset(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        "OPENAI_API_KEY",
        ("gpt-4o-mini", "gpt-4o", "o3-mini"),
    ),
    ProviderPreset(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "openai/gpt-4o-mini",
        "OPENROUTER_API_KEY",
        ("openai/gpt-4o-mini", "anthropic/claude-sonnet-4.5", "google/gemini-2.5-flash"),
    ),
    ProviderPreset("custom", "自定义 OpenAI 兼容接口", "", "", "OPENAI_API_KEY"),
)


MODEL_LABELS = {
    "spark-x2.6": "Spark X2.6",
    "spark-x2.6-4b": "Spark X2.6 4B",
    "spark-x2.6-preview": "Spark X2.6 Preview",
    "spark-x2.5": "Spark X2.5",
    "deepseek-flash": "DeepSeek Flash",
    "deepseek-v4-pro": "DeepSeek V4 Pro",
    "deepseek-chat": "DeepSeek Chat",
    "deepseek-reasoner": "DeepSeek Reasoner",
    "deepseek-ai/DeepSeek-V3": "DeepSeek V3",
    "Qwen/Qwen2.5-72B-Instruct": "Qwen 2.5 72B",
    "Qwen/Qwen3-32B": "Qwen3 32B",
    "qwen-plus": "Qwen Plus",
    "qwen-max": "Qwen Max",
    "qwen-turbo": "Qwen Turbo",
    "gpt-4o-mini": "GPT-4o mini",
    "gpt-4o": "GPT-4o",
    "o3-mini": "o3 mini",
    "openai/gpt-4o-mini": "GPT-4o mini",
    "anthropic/claude-sonnet-4.5": "Claude Sonnet 4.5",
    "google/gemini-2.5-flash": "Gemini 2.5 Flash",
}

PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "siliconflow": "SiliconFlow",
    "dashscope": "百炼",
    "spark": "讯飞星火",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "custom": "自定义",
}


def display_model(model: str) -> str:
    return MODEL_LABELS.get(model, model)


def provider_label(provider: str) -> str:
    return PROVIDER_LABELS.get(provider, provider)


def has_api_key(preset: ProviderPreset) -> bool:
    if preset.key == "custom":
        return False
    llm = LLMConfig(
        provider=preset.key,
        base_url=preset.base_url,
        model=preset.model,
        api_key_env=preset.api_key_env,
    )
    return bool(llm.api_key())


EFFORT_CHOICES = (
    ("默认", "default"),
    ("低", "low"),
    ("中", "medium"),
    ("高", "high"),
)


def _choose_model(preset: ProviderPreset, current: str) -> str:
    options = list(preset.models)
    if current and current not in options:
        options.insert(0, current)
    if not options:
        value = ask(questionary.text("模型名称", default=current))
        if value is None or not value.strip():
            raise KeyboardInterrupt
        return value.strip()
    preferred = current if current in options else preset.model
    ordered = [preferred] + [model for model in options if model != preferred]
    ordered.append("自定义模型")
    selected = ask(
        questionary.select(
            "选择模型",
            choices=[
                questionary.Choice(
                    (display_model(model) + ("  ← 当前" if model == current else "")),
                    value=model,
                )
                for model in ordered
            ],
            style=_MENU_STYLE,
            qmark="›",
            pointer="❯",
            instruction="↑↓ 选择 · Enter 确认",
        )
    )
    if selected is None:
        raise KeyboardInterrupt
    if selected == "自定义模型":
        value = ask(questionary.text("模型名称", default=current))
        if value is None or not value.strip():
            raise KeyboardInterrupt
        return value.strip()
    return selected


def _choose_reasoning(params: tuple[str, ...], current: str | None) -> str | None:
    if "reasoning_effort" not in params:
        console.print("[dim]该模型不支持 reasoning_effort，跳过思考强度。[/dim]")
        return None
    efforts = list(EFFORT_CHOICES)
    if current:
        efforts.sort(key=lambda item: 0 if item[1] == current else 1)
    selected = ask(
        questionary.select(
            "思考强度",
            choices=[questionary.Choice(label, value=value) for label, value in efforts],
            style=_MENU_STYLE,
            qmark="›",
            pointer="❯",
            instruction="↑↓ 选择 · Enter 确认",
        )
    )
    if selected is None:
        raise KeyboardInterrupt
    return None if selected == "default" else selected


def _choose_context(max_input: int | None, current: int) -> int:
    if max_input:
        options = [value for value in (16000, 32000, 64000, 128000, 256000) if value <= max_input]
        if not options:
            options = [max_input]
    else:
        options = [16000, 32000, 64000]
    default = min(options, key=lambda value: abs(value - current))
    options = [default] + [value for value in options if value != default]
    selected = ask(
        questionary.select(
            "上下文长度",
            choices=[questionary.Choice(format_tokens(value), value=value) for value in options],
            style=_MENU_STYLE,
            qmark="›",
            pointer="❯",
            instruction="↑↓ 选择 · Enter 确认",
        )
    )
    if selected is None:
        raise KeyboardInterrupt
    return int(selected)


def _capabilities(provider: str, base_url: str, model: str, env_name: str):
    from ..llm import model_metadata, supported_params

    llm = LLMConfig(provider=provider, base_url=base_url, model=model, api_key_env=env_name)
    key = llm.litellm_model()
    return model_metadata(key), supported_params(key)


def save_api_key(env_name: str, api_key: str) -> bool:
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, env_name, api_key)
        return True
    except Exception:
        return False


def delete_api_key(env_name: str) -> bool:
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, env_name)
        return True
    except Exception:
        return False


def test_connection(llm: LLMConfig) -> tuple[bool, str]:
    api_key = llm.api_key()
    if not api_key:
        return False, "没有可用的 API Key"
    try:
        from ..llm import complete

        complete(
            llm,
            [{"role": "user", "content": "ping"}],
            temperature=0,
            max_tokens=1,
        )
        return True, "连接成功"
    except Exception as exc:
        return False, str(exc)[:300]


def write_config(path: Path, config: Config) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f'''# Syvrio 配置（由 syvrio setup 生成）
vault = {toml_literal(config.vault.as_posix())}
runtime = {toml_literal(config.runtime or "~/.syvrio/runtime")}

confidence_threshold = {config.confidence_threshold}
auto_push = {str(config.auto_push).lower()}

[git]
auto_sync = {str(config.git.auto_sync).lower()}
sync_time = {toml_literal(config.git.sync_time)}
retry_interval_minutes = {config.git.retry_interval_minutes}

[llm]
provider = {toml_literal(config.llm.provider)}
base_url = {toml_literal(config.llm.base_url)}
model = {toml_literal(config.llm.model)}
api_key_env = {toml_literal(config.llm.api_key_env)}
reasoning_effort = {toml_literal(config.llm.reasoning_effort or '')}
spark_thinking = {toml_literal(config.llm.spark_thinking)}
fallbacks = {toml_literal(config.llm.fallbacks)}
num_retries = {config.llm.num_retries}
temperature = {config.llm.temperature}
timeout = {config.llm.timeout}

[chat]
context_window = {config.chat.context_window}
reserve_output_tokens = {config.chat.reserve_output_tokens}
keep_recent_tokens = {config.chat.keep_recent_tokens}
compact_threshold = {config.chat.compact_threshold}
max_tool_steps = {config.chat.max_tool_steps}
tool_timeout = {config.chat.tool_timeout}
auto_recall = {config.chat.auto_recall}
recall_rewrite = {str(config.chat.recall_rewrite).lower()}
recall_context_turns = {config.chat.recall_context_turns}
extract_on_exit = {str(config.chat.extract_on_exit).lower()}

[retrieval]
method = {toml_literal(config.retrieval.method)}

[retrieval.embedding]
backend = {toml_literal(config.retrieval.embedding.backend)}
model = {toml_literal(config.retrieval.embedding.model)}
provider = {toml_literal(config.retrieval.embedding.provider)}
base_url = {toml_literal(config.retrieval.embedding.base_url)}
api_key_env = {toml_literal(config.retrieval.embedding.api_key_env)}

[search]
enabled = {str(config.search.enabled).lower()}
providers = {toml_literal(config.search.providers)}
base_url = {toml_literal(config.search.base_url)}
timeout = {config.search.timeout}
max_results = {config.search.max_results}
cache_ttl = {config.search.cache_ttl}
transport = {toml_literal(config.search.transport)}
auto_start = {str(config.search.auto_start).lower()}
engines = {toml_literal(config.search.engines)}
fetch_timeout = {config.search.fetch_timeout}
fallback_engines = {toml_literal(config.search.fallback_engines)}
max_engines = {config.search.max_engines}
max_search_engines = {config.search.max_search_engines}
timeout_budget = {config.search.timeout_budget}
max_requests = {config.search.max_requests}
anysearch_zone = {toml_literal(config.search.anysearch_zone)}
anysearch_language = {toml_literal(config.search.anysearch_language)}
searxng_base_url = {toml_literal(config.search.searxng_base_url)}
searxng_timeout = {config.search.searxng_timeout}
{usage_limits_toml(config.search.usage_limits)}
[fetch]
providers = {toml_literal(config.fetch.providers)}
'''
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    # 先写临时文件再替换，避免中断时把现有配置截成空文件。
    os.replace(tmp, path)


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


def run_setup(config_path: Path | None = None, vault: Path | None = None) -> dict:
    """syvrio setup 顶层入口：先选配置内容，再进入对应向导。"""
    path = resolve_config_path(config_path)
    config = load_config(path) if path.exists() else Config(vault=default_vault_path())
    if vault is not None:
        config.vault = vault.expanduser().resolve()

    choice = ask(
        questionary.select(
            "配置内容",
            choices=[
                questionary.Choice("模型与 LLM", value="llm"),
                questionary.Choice("搜索与网页抓取 API", value="search"),
            ],
            style=_MENU_STYLE,
            qmark="›",
            pointer="❯",
            instruction="↑↓ 选择 · Enter 确认",
        )
    )
    if choice is None:
        raise KeyboardInterrupt
    if choice == "search":
        return run_search_setup(config, path)
    return _run_llm_setup(config, path)


SEARCH_PROVIDER_LABELS = {
    "open_websearch": "open-websearch（免费，无需 Key）",
    "anysearch": "AnySearch（匿名可用，可选 Key）",
    "parallel_mcp": "Parallel MCP（匿名可用，可选 Key）",
    "searxng": "自托管 SearXNG（无需 Key）",
    "baidu_search": "百度千帆 AI 搜索",
    "tavily": "Tavily",
    "exa": "Exa",
    "you": "You.com",
    "volcengine": "火山引擎豆包搜索",
}

# 各官方厂商的 Key 环境变量名；open-websearch 无需 Key。
SEARCH_KEY_ENVS = {
    "anysearch": "ANYSEARCH_API_KEY",
    "parallel_mcp": "PARALLEL_API_KEY",
    "baidu_search": "BAIDU_SEARCH_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "exa": "EXA_API_KEY",
    "you": "YDC_API_KEY",
    "volcengine": "VOLCENGINE_SEARCH_API_KEY",
}

# 没有 Key 也能启用；有 Key 时自动切换到认证模式。
ANONYMOUS_SEARCH_PROVIDERS = {"anysearch", "parallel_mcp"}


def _ensure_usage_limit(config: Config, name: str, messages: list[str]) -> None:
    from .config import default_usage_limits

    if name not in config.search.usage_limits:
        default = default_usage_limits().get(name)
        if default is not None:
            config.search.usage_limits[name] = default


def _manage_usage(config: Config) -> None:
    from .config import UsageLimit, default_usage_limits
    from .search.usage import UsageLimiter

    defaults = default_usage_limits()
    limiter = UsageLimiter(config.search.usage_limits, config.paths().state_dir / "usage.json")
    while True:
        for name, default in defaults.items():
            effective = config.search.usage_limits.get(name, default)
            state = limiter.status(name)
            def display(value):
                return str(value) if value else "不限"
            def remaining(value):
                return "不限" if value is None else str(value)
            console.print(f"{name}: 默认 daily={display(default.daily)}, monthly={display(default.monthly)}; "
                          f"生效 daily={display(effective.daily)}, monthly={display(effective.monthly)}; "
                          f"已使用/剩余 daily={state['daily']['used']}/{remaining(state['daily']['remaining'])}, "
                          f"monthly={state['monthly']['used']}/{remaining(state['monthly']['remaining'])}; "
                          f"官方暂停={state['paused'] or '无'}; 历史未知周期={state['legacy_unknown_period'] or '无'}")
        name = _select("额度管理：选择厂商（本地请求次数不是厂商余额）", [
            *[questionary.Choice(label, value=name) for name, label in SEARCH_PROVIDER_LABELS.items()],
            questionary.Choice("返回", value=None),
        ])
        if name is None:
            return
        action = _select("额度操作", [
            questionary.Choice("修改每日上限（0 不限）", value="daily"),
            questionary.Choice("修改每月上限（0 不限）", value="monthly"),
            questionary.Choice("恢复默认上限", value="defaults"),
            questionary.Choice("解除官方暂停（不清空计数）", value="resume"),
            questionary.Choice("返回", value=None),
        ])
        if action in ("daily", "monthly"):
            raw = ask(questionary.text("请求次数上限（非负整数，0 不限）"))
            if raw is None:
                continue
            try:
                value = int(str(raw).strip())
                if value < 0:
                    raise ValueError
            except ValueError:
                console.print("请输入非负整数。")
                continue
            old = config.search.usage_limits.get(name, defaults.get(name, UsageLimit()))
            values = {"daily": old.daily, "monthly": old.monthly, action: value}
            config.search.usage_limits[name] = UsageLimit(**values)
        elif action == "defaults":
            config.search.usage_limits[name] = defaults[name]
        elif action == "resume":
            limiter.resume(name)


def _toggle_provider(names: list[str], name: str, enable: bool) -> None:
    if enable and name not in names:
        names.append(name)
    elif not enable and name in names:
        names.remove(name)


def _move_provider(names: list[str], name: str, offset: int) -> None:
    index = names.index(name)
    target = index + offset
    if 0 <= target < len(names):
        names[index], names[target] = names[target], names[index]


def _select(message: str, choices: list):
    return ask(
        questionary.select(
            message,
            choices=choices,
            style=_MENU_STYLE,
            qmark="›",
            pointer="❯",
            instruction="↑↓ 选择 · Enter 确认",
        )
    )


def _provider_state(config: Config, name: str) -> str:
    marks = []
    if name in config.search.providers:
        marks.append("Search")
    if name in config.fetch.providers:
        marks.append("Fetch")
    return f"（{'/'.join(marks)}已启用）" if marks else ""


def _configure_search_provider(config: Config, messages: list[str]) -> None:
    from .search.fetch_router import FETCH_PROVIDERS

    choices = [
        questionary.Choice(f"{label}{_provider_state(config, name)}", value=name)
        for name, label in SEARCH_PROVIDER_LABELS.items()
    ]
    name = _select("选择厂商", choices)
    if name is None:
        raise KeyboardInterrupt
    env_name = SEARCH_KEY_ENVS.get(name)
    if name == "searxng":
        console.print(
            "[dim]自托管 SearXNG 无需 API Key；请确认实例已在 settings.yml 启用 JSON 输出。[/dim]"
        )
        from .search.searxng import normalize_base_url

        current = config.search.searxng_base_url
        raw = ask(
            questionary.text(
                f"SearXNG 实例地址（当前 {current}；留空保持不变）"
            )
        )
        if raw is not None and str(raw).strip():
            try:
                config.search.searxng_base_url = normalize_base_url(str(raw))
                messages.append(
                    f"SearXNG 实例地址已设为 {config.search.searxng_base_url}。"
                )
            except ValueError as exc:
                messages.append(f"SearXNG 地址未保存：{exc}")
    elif env_name is None:
        console.print("[dim]open-websearch 免费且无需 Key，本地 daemon 即可用。[/dim]")
    else:
        current_key = read_api_key(env_name)
        if name in ANONYMOUS_SEARCH_PROVIDERS:
            state = "已配置" if current_key else "未配置（匿名模式）"
            console.print("[dim]不填写 Key 也可以启用并使用匿名搜索。[/dim]")
        else:
            state = "已配置" if current_key else "未配置"
        api_key = ask(
            questionary.password(
                f"{SEARCH_PROVIDER_LABELS[name]} API Key（当前{state}；留空保持不变）"
            )
        )
        if api_key and api_key.strip():
            if save_api_key(env_name, api_key.strip()):
                messages.append(
                    f"{name} 的 API Key 已保存到系统凭据（环境变量 {env_name} 优先）。"
                )
            else:
                messages.append(
                    f"系统凭据存储不可用，{name} 的 Key 未保存明文，请改用环境变量 {env_name}。"
                )
    while True:
        search_on = name in config.search.providers
        fetch_on = name in config.fetch.providers
        action = _select(
            f"配置 {SEARCH_PROVIDER_LABELS[name]}",
            [
                questionary.Choice(
                    f"{'禁用' if search_on else '启用'} Search", value="search"
                ),
                *([questionary.Choice(
                    f"{'禁用' if fetch_on else '启用'} Fetch", value="fetch"
                )] if name in FETCH_PROVIDERS else []),
                *([questionary.Choice("清除 API Key（恢复匿名）", value="clear_key")]
                  if env_name and name in ANONYMOUS_SEARCH_PROVIDERS
                  and read_api_key(env_name) else []),
                questionary.Choice("返回上级", value="back"),
            ],
        )
        if action is None or action == "back":
            return
        if action == "clear_key":
            if delete_api_key(env_name):
                messages.append(
                    f"{name} 的系统凭据已清除；若环境变量 {env_name} 仍非空，"
                    "环境变量会继续优先使用。"
                )
            else:
                messages.append(f"{name} 的系统凭据清除失败，请检查系统凭据存储。")
            continue
        if action in ("search", "fetch"):
            enable = not (search_on if action == "search" else fetch_on)
            if (
                enable
                and env_name
                and name not in ANONYMOUS_SEARCH_PROVIDERS
                and not read_api_key(env_name)
            ):
                messages.append(
                    f"{name} 未检测到有效 Key（环境变量或系统凭据），无法启用。"
                )
                continue
            names = (
                config.search.providers if action == "search" else config.fetch.providers
            )
            _toggle_provider(names, name, enable)
            if enable and env_name and name in ANONYMOUS_SEARCH_PROVIDERS \
                    and not read_api_key(env_name):
                messages.append(f"{name} 未配置 Key，将以匿名模式运行。")
            if enable and env_name:
                _ensure_usage_limit(config, name, messages)


def _adjust_order(names: list[str], label: str) -> None:
    if not names:
        console.print(f"[dim]{label} 当前没有启用的 provider。[/dim]")
        return
    while True:
        current = " → ".join(
            SEARCH_PROVIDER_LABELS.get(name, name) for name in names
        )
        choices = [
            questionary.Choice(
                f"{SEARCH_PROVIDER_LABELS.get(name, name)}", value=name
            )
            for name in names
        ]
        choices.append(questionary.Choice("完成", value=None))
        name = _select(f"{label} 当前顺序：{current}（选择要移动的厂商）", choices)
        if name is None:
            return
        direction = _select(
            f"移动 {SEARCH_PROVIDER_LABELS.get(name, name)}",
            [
                questionary.Choice("上移", value=-1),
                questionary.Choice("下移", value=1),
            ],
        )
        if direction is None:
            return
        _move_provider(names, name, direction)


def run_search_setup(config: Config, path: Path) -> dict:
    """搜索与网页抓取向导：Key、启停与优先级；TOML 只写非敏感配置。"""
    messages: list[str] = []
    while True:
        action = _select(
            "搜索与网页抓取配置",
            [
                questionary.Choice("选择厂商：配置 Key / 启用停用", value="providers"),
                questionary.Choice("调用额度与官方暂停管理", value="usage"),
                questionary.Choice("调整 Search 优先级", value="search_order"),
                questionary.Choice("调整 Fetch 优先级", value="fetch_order"),
                questionary.Choice("保存并返回", value="done"),
            ],
        )
        if action is None or action == "done":
            break
        if action == "providers":
            _configure_search_provider(config, messages)
        elif action == "usage":
            _manage_usage(config)
        elif action == "search_order":
            _adjust_order(config.search.providers, "Search")
        elif action == "fetch_order":
            _adjust_order(config.fetch.providers, "Fetch")
    write_config(path, config)
    return {
        "mode": "search",
        "config_path": str(path),
        "search_providers": list(config.search.providers),
        "fetch_providers": list(config.fetch.providers),
        "messages": messages,
    }


def _run_llm_setup(config: Config, path: Path) -> dict:
    choice = ask(
        questionary.select(
            "选择模型供应商",
            choices=[questionary.Choice(title=preset.label, value=preset) for preset in PRESETS],
            style=_MENU_STYLE,
            qmark="›",
            pointer="❯",
            instruction="↑↓ 选择 · Enter 确认",
        )
    )
    if choice is None:
        raise KeyboardInterrupt
    preset: ProviderPreset = choice

    if preset.key == "custom":
        base_url = ask(questionary.text("Base URL", default=config.llm.base_url or "https://"))
        env_name = ask(questionary.text("API Key 环境变量名", default=config.llm.api_key_env))
        if base_url is None or env_name is None:
            raise KeyboardInterrupt
        model = _choose_model(preset, config.llm.model)
        provider = "custom"
    else:
        base_url = preset.base_url
        env_name = preset.api_key_env
        current_model = config.llm.model if config.llm.provider == preset.key else preset.model
        model = _choose_model(preset, current_model)
        provider = preset.key

    metadata, params = _capabilities(provider, base_url.strip(), model, env_name.strip())
    max_input = metadata.get("max_input_tokens")
    if max_input is not None:
        try:
            max_input = int(max_input)
        except (TypeError, ValueError):
            max_input = None
    if max_input:
        console.print(f"模型上下文上限：{format_tokens(max_input)} tokens")
    effort = _choose_reasoning(params, config.llm.reasoning_effort)
    context_window = _choose_context(max_input, config.chat.context_window)
    console.print(
        f"使用 {preset.label}：{model} · 上下文 {context_window} · "
        f"思考强度 {effort or '默认'}"
    )

    api_key = ask(
        questionary.password(
            "API Key（不会写入配置文件；留空则跳过）"
        )
    )
    key_saved = False
    keyring_error = ""
    if api_key:
        key_saved = save_api_key(env_name.strip(), api_key.strip())
        if not key_saved:
            keyring_error = "系统凭据存储不可用，请改用环境变量。"

    config.llm = LLMConfig(
        provider=provider,
        base_url=base_url.strip(),
        model=model.strip(),
        api_key_env=env_name.strip(),
        reasoning_effort=effort,
        spark_thinking=config.llm.spark_thinking,
        fallbacks=config.llm.fallbacks,
        num_retries=config.llm.num_retries,
        temperature=config.llm.temperature,
        timeout=config.llm.timeout,
    )
    config.chat.context_window = context_window
    write_config(path, config)

    check = False
    detail = "已跳过连接测试"
    if api_key and key_saved:
        if ask(questionary.confirm("现在测试连接？", default=True)):
            check, detail = test_connection(config.llm)

    initialized = False
    if not (config.vault / ".git").exists():
        if ask(questionary.confirm(f"初始化记忆库 {config.vault}？", default=True)):
            from ..memory.service import MemoryService

            MemoryService(config).init_vault()
            initialized = True

    return {
        "config_path": str(path),
        "provider": config.llm.provider,
        "base_url": config.llm.base_url,
        "model": config.llm.model,
        "api_key_env": config.llm.api_key_env,
        "reasoning_effort": config.llm.reasoning_effort,
        "context_window": config.chat.context_window,
        "key_saved": key_saved,
        "keyring_error": keyring_error,
        "checked": check,
        "check_detail": detail,
        "initialized": initialized,
        "vault": str(config.vault),
    }


def write_default_config(path: Path, vault: Path) -> None:
    write_example_config(path, vault)
