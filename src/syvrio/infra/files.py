from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path, PureWindowsPath


def safe_component(name: str) -> bool:
    return bool(
        name
        and not name.startswith(".")
        and not name.endswith((".", " "))
        and not PureWindowsPath(name).is_reserved()
        and re.search(r'[<>:"/\\|?*\x00-\x1f]', name) is None
    )


def sync_directory(path: Path) -> None:
    """Persist directory entries on systems supporting directory fsync."""
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(path: Path, payload: bytes) -> None:
    """Publish complete bytes in the destination directory, preserving permissions."""
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
