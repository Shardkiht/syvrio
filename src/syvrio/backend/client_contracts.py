"""本地客户端和 MCP 共用的序列化模型，领域校验只有一份定义。"""

from __future__ import annotations

from pydantic import Base64Bytes, BaseModel, ConfigDict

from ..application.command_catalog import (
    SLASH_COMMANDS,
    SLASH_SUBCOMMANDS,
    parse_command,
    slash_matches,
)
from ..infra.config import WebShareConfig as WebShareConfig
from ..infra.storage.documents import SUPPORTED_SUFFIXES
from ..infra.storage.paths import SESSION_ID_RE
from ..infra.vault.sync import SyncResult, SyncStage
from ..memory.models import Entry, Inference, MemoryContext, MemoryDraft, PendingItem, RecallEntry
from .updates.contracts import ProgressCallback, UpdateProgress


class UploadedImportDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    content: Base64Bytes = b""
    read_error: str = ""


__all__ = [
    "SLASH_COMMANDS",
    "SLASH_SUBCOMMANDS",
    "parse_command",
    "slash_matches",
    "SUPPORTED_SUFFIXES",
    "SESSION_ID_RE",
    "ProgressCallback",
    "UpdateProgress",
    "Entry",
    "Inference",
    "MemoryContext",
    "MemoryDraft",
    "PendingItem",
    "RecallEntry",
    "UploadedImportDocument",
    "SyncResult",
    "SyncStage",
]
