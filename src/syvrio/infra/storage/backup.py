"""持有源 Vault 租约后，生成可独立还原的数据备份。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from ..session.attachment_objects import original_path
from .database import encode, require_current_schema


def _digest(path):
    if not path.is_file() or path.is_symlink() or path.resolve() != path:
        raise ValueError("Missing or redirected backup member.")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _snapshot(path, target, checksums):
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("Redirected database.")
    uri = path.as_uri() + "?mode=ro"
    if checksums is not None:
        if _digest(path) != checksums.get(path.name):
            raise ValueError(f"Database checksum mismatch: {path.name}")
        # 归档成员已经过校验并完成检查点，恢复只读取清单覆盖的数据库内容。
        uri += "&immutable=1"
    elif any(
        path.with_name(path.name + suffix).is_symlink() for suffix in ("-wal", "-shm", "-journal")
    ):
        raise ValueError("Redirected database journal.")
    with closing(sqlite3.connect(uri, uri=True)) as live, closing(sqlite3.connect(target)) as db:
        live.backup(db)
        require_current_schema(db)
        if (
            db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
            or db.execute("PRAGMA foreign_key_check").fetchone()
        ):
            raise ValueError("Database integrity check failed.")


def assemble(source: Path, destination: Path, *, restore: bool = False):
    source = source.resolve(strict=True)
    destination = destination.absolute()
    if (
        destination.resolve() != destination
        or destination.exists()
        or destination.is_relative_to(source)
    ):
        raise ValueError("Choose a new directory outside the source.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    name = "syvrio.sqlite3"
    checksums = None
    manifest = source / "manifest.json"
    if restore:
        _digest(manifest)
        ledger = json.loads(manifest.read_text(encoding="utf-8"))
        if ledger.get("version") != 1 or not isinstance(ledger.get("files"), dict):
            raise ValueError("Invalid backup manifest.")
        checksums = ledger["files"]

    def copy(rel):
        path = source / rel
        target = out / rel
        if path.resolve() != path:
            raise ValueError("Redirected backup member.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        return _digest(target)

    with TemporaryDirectory(dir=destination.parent, prefix=".syvrio-copy-") as temp:
        out = Path(temp) / "vault"
        out.mkdir(mode=0o700)
        _snapshot(source / name, out / name, checksums)
        with closing(sqlite3.connect(out / name)) as db:
            hashes = {}
            for digest, size in db.execute(
                "SELECT DISTINCT sha256,size FROM attachments ORDER BY sha256"
            ):
                original = original_path(source / "attachments", digest)
                rel = original.relative_to(source).as_posix()
                hashes[rel] = copy(rel)
                if hashes[rel] != digest or (out / rel).stat().st_size != size:
                    raise ValueError("Attachment verification failed.")
            if (source / "SOUL.md").exists():
                hashes["SOUL.md"] = copy("SOUL.md")
            if restore:
                if hashes != {k: v for k, v in checksums.items() if k != name}:
                    raise ValueError("Backup file manifest differs from database references.")
                with db:
                    db.execute("DELETE FROM git_sync")
                    db.execute("DELETE FROM file_cleanup")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if not restore:
            hashes[name] = _digest(out / name)
            (out / "manifest.json").write_text(
                encode({"version": 1, "files": hashes}), encoding="utf-8"
            )
        out.rename(destination)
    return destination
