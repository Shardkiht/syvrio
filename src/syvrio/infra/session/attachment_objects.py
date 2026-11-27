"""管理按内容共享的附件原件及提交后的清理。"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

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


_sending: Counter[tuple[Path, str]] = Counter()
_sending_lock = Lock()


@contextmanager
def protect_upload(paths, files):
    digests = {item.sha256 for item in files}
    keys = [(paths.attachments_dir, digest) for digest in digests]
    with _sending_lock:
        _sending.update(keys)
    try:
        # 必须先提交清理标记再发布原件。清理同样取得数据库写事务，
        # 即使本次登记紧随它的占用检查，也会等它结束后才发布文件。
        with transaction(paths) as db:
            db.executemany(
                "INSERT OR IGNORE INTO file_cleanup VALUES('attachment',?)",
                [(digest,) for digest in digests],
            )
        yield
    finally:
        with _sending_lock:
            for key in keys:
                _sending[key] -= 1
                if not _sending[key]:
                    del _sending[key]
        try:
            cleanup(paths, digests)
        except (OSError, sqlite3.Error):
            logging.getLogger(__name__).warning("附件清理延后重试", exc_info=True)


def publish(paths, item, source):
    dest = original_path(paths.attachments_dir, item.sha256)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, dest)
    except FileExistsError:
        item.verify(paths.attachments_dir, paths.attachments_dir)
    for directory in (dest.parent, dest.parent.parent, paths.attachments_dir, paths.vault_dir):
        sync_directory(directory)


def cleanup(paths, targets=None):
    if targets is None:
        with transaction(paths, write=False) as db:
            targets = [
                r[0]
                for r in db.execute("SELECT object_id FROM file_cleanup WHERE kind='attachment'")
            ]
    for digest in targets:
        with transaction(paths) as db:
            with _sending_lock:
                if _sending[paths.attachments_dir, digest]:
                    continue
            if not db.execute(
                "SELECT 1 FROM attachments WHERE sha256=? LIMIT 1", (digest,)
            ).fetchone():
                try:
                    path = original_path(paths.attachments_dir, digest)
                    path.unlink(missing_ok=True)
                    if path.parent.exists():
                        sync_directory(path.parent)
                except OSError:
                    continue
            db.execute(
                "DELETE FROM file_cleanup WHERE kind='attachment' AND object_id=?", (digest,)
            )
