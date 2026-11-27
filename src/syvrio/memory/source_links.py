"""推断和候选各自的来源关系；写入、引用检查和恢复共用同一份定义。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceLinks:
    owner: str
    table: str
    owner_column: str
    label: str
    pending_only: bool = False

    def add(self, db: sqlite3.Connection, key: str, ids: list[str]) -> None:
        db.executemany(
            f"INSERT INTO {self.table}({self.owner_column},position,memory_id) VALUES(?,?,?)",
            [(key, i, memory) for i, memory in enumerate(ids)],
        )

    def rows(self, db: sqlite3.Connection, key: str) -> list[dict]:
        return [
            dict(row)
            for row in db.execute(
                f"SELECT * FROM {self.table} WHERE {self.owner_column}=? ORDER BY rowid", (key,)
            )
        ]

    def restore(self, db: sqlite3.Connection, key: str, rows: list[dict]) -> None:
        db.execute(f"DELETE FROM {self.table} WHERE {self.owner_column}=?", (key,))
        if self is INFERENCE_SOURCES:
            db.executemany(
                """INSERT INTO inference_sources(inference_id,position,memory_id,memory_version)
                VALUES(?,?,?,?)""",
                [(key, r["position"], r["memory_id"], r["memory_version"]) for r in rows],
            )
        else:
            self.add(
                db, key, [row["memory_id"] for row in sorted(rows, key=lambda r: r["position"])]
            )

    def owners_query(self) -> str:
        owners = f"SELECT '{self.label}:'||link.{self.owner_column} FROM {self.table} link"
        if self.pending_only:
            return (
                owners
                + f" JOIN {self.owner} item ON item.id=link.{self.owner_column} WHERE link.memory_id=:memory AND item.state='pending'"
            )
        return owners + " WHERE link.memory_id=:memory"


INFERENCE_SOURCES = SourceLinks("inferences", "inference_sources", "inference_id", "inference")
CANDIDATE_SOURCES = SourceLinks(
    "candidates", "candidate_related", "candidate_id", "pending", pending_only=True
)
SOURCES_BY_OWNER = {source.owner: source for source in (INFERENCE_SOURCES, CANDIDATE_SOURCES)}
_REFERENCE_QUERY = " UNION ".join(
    (
        "SELECT 'memory:'||id FROM memories WHERE supersedes=:memory",
        *(source.owners_query() for source in SOURCES_BY_OWNER.values()),
    )
)


def references(db: sqlite3.Connection, key: str) -> set[str]:
    return {row[0] for row in db.execute(_REFERENCE_QUERY, {"memory": key})}
