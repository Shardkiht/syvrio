"""Inspect synthetic rows at a transaction boundary in storage regression tests."""

from __future__ import annotations

from syvrio.infra.storage.database import transaction


def rows(paths, table):
    with transaction(paths, write=False) as db:
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return [dict(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]


def rejected(queue):
    with transaction(queue.paths, write=False) as db:
        rows = db.execute(
            "SELECT * FROM candidates WHERE state='rejected' ORDER BY rowid"
        ).fetchall()
        return queue._items(db, rows)
