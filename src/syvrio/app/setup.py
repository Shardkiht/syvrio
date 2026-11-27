"""终端配置向导，只负责输入、选择与结果展示。"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path

from ..backend.setup import (
    ANONYMOUS_SEARCH_PROVIDERS,
    PRESETS,
    SEARCH_KEY_ENVS,
    SEARCH_PROVIDER_LABELS,
    ProviderPreset,
    SetupSession,
    local_setup,
    normalize_provider_url,
)
from ..infra.formatting import format_tokens
from .dialogs import ask


@lru_cache(maxsize=1)
def _menu_style():
    from prompt_toolkit.styles import Style

    return Style.from_dict(
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


@lru_cache(maxsize=1)
def _console():
    from rich.console import Console

    return Console()


def _choose_model(models: list[str], current: str) -> str:
    import questionary

    if models:
        ordered = ([current] if current in models else []) + [
            model for model in models if model != current
        ]
        selected = ask(
            questionary.select(
                "选择模型",
                choices=[questionary.Choice(model, value=model) for model in ordered]
                + [questionary.Choice("手动输入模型 ID", value="")],
                style=_menu_style(),
                qmark="›",
                pointer="❯",
                instruction="↑↓ 选择 · Enter 确认",
            )
        )
        if selected is None:
            raise KeyboardInterrupt
        if selected:
            return selected
    else:
        _console().print("[dim]API 未返回模型列表，可手动输入已确认的模型 ID。[/dim]")
    value = ask(questionary.text("模型名称", default=current))
    if value is None or not value.strip():
        raise KeyboardInterrupt
    return value.strip()


def _choose_reasoning(values: tuple[str, ...], current: str | None) -> str | None:
    import questionary

    if not values:
        _console().print("[dim]该模型没有可选的思考档位。[/dim]")
        return None
    selected = ask(
        questionary.select(
            "思考强度",
            choices=[questionary.Choice(value or "默认", value=value) for value in values],
            default=current if current in values else None,
            style=_menu_style(),
            qmark="›",
            pointer="❯",
            instruction="↑↓ 选择 · Enter 确认",
        )
    )
    if selected is None:
        raise KeyboardInterrupt
    return selected or None


def _choose_context(options: list[int], current: int) -> int:
    import questionary

    default = min(options, key=lambda value: abs(value - current))
    options = [default] + [value for value in options if value != default]
    selected = ask(
        questionary.select(
            "上下文长度",
            choices=[questionary.Choice(format_tokens(value), value=value) for value in options],
            style=_menu_style(),
            qmark="›",
            pointer="❯",
            instruction="↑↓ 选择 · Enter 确认",
        )
    )
    if selected is None:
        raise KeyboardInterrupt
    return int(selected)


def run_setup(config_path: Path | None = None) -> dict:
    """syvrio setup 顶层入口：先选配置内容，再进入对应向导。"""
    import questionary

    choice = ask(
        questionary.select(
            "配置内容",
            choices=[
                questionary.Choice("模型与 LLM", value="llm"),
                questionary.Choice("搜索与网页抓取 API", value="search"),
            ],
            style=_menu_style(),
            qmark="›",
            pointer="❯",
            instruction="↑↓ 选择 · Enter 确认",
        )
    )
    if choice is None:
        raise KeyboardInterrupt
    with local_setup(config_path) as setup:
        if choice == "search":
            return run_search_setup(setup)
        return _run_llm_setup(setup)


def _manage_search_pauses(setup: SetupSession) -> None:
    import questionary

    while paused := setup.paused_search_providers():
        choices = []
        for name, pause in paused.items():
            label = SEARCH_PROVIDER_LABELS.get(name, name)
            detail = pause.reason
            if pause.resume_at is not None:
                local = datetime.fromtimestamp(pause.resume_at).astimezone()
                detail += f" · {local:%Y-%m-%d %H:%M} 自动恢复"
            choices.append(questionary.Choice(f"{label} · {detail}", value=name))
        choices.append(questionary.Choice("返回", value=""))
        name = _select("选择要解除暂停的搜索厂商", choices)
        if not name:
            return
        setup.resume_search_provider(name)
    _console().print("当前没有暂停的搜索厂商。")


def _select(message: str, choices: list):
    import questionary

    return ask(
        questionary.select(
            message,
            choices=choices,
            style=_menu_style(),
            qmark="›",
            pointer="❯",
            instruction="↑↓ 选择 · Enter 确认",
        )
    )


def _provider_state(setup: SetupSession, name: str) -> str:
    marks = []
    if name in setup.sources("search"):
        marks.append("Search")
    if name in setup.sources("fetch"):
        marks.append("Fetch")
    return f"（{'/'.join(marks)}已启用）" if marks else ""


def _prompt_api_key(prompt: str) -> str | None:
    import questionary

    value = ask(questionary.password(prompt))
    return value.strip() if value is not None else None


def _configure_search_provider(setup: SetupSession, messages: list[str]) -> None:
    import questionary

    choices = [
        questionary.Choice(f"{label}{_provider_state(setup, name)}", value=name)
        for name, label in SEARCH_PROVIDER_LABELS.items()
    ]
    name = _select("选择厂商", choices)
    if name is None:
        raise KeyboardInterrupt
    env_name = SEARCH_KEY_ENVS.get(name)
    if name == "searxng":
        _console().print(
            "[dim]自托管 SearXNG 无需 API Key；请确认实例已在 settings.yml 启用 JSON 输出。[/dim]"
        )

        current = setup.search_endpoint
        raw = ask(questionary.text(f"SearXNG 实例地址（当前 {current}；留空保持不变）"))
        if raw is not None and str(raw).strip():
            try:
                setup.set_search_endpoint(str(raw))
                messages.append(f"SearXNG 实例地址已设为 {setup.search_endpoint}。")
            except ValueError as exc:
                messages.append(f"SearXNG 地址未保存：{exc}")
    elif env_name is None:
        _console().print("[dim]open-websearch 免费且无需 Key，本地 daemon 即可用。[/dim]")
    else:
        current_key = setup.has_key(env_name)
        if name in ANONYMOUS_SEARCH_PROVIDERS:
            state = "已配置" if current_key else "未配置（匿名模式）"
            _console().print("[dim]不填写 Key 也可以启用并使用匿名搜索。[/dim]")
        else:
            state = "已配置" if current_key else "未配置"
        api_key = _prompt_api_key(
            f"{SEARCH_PROVIDER_LABELS[name]} API Key（当前{state}；留空保持不变）"
        )
        if api_key:
            if setup.set_key(env_name, api_key):
                messages.append(f"{name} 的 API Key 已保存到系统凭据。")
            else:
                messages.append(f"{name} 的 API Key 保存失败，请检查系统凭据库是否可用后重试。")
    while True:
        search_on = name in setup.sources("search")
        fetch_on = name in setup.sources("fetch")
        action = _select(
            f"配置 {SEARCH_PROVIDER_LABELS[name]}",
            [
                questionary.Choice(f"{'禁用' if search_on else '启用'} Search", value="search"),
                *(
                    [questionary.Choice(f"{'禁用' if fetch_on else '启用'} Fetch", value="fetch")]
                    if setup.can_toggle_fetch(name)
                    else []
                ),
                *(
                    [questionary.Choice("清除 API Key（恢复匿名）", value="clear_key")]
                    if env_name and name in ANONYMOUS_SEARCH_PROVIDERS and setup.has_key(env_name)
                    else []
                ),
                questionary.Choice("返回上级", value="back"),
            ],
        )
        if action is None or action == "back":
            return
        if action == "clear_key":
            if setup.remove_key(env_name):
                messages.append(f"{name} 的系统凭据已清除。")
            else:
                messages.append(f"{name} 的系统凭据清除失败，请检查系统凭据存储。")
            continue
        if action in ("search", "fetch"):
            if message := setup.toggle(name, action):
                messages.append(message)


def _adjust_order(setup: SetupSession, capability: str, label: str) -> None:
    import questionary

    if not setup.sources(capability):
        _console().print(f"[dim]{label} 当前没有启用的 provider。[/dim]")
        return
    while True:
        names = setup.sources(capability)
        current = " → ".join(SEARCH_PROVIDER_LABELS.get(name, name) for name in names)
        choices = [
            questionary.Choice(f"{SEARCH_PROVIDER_LABELS.get(name, name)}", value=name)
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
        setup.move(capability, name, direction)


def run_search_setup(setup: SetupSession) -> dict:
    """搜索与网页抓取向导：Key、启停与优先级；TOML 只写非敏感配置。"""
    import questionary

    messages: list[str] = []
    while True:
        action = _select(
            "搜索与网页抓取配置",
            [
                questionary.Choice("选择厂商：配置 Key / 启用停用", value="providers"),
                questionary.Choice("查看与解除搜索厂商暂停", value="pauses"),
                questionary.Choice("调整 Search 优先级", value="search_order"),
                questionary.Choice("调整 Fetch 优先级", value="fetch_order"),
                questionary.Choice("保存并返回", value="done"),
            ],
        )
        if action is None or action == "done":
            break
        if action == "providers":
            _configure_search_provider(setup, messages)
        elif action == "pauses":
            _manage_search_pauses(setup)
        elif action == "search_order":
            _adjust_order(setup, "search", "Search")
        elif action == "fetch_order":
            _adjust_order(setup, "fetch", "Fetch")
    setup.save()
    return {
        "mode": "search",
        "config_path": str(setup.path),
        "search_providers": list(setup.sources("search")),
        "fetch_providers": list(setup.sources("fetch")),
        "messages": messages,
    }


def _run_llm_setup(setup: SetupSession) -> dict:
    import questionary

    current = setup.model_settings()
    choice = ask(
        questionary.select(
            "选择模型供应商",
            choices=[questionary.Choice(title=preset.label, value=preset) for preset in PRESETS],
            style=_menu_style(),
            qmark="›",
            pointer="❯",
            instruction="↑↓ 选择 · Enter 确认",
        )
    )
    if choice is None:
        raise KeyboardInterrupt
    preset: ProviderPreset = choice

    provider = preset.key
    base_url = ask(questionary.text("Base URL", default=setup.default_url(preset)))
    env_name = (
        ask(questionary.text("API Key 凭据条目名", default=current["api_key_env"]))
        if provider == "custom"
        else preset.api_key_env
    )
    if base_url is None or env_name is None:
        raise KeyboardInterrupt
    while True:
        try:
            base_url = normalize_provider_url(base_url)
            break
        except ValueError as exc:
            _console().print(str(exc))
            base_url = ask(questionary.text("Base URL", default=base_url))
            if base_url is None:
                raise KeyboardInterrupt from None
    env_name = env_name.strip()
    api_key = _prompt_api_key("API Key（留空使用凭据库中的密钥）")
    if api_key is None:
        raise KeyboardInterrupt
    current_model = current["model"] if current["provider"] == provider else ""
    model = _choose_model(setup.model_list(provider, base_url, env_name, api_key), current_model)
    max_input, levels, reasoning, options = setup.model_limits(
        provider, base_url, env_name, model, api_key
    )
    if max_input:
        _console().print(f"模型上下文上限：{format_tokens(max_input)} tokens")
    selected = _choose_reasoning(levels, reasoning)
    context_window = _choose_context(options, current["context_window"])
    _console().print(
        f"使用 {preset.label}：{model} · 上下文 {context_window} · 思考强度 {selected or '默认'}"
    )

    key_saved = False
    keyring_error = ""
    if api_key:
        key_saved = setup.set_key(env_name, api_key)
        if not key_saved:
            keyring_error = "API Key 保存失败，请检查系统凭据库是否可用后重试。"

    setup.save_model(provider, base_url, env_name, model, selected, context_window)

    check = False
    detail = "已跳过连接测试"
    if key_saved or (not api_key and setup.has_key(env_name)):
        if ask(questionary.confirm("现在测试连接？", default=True)):
            check, detail = setup.test_model()

    initialized = False
    if setup.needs_initialization():
        if ask(questionary.confirm(f"初始化记忆库 {current['vault']}？", default=True)):
            setup.initialize()
            initialized = True

    return {
        "config_path": str(setup.path),
        **setup.model_settings(),
        "thinking_selection": selected,
        "key_saved": key_saved,
        "keyring_error": keyring_error,
        "checked": check,
        "check_detail": detail,
        "initialized": initialized,
    }
