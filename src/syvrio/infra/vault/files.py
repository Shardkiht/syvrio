"""Vault 初始化及根目录文件。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sync import GitSyncService


# 新 Vault 的人设文件和聊天缺省人设使用同一份文本。
DEFAULT_PERSONA = "你是用户的个人记忆助手，运行在本地终端，拥有跨会话的长期记忆。"

SOUL_FILENAME = "SOUL.md"


def load_persona(vault: Path) -> str:
    """读取 Vault 中的人设原文，缺失或为空时返回空串。

    用户可能用记事本以 ANSI/GBK 保存，UTF-8 失败时按本机编码兜底，避免炸掉整轮对话。
    """
    path = vault / SOUL_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError:
        # 记事本 ANSI（中文系统即 GBK）保存的场景；gbk 编码全平台内置，
        # 不用 Windows 专属的 mbcs——英文系统的 cp1252 解不了 GBK。
        try:
            text = path.read_text(encoding="gbk")
        except (UnicodeDecodeError, OSError):
            return ""
    return text.strip()


def initialize_vault(git: GitSyncService, initialize_memory: Callable[[], None]) -> dict:
    """验证仓库归属后，在初始化提交的同一把锁内创建 Vault 文件。"""
    paths = git.paths

    def prepare_files() -> None:
        initialize_memory()
        soul = paths.vault_dir / SOUL_FILENAME
        if not soul.exists():
            soul.write_text(DEFAULT_PERSONA + "\n", encoding="utf-8")

    return git.initialize(prepare_files)


def open_vault(git: GitSyncService, initialize_memory: Callable[[], None]) -> None:
    if (git.repo.path / ".git").exists():
        git.ensure_ignore_rules()
    else:
        initialize_vault(git, initialize_memory)
