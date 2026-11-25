"""Immutable external objects. Unreferenced objects are removed only after commit."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..storage.database import transaction
from ..storage.documents import DocumentError
from ..storage.files import sync_directory


def original_path(root: Path, digest: str):
    if not re.fullmatch("[a-f0-9]{64}", digest):
        raise DocumentError("Invalid attachment checksum.")
    path = root / "objects" / digest[:2] / digest
    if path.resolve() != path:
        raise DocumentError("Redirected attachment object.")
    return path


def reserve(paths, token, files):
    with transaction(paths) as db:
        db.executemany(
            "INSERT OR IGNORE INTO attachment_reservations VALUES(?,?)",
            [(token, f.sha256) for f in files],
        )
        db.executemany(
            "INSERT OR IGNORE INTO file_cleanup VALUES('attachment',?)",
            [(f.sha256,) for f in files],
        )


def release(paths, token):
    with transaction(paths) as db:
        db.execute("DELETE FROM attachment_reservations WHERE token=?", (token,))


def publish(paths, item, source):
    # The durable reservation protects the object until the message and reference commit.
    dest = original_path(paths.attachments_dir, item.sha256)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, dest)
    except FileExistsError:
        item.verify(paths.attachments_dir, paths.attachments_dir)
    for directory in (dest.parent, dest.parent.parent, paths.attachments_dir, paths.vault_dir):
        sync_directory(directory)
    with transaction(paths) as db:
        db.execute("INSERT OR IGNORE INTO attachment_objects VALUES(?,?)", (item.sha256, item.size))


def cleanup(paths):
    with transaction(paths, write=False) as db:
        targets = [
            r[0] for r in db.execute("SELECT object_id FROM file_cleanup WHERE kind='attachment'")
        ]
    for digest in targets:
        with transaction(paths) as db:
            if db.execute(
                "SELECT 1 FROM attachment_reservations WHERE digest=?", (digest,)
            ).fetchone():
                continue
            if not db.execute(
                "SELECT 1 FROM attachments WHERE sha256=? LIMIT 1", (digest,)
            ).fetchone():
                try:
                    original_path(paths.attachments_dir, digest).unlink(missing_ok=True)
                except OSError:
                    continue
                db.execute("DELETE FROM attachment_objects WHERE sha256=?", (digest,))
            db.execute(
                "DELETE FROM file_cleanup WHERE kind='attachment' AND object_id=?", (digest,)
            )
