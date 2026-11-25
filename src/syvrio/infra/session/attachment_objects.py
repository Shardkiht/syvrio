"""按内容保存附件原件；正向引用用于删除会话，反向引用保护其他消息共享的原件。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..storage.documents import DocumentError
from ..storage.locking import attachment_store_lock

_DIGEST = re.compile("[a-f0-9]{64}")
_LEGACY = re.compile(r"[a-f0-9]{32}\.[a-z0-9]+")
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def _safe(path: Path) -> Path:
    if path.resolve() != path:
        raise DocumentError("Redirected attachment path.")
    return path


def original_path(root: Path, digest: str) -> Path:
    if not _DIGEST.fullmatch(digest):
        raise DocumentError("Invalid attachment checksum.")
    return _safe(root / ".objects" / digest[:2] / digest / "original")


def _owner(session_id: str, message_id: str) -> str:
    if not all(_OWNER.fullmatch(key) for key in (session_id, message_id)):
        raise DocumentError("Invalid attachment owner.")
    return f"{session_id}.{message_id}.ref"


def publish(root: Path, source: Path, folder: Path, digest: str) -> None:
    if not _DIGEST.fullmatch(digest):
        raise DocumentError("Invalid attachment checksum.")
    owner = _owner(folder.parent.name, folder.name)
    with attachment_store_lock(root, digest):
        objects = _safe(root) / ".objects"
        objects.mkdir(exist_ok=True)
        target = original_path(root, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先登记归属；中途失败仍可按消息释放引用，原件通过同卷重命名发布。
        _safe(folder / (digest + ".ref")).touch()
        _safe(target.parent / owner).touch()
        if target.exists():
            with target.open("rb") as live:
                if hashlib.file_digest(live, "sha256").hexdigest() != digest:
                    raise DocumentError("Stored attachment checksum mismatch.")
            source.unlink()
        else:
            source.replace(target)


def release_message(root: Path, folder: Path, session_id: str) -> None:
    owner = _owner(session_id, folder.name)
    for ref in _safe(folder).glob("*.ref"):
        target = original_path(root, ref.stem)
        with attachment_store_lock(root, ref.stem):
            _safe(target.parent / owner).unlink(missing_ok=True)
            if target.parent.is_dir() and not any(target.parent.glob("*.ref")):
                target.unlink(missing_ok=True)
                target.parent.rmdir()
            _safe(ref).unlink()


def upgrade_originals(root: Path) -> None:
    """常驻后端启动时收拢此前按消息保存的原件；空标记和新格式目录无需读取正文。"""
    if not root.is_dir():
        return
    for session in _safe(root).iterdir():
        if not _OWNER.fullmatch(session.name) or not _safe(session).is_dir():
            continue
        for message in session.iterdir():
            if not _OWNER.fullmatch(message.name) or not _safe(message).is_dir():
                continue
            for source in message.iterdir():
                if _LEGACY.fullmatch(source.name):
                    with _safe(source).open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    publish(root, source, message, digest)
