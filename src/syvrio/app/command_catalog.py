"""斜杠命令清单的 TUI 导入入口。

权威定义已下沉到 ``application.commands``，使 Web 与 TUI 共用同一份命令
名称、用法和能力标记；此处只保留转发，避免改动既有 TUI 调用点。
"""
from __future__ import annotations

from ..application.commands import SLASH_COMMANDS, SLASH_SUBCOMMANDS, slash_matches

__all__ = ["SLASH_COMMANDS", "SLASH_SUBCOMMANDS", "slash_matches"]
