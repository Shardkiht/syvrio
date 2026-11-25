"""Standalone data archive. The caller owns the source lease during backup."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from ..session.attachment_objects import original_path
from .database import APPLICATION_ID, SCHEMA_VERSION, encode


def _digest(path):
    if not path.is_file() or path.is_symlink() or path.resolve() != path:
        raise ValueError("Missing or redirected backup member.")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _verify(db):
    if (
        db.execute("PRAGMA application_id").fetchone()[0],
        db.execute("PRAGMA user_version").fetchone()[0],
    ) != (APPLICATION_ID, SCHEMA_VERSION):
        raise ValueError("Unsupported database.")
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
    ledger = {}
    manifest = source / "manifest.json"
    if restore:
        _digest(manifest)
        ledger = json.loads(manifest.read_text(encoding="utf-8"))
        if ledger.get("version") != 1 or not isinstance(ledger.get("files"), dict):
            raise ValueError("Invalid backup manifest.")

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
        database = source / name
        if database.resolve() != database:
            raise ValueError("Redirected database.")
        with (
            closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as live,
            closing(sqlite3.connect(out / name)) as db,
        ):
            live.backup(db)
            _verify(db)
            if restore and _digest(database) != ledger["files"].get(name):
                raise ValueError("Database checksum mismatch.")
            hashes = {}
            for digest, size in db.execute(
                "SELECT DISTINCT o.sha256,o.size FROM attachment_objects o JOIN attachments a USING(sha256) ORDER BY o.sha256"
            ):
                original = original_path(source / "attachments", digest)
                rel = original.relative_to(source).as_posix()
                hashes[rel] = copy(rel)
                if hashes[rel] != digest or (out / rel).stat().st_size != size:
                    raise ValueError("Attachment verification failed.")
            if (source / "SOUL.md").exists():
                hashes["SOUL.md"] = copy("SOUL.md")
            if restore:
                if hashes != {k: v for k, v in ledger["files"].items() if k != name}:
                    raise ValueError("Backup file manifest differs from database references.")
                with db:
                    for table in (
                        "vectors",
                        "search_cache",
                        "git_sync",
                        "file_cleanup",
                        "attachment_reservations",
                    ):
                        db.execute(f"DELETE FROM {table}")
                    db.execute(
                        "DELETE FROM attachment_objects WHERE sha256 NOT IN (SELECT sha256 FROM attachments)"
                    )
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if not restore:
            hashes[name] = _digest(out / name)
            (out / "manifest.json").write_text(
                encode({"version": 1, "files": hashes}), encoding="utf-8"
            )
        out.rename(destination)
    return destination
