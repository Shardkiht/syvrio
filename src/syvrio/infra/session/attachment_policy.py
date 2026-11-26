"""聊天附件选择与接收共用的约束。"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

from pydantic import BaseModel, ConfigDict

from ..storage.documents import MAX_DOCUMENT_BYTES, SUPPORTED_SUFFIXES, DocumentError

IMAGE_TYPES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp", ".gif": "gif"}
_MIB = 1024 * 1024


class AttachmentPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    extensions: tuple[str, ...]
    max_files: int
    max_file_bytes: int
    max_batch_bytes: int
    max_name_length: int

    def validate_name(self, name: str) -> None:
        if (
            not name
            or PureWindowsPath(name).name != name
            or len(name) > self.max_name_length
            or any(ord(char) < 32 for char in name)
            or Path(name).suffix.lower() not in self.extensions
        ):
            raise DocumentError("Unsupported attachment name or format.")

    def validate_size(self, size: int) -> None:
        if not size:
            raise DocumentError("Empty attachments cannot be read.")
        if size > self.max_file_bytes:
            raise DocumentError(f"Attach files of at most {self.max_file_bytes / _MIB:g} MiB each.")

    def validate_batch(self, count: int, total: int) -> None:
        if count > self.max_files or total > self.max_batch_bytes:
            raise DocumentError(
                f"Attach at most {self.max_files} files and {self.max_batch_bytes / _MIB:g} MiB per message."
            )


ATTACHMENT_POLICY = AttachmentPolicy(
    extensions=tuple(sorted(SUPPORTED_SUFFIXES | IMAGE_TYPES.keys())),
    max_files=8,
    max_file_bytes=MAX_DOCUMENT_BYTES,
    max_batch_bytes=40 * _MIB,
    max_name_length=255,
)
