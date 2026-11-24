from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path, PureWindowsPath

type FileVersion = tuple[int, int, int, int, int]


def file_version(stat: os.stat_result) -> FileVersion:
    # Windows 上通过路径和句柄读取的 ctime 含义可能不同，创建时间在两者间一致。
    stamp = stat.st_birthtime_ns if os.name == "nt" else stat.st_ctime_ns
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stamp


def safe_component(name: str) -> bool:
    return bool(
        name
        and not name.startswith(".")
        and not name.endswith((".", " "))
        and not PureWindowsPath(name).is_reserved()
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
