"""先在本地暂存符合限制的上传文件，再将整批文件绑定到同一条会话消息。"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import BinaryIO
from uuid import uuid4

from PIL import Image

from ..storage.documents import SUPPORTED_SUFFIXES, DocumentError

MAX_FILES = 8
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_BATCH_BYTES = 40 * 1024 * 1024
IMAGE_TYPES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp", ".gif": "gif"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


@dataclass(frozen=True)
class Attachment:
    id: str
    name: str
    size: int
    media_type: str
    sha256: str

    @property
    def filename(self) -> str:
        return self.id + Path(self.name).suffix.lower()

    def verify(
        self, folder: Path, copy_to: Callable[[bytes], object] | None = None
    ) -> os.stat_result:
        if (
            not re.fullmatch(r"[a-f0-9]{32}", self.id)
            or PureWindowsPath(self.name).name != self.name
        ):
            raise DocumentError("Invalid attachment metadata.")
        path = folder / self.filename
        unavailable = "Attachment is missing or changed. Restore the matching attachment archive."
        if path.resolve() != path or not path.is_file():
            raise DocumentError(unavailable)
        with path.open("rb") as source:
            stat = os.fstat(source.fileno())
            if not 0 < stat.st_size == self.size <= MAX_FILE_BYTES:
                raise DocumentError(unavailable)
            if copy_to is None:
                digest = hashlib.file_digest(source, "sha256")
            else:
                # 快照与校验取自同一批字节，并限制原件同时被追加时的复制量。
                digest = hashlib.sha256()
                copied = 0
                while chunk := source.read(256 * 1024):
                    copied += len(chunk)
                    if copied > self.size:
                        raise DocumentError(unavailable)
                    digest.update(chunk)
                    copy_to(chunk)
                if copied != self.size:
                    raise DocumentError(unavailable)
            if digest.hexdigest() != self.sha256:
                raise DocumentError(
                    "Attachment checksum mismatch. Restore the matching attachment archive."
                )
            return stat


def _directory(root: Path, *parts: str) -> Path:
    if not all(_ID.fullmatch(part) for part in parts):
        raise DocumentError("Invalid attachment identifier.")
    path = root
    for part in ("", *parts):
        path = path / part
        if path.resolve() != path:
            raise DocumentError("Attachment directories must stay inside the Vault.")
    return path


@dataclass
class AttachmentBatch:
    root: Path
    folder: Path
    files: tuple[Attachment, ...]

    @classmethod
    def stage(cls, root: Path, streams: Iterable[tuple[str, BinaryIO]]) -> AttachmentBatch:
        # 并发首次上传时，先创建共享目录，再校验其实际路径，避免在另一个请求创建目录时解析它。
        _directory(root.parent)
        root.mkdir(parents=True, exist_ok=True)
        incoming = _directory(root) / ".incoming"
        incoming.mkdir(exist_ok=True)
        folder = _directory(incoming, uuid4().hex)
        folder.mkdir()
        paths: list[Path] = []
        files: list[Attachment] = []
        total = 0
        try:
            for name, stream in streams:
                suffix = Path(name).suffix.lower()
                if (
                    not name
                    or PureWindowsPath(name).name != name
                    or len(name) > 255
                    or any(ord(c) < 32 for c in name)
                    or suffix not in SUPPORTED_SUFFIXES
                    and suffix not in IMAGE_TYPES
                ):
                    raise DocumentError("Unsupported attachment name or format.")
                if len(files) >= MAX_FILES:
                    raise DocumentError(f"Attach at most {MAX_FILES} files per message.")
                identifier = uuid4().hex
                path = folder / (identifier + suffix)
                size = 0
                digest = hashlib.sha256()
                with path.open("xb") as output:
                    paths.append(path)
                    while chunk := stream.read(256 * 1024):
                        size += len(chunk)
                        total += len(chunk)
                        if size > MAX_FILE_BYTES or total > MAX_BATCH_BYTES:
                            raise DocumentError(
                                "Attachment limit is 20 MiB per file and 40 MiB per message."
                            )
                        output.write(chunk)
                        digest.update(chunk)
                if not size:
                    raise DocumentError("Empty attachments cannot be read.")
                media = "application/octet-stream"
                if suffix in IMAGE_TYPES:
                    try:
                        with Image.open(path) as img:
                            if (
                                img.format.lower() != IMAGE_TYPES[suffix]
                                or img.width * img.height > 40_000_000
                            ):
                                raise DocumentError("Image format or dimensions are unsupported.")
                            img.verify()
                    except (ValueError, OSError, Image.DecompressionBombError) as exc:
                        raise DocumentError("Invalid or oversized image attachment.") from exc
                    media = "image/" + IMAGE_TYPES[suffix]
                files.append(Attachment(identifier, name, size, media, digest.hexdigest()))
            if not files:
                raise DocumentError("Select at least one attachment.")
        except BaseException:
            for path in paths:
                path.unlink(missing_ok=True)
            folder.rmdir()
            raise
        return cls(root, folder, tuple(files))

    def bind(self, session_id: str, message_id: str) -> None:
        dest = _directory(self.root, session_id, message_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(self.folder, dest)
        self.folder = dest

    def discard(self) -> None:
        if self.folder.resolve() != self.folder or not self.folder.is_relative_to(self.root):
            raise DocumentError("Invalid attachment directory.")
        for item in self.files:
            (self.folder / item.filename).unlink(missing_ok=True)
        self.folder.rmdir()

    def documents(self) -> dict[str, Path]:
        # 用位置区分显示名称相同的两个上传文件。
        return {
            f"{index}. {item.name}": self.folder / item.filename
            for index, item in enumerate(self.files, 1)
            if not item.media_type.startswith("image/")
        }

    @staticmethod
    def copy_original(
        root: Path,
        session_id: str,
        message_id: str,
        record: dict,
        destination: Callable[[bytes], object],
    ) -> tuple[Attachment, os.stat_result]:
        folder = _directory(root, session_id, message_id)
        item = Attachment(**record)
        return item, item.verify(folder, destination)

    @classmethod
    def restore(
        cls, root: Path, session_id: str, message_id: str, records: list[dict]
    ) -> AttachmentBatch:
        folder = _directory(root, session_id, message_id)
        files = tuple(Attachment(**record) for record in records)
        for item in files:
            item.verify(folder)
        return cls(root, folder, files)

    def model_content(self, text: str) -> str | list[dict]:
        docs = self.documents()
        if docs:
            text += "\n\nAttached documents (use read_file to inspect):\n" + "\n".join(docs)
        parts = [{"type": "text", "text": text}]
        for item in self.files:
            if item.media_type.startswith("image/"):
                data = base64.b64encode((self.folder / item.filename).read_bytes()).decode("ascii")
                parts.extend(
                    [
                        {"type": "text", "text": item.name},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{item.media_type};base64,{data}"},
                        },
                    ]
                )
        return parts if len(parts) > 1 else text
