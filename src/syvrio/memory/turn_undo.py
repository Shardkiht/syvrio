"""记录工具影响的实体前像，与该次业务变更一起提交。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar

from ..infra.errors import SyvrioError
from ..infra.storage.database import encode, transaction


class TurnUndoError(SyvrioError, ValueError):
    pass


_active: ContextVar[TurnJournal | None] = ContextVar("turn_journal", default=None)
_KEYS = {
    "topics": "name",
    "memories": "id",
    "inferences": "id",
    "candidates": "id",
    "memory_changes": "seq",
}
_LINKS = {
    "inferences": ("inference_sources", "inference_id", "memory_id"),
    "candidates": ("candidate_related", "candidate_id", "memory_id"),
}


def image(db, entity, key):
    row = db.execute(f"SELECT * FROM {entity} WHERE {_KEYS[entity]}=?", (key,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    if entity in _LINKS:
        table, fk, _ = _LINKS[entity]
        result["_links"] = [
            dict(r)
            for r in db.execute(f"SELECT * FROM {table} WHERE {fk}=? ORDER BY rowid", (key,))
        ]
    return result


def references(db, key):
    return {
        r[0]
        for r in db.execute(
            """SELECT 'memory:'||id FROM memories WHERE supersedes=?
        UNION SELECT 'inference:'||inference_id FROM inference_sources WHERE memory_id=?
        UNION SELECT 'pending:'||candidate_id FROM candidate_related JOIN candidates c ON c.id=candidate_id
        WHERE memory_id=? AND state='pending'""",
            (key, key, key),
        )
    }


def track(db, entity, key, *, before):
    journal = _active.get()
    if journal is None:
        return
    after = image(db, entity, key)
    db.execute(
        """INSERT INTO undo_effects(session_id,turn_id,entity,entity_id,before_row,after_row,prior_references)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id,turn_id,entity,entity_id) DO UPDATE SET
        conflicted=conflicted OR undo_effects.after_row IS NOT excluded.before_row,after_row=excluded.after_row""",
        (
            journal.session,
            journal.turn,
            entity,
            str(key),
            encode(before) if before is not None else None,
            encode(after) if after is not None else None,
            encode(sorted(references(db, key)) if entity == "memories" else []),
        ),
    )


class TurnJournal:
    def __init__(self, paths, session, turn):
        self.paths, self.session, self.turn = paths, session, turn

    @contextmanager
    def capture(self):
        token = _active.set(self)
        try:
            yield
        finally:
            _active.reset(token)

    def has_effects(self):
        with transaction(self.paths, write=False) as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM undo_effects WHERE session_id=? AND turn_id=? LIMIT 1",
                    (self.session, self.turn),
                ).fetchone()
            )

    def restore(self):
        with transaction(self.paths) as db:
            rows = db.execute(
                "SELECT * FROM undo_effects WHERE session_id=? AND turn_id=? ORDER BY seq DESC",
                (self.session, self.turn),
            ).fetchall()
            for row in rows:
                current = image(db, row["entity"], row["entity_id"])
                expected = json.loads(row["after_row"]) if row["after_row"] is not None else None
                if row["entity"] == "topics":
                    continue
                if row["conflicted"] or current != expected:
                    raise TurnUndoError(
                        "Another operation changed an affected record. The turn was preserved."
                    )
            for row in rows:
                table, key = row["entity"], row["entity_id"]
                before = json.loads(row["before_row"]) if row["before_row"] is not None else None
                if table == "topics":
                    expected = json.loads(row["after_row"])
                    db.execute(
                        "DELETE FROM topics WHERE name=? AND title=? AND NOT EXISTS(SELECT 1 FROM memories WHERE topic=?)",
                        (key, expected["title"], key),
                    )
                    continue
                if before is None:
                    db.execute(f"DELETE FROM {table} WHERE {_KEYS[table]}=?", (key,))
                    continue
                if "revision" in before:
                    # Reversal is another write. Never reuse a version held by a late task.
                    version = db.execute(
                        "SELECT version+1 FROM revisions WHERE domain=?", (table,)
                    ).fetchone()[0]
                    original = dict(before)
                    before["revision"] = version
                    db.execute(
                        """UPDATE undo_effects SET after_row=? WHERE entity=? AND entity_id=?
                                  AND seq<? AND after_row=?""",
                        (encode(before), table, key, row["seq"], encode(original)),
                    )
                links = before.pop("_links", [])
                cols = list(before)
                assignments = ",".join(f"{c}=excluded.{c}" for c in cols if c != _KEYS[table])
                db.execute(
                    f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)}) ON CONFLICT({_KEYS[table]}) DO UPDATE SET {assignments}",
                    tuple(before.values()),
                )
                if table in _LINKS:
                    relation, fk, _ = _LINKS[table]
                    db.execute(f"DELETE FROM {relation} WHERE {fk}=?", (key,))
                    for link in links:
                        db.execute(
                            f"INSERT INTO {relation}({','.join(link)}) VALUES({','.join('?' for _ in link)})",
                            tuple(link.values()),
                        )
            for row in rows:
                if row["entity"] == "memories":
                    allowed = (
                        set(json.loads(row["prior_references"])) if row["before_row"] else set()
                    )
                    if references(db, row["entity_id"]) - allowed:
                        raise TurnUndoError("Another record now references an affected memory.")
            db.execute(
                "DELETE FROM undo_effects WHERE session_id=? AND turn_id=?",
                (self.session, self.turn),
            )
            return len(rows)
