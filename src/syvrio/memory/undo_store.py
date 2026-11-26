"""存放回合变更的前后像、引用关系和原子恢复操作。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ..infra.storage.database import encode, transaction
from ..infra.storage.paths import SyvrioPaths
from .source_links import SOURCES_BY_OWNER, references

_active: ContextVar[tuple[str, str] | None] = ContextVar("turn_journal", default=None)
_KEYS = {
    "topics": "name",
    "memories": "id",
    "inferences": "id",
    "candidates": "id",
    "memory_changes": "seq",
}


def image(db, entity, key):
    row = db.execute(f"SELECT * FROM {entity} WHERE {_KEYS[entity]}=?", (key,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    if source := SOURCES_BY_OWNER.get(entity):
        result["_links"] = source.rows(db, key)
    return result


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
            journal[0],
            journal[1],
            entity,
            str(key),
            encode(before) if before is not None else None,
            encode(after) if after is not None else None,
            encode(sorted(references(db, key)) if entity == "memories" else []),
        ),
    )


@dataclass(frozen=True)
class UndoEffect:
    seq: int
    entity: str
    key: str
    before: dict | None
    after: dict | None
    conflicted: bool
    prior_references: set[str]


class UndoBatch:
    def __init__(self, db, effects: list[UndoEffect]):
        self.db = db
        self.effects = effects

    def was_changed(self, effect: UndoEffect) -> bool:
        # 本回合新增的主题可被其他事实共用，只在恢复时有条件清掉空主题。
        return effect.entity != "topics" and (
            effect.conflicted or image(self.db, effect.entity, effect.key) != effect.after
        )

    def has_new_references(self, effect: UndoEffect) -> bool:
        if effect.entity != "memories":
            return False
        allowed = effect.prior_references if effect.before is not None else set()
        return bool(references(self.db, effect.key) - allowed)

    def restore(self, effect: UndoEffect) -> None:
        db, table, key = self.db, effect.entity, effect.key
        before = effect.before.copy() if effect.before is not None else None
        if table == "topics":
            expected = effect.after
            db.execute(
                "DELETE FROM topics WHERE name=? AND title=? AND NOT EXISTS(SELECT 1 FROM memories WHERE topic=?)",
                (key, expected["title"], key),
            )
            return
        if before is None:
            db.execute(f"DELETE FROM {table} WHERE {_KEYS[table]}=?", (key,))
            return
        if "revision" in before:
            # 恢复也是一次变更，迟到的后台结果不能复用旧版本。
            version = db.execute(
                "SELECT version+1 FROM revisions WHERE domain=?", (table,)
            ).fetchone()[0]
            original = dict(before)
            before["revision"] = version
            db.execute(
                """UPDATE undo_effects SET after_row=? WHERE entity=? AND entity_id=?
                          AND seq<? AND after_row=?""",
                (encode(before), table, key, effect.seq, encode(original)),
            )
        links = before.pop("_links", [])
        cols = list(before)
        assignments = ",".join(f"{c}=excluded.{c}" for c in cols if c != _KEYS[table])
        db.execute(
            f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)}) ON CONFLICT({_KEYS[table]}) DO UPDATE SET {assignments}",
            tuple(before.values()),
        )
        if source := SOURCES_BY_OWNER.get(table):
            source.restore(db, key, links)


class UndoStore:
    def __init__(self, paths: SyvrioPaths, session: str, turn: str):
        self.paths = paths
        self.owner = session, turn

    @contextmanager
    def capture(self) -> Iterator[None]:
        token = _active.set(self.owner)
        try:
            yield
        finally:
            _active.reset(token)

    def has_effects(self) -> bool:
        with transaction(self.paths, write=False) as db:
            return (
                db.execute(
                    "SELECT 1 FROM undo_effects WHERE session_id=? AND turn_id=? LIMIT 1",
                    self.owner,
                ).fetchone()
                is not None
            )

    @contextmanager
    def reversal(self) -> Iterator[UndoBatch]:
        with transaction(self.paths) as db:
            rows = db.execute(
                "SELECT * FROM undo_effects WHERE session_id=? AND turn_id=? ORDER BY seq DESC",
                self.owner,
            )
            effects = [
                UndoEffect(
                    row["seq"],
                    row["entity"],
                    row["entity_id"],
                    json.loads(row["before_row"]) if row["before_row"] is not None else None,
                    json.loads(row["after_row"]) if row["after_row"] is not None else None,
                    bool(row["conflicted"]),
                    set(json.loads(row["prior_references"])),
                )
                for row in rows
            ]
            yield UndoBatch(db, effects)
            db.execute("DELETE FROM undo_effects WHERE session_id=? AND turn_id=?", self.owner)
