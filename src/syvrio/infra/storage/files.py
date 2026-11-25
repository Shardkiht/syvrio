from __future__ import annotations

import ntpath
import os
import re
import stat
import tempfile
from pathlib import Path, PureWindowsPath


def _reserved(name: str) -> bool:
    if hasattr(ntpath, "isreserved"):
        return ntpath.isreserved(name)
    return PureWindowsPath(name).is_reserved()


def safe_component(name: str) -> bool:
    return bool(
        name
        and not name.startswith(".")
        and not name.endswith((".", " "))
        and not _reserved(name)
        and re.search(r'[<>:"/\\|?*\x00-\x1f]', name) is None
    )


def sync_directory(path: Path) -> None:
    """在支持对目录执行 fsync 的系统上持久化目录项。"""
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(path: Path, payload: bytes) -> None:
    """在目标目录发布完整字节内容并保留权限。"""
    directory = path.parent
    missing = []
    while directory != directory.parent and not directory.exists():
        missing.append(directory)
        directory = directory.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        sync_directory(directory.parent)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            if path.exists():
                os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
