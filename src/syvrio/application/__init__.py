"""与前端无关的应用用例，按需加载公开入口。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

# 读取轻量命令协议时，无需装配模型运行时和后台任务。
_EXPORTS = {
    "ApplicationService": ".service",
    "CommandResult": ".commands",
    "CommandUseCases": ".commands",
    "ConversationUseCases": ".conversation",
    "GitUseCases": ".git",
    "ImportChunkProgress": ".contracts",
    "ImportResult": ".contracts",
    "InferenceReview": ".contracts",
    "MemoryUseCases": ".memory",
    "ModelOption": ".contracts",
    "PendingSnapshot": ".contracts",
    "ProfileSnapshot": ".contracts",
    "ProviderOption": ".contracts",
    "QueueSnapshot": ".contracts",
    "QueuedItem": ".contracts",
    "QueuedLine": ".conversation",
    "ReflectionUseCases": ".reflection",
    "SAFE_COMMANDS": ".conversation",
    "SLASH_COMMANDS": ".command_catalog",
    "SLASH_SUBCOMMANDS": ".command_catalog",
    "SessionSnapshot": ".contracts",
    "SettingsResult": ".contracts",
    "SettingsUseCases": ".settings",
    "Submission": ".contracts",
    "SwitchResult": ".contracts",
    "TurnCallbacks": ".contracts",
    "TurnResult": ".contracts",
    "UndoOutcome": ".contracts",
    "WebCapability": ".commands",
    "WebCommandInfo": ".commands",
    "web_command_catalog": ".commands",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
