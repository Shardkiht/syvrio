"""Vault 初始化及根目录文件。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sync import GitSyncService

VAULT_README = """# Syvrio vault

这个目录由 [Syvrio](https://github.com/Shardkiht/Syvrio) 管理。

- `memories/<category>/<topic>.md`：Markdown v2，是唯一事实来源。
- `sessions/YYYY/MM/`：会话 JSONL，保留原始对话与工具轨迹。
- `archived/sessions/YYYY/MM/<id>.jsonl`：归档会话，仅供人工查看、还原或删除。
- `attachments/`：聊天上传的原件，由 Vault 的 .gitignore 排除；迁移时随整个 Vault 复制。
- `memories/pending.md` 保存待确认项，拒绝的候选移至 `archived/pending/rejected.md`。
- `inferences/` 保存推断，`records/` 保存正式记忆的变化与审计。
- Runtime 与 Vault 分离；派生索引、缓存和锁不会进入 Vault。
- `SOUL.md`（可选）：自定义人设，替代系统提示词里的默认人设段。
- Git 跟踪会话和记忆的变更。
"""

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
        paths.sessions_dir.mkdir(parents=True, exist_ok=True)
        readme = paths.vault_dir / "README.md"
        if not readme.exists():
            readme.write_text(VAULT_README, encoding="utf-8")
        soul = paths.vault_dir / SOUL_FILENAME
        if not soul.exists():
            soul.write_text(DEFAULT_PERSONA + "\n", encoding="utf-8")

    return git.initialize(prepare_files)


def open_vault(git: GitSyncService, initialize_memory: Callable[[], None]) -> None:
    if (git.paths.vault_dir / ".git").exists():
        git.ensure_ignore_rules()
    else:
        initialize_vault(git, initialize_memory)
