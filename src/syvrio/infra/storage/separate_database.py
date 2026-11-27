"""按用途独立打开 SQLite 文件，只初始化空库并核对当前表结构。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from .database import DatabaseError, wal_connection
from .paths import same_storage_path


def _columns(db: sqlite3.Connection, table: str) -> list[tuple]:
    return [
        tuple(row)
        for row in db.execute("SELECT * FROM pragma_table_info(?) ORDER BY cid", (table,))
    ]


@dataclass(frozen=True)
class SeparateDatabase:
    application_id: int
    description: str
    statements: tuple[str, ...]

    @cached_property
    def _layout(self) -> dict[str, list[tuple]]:
        # 从实际建表语句提取列及主键，结构检验不依赖 SQL 的字面排版。
        with closing(sqlite3.connect(":memory:")) as template:
            for statement in self.statements:
                template.execute(statement)
            return {
                name: _columns(template, name)
                for (name,) in template.execute("SELECT name FROM sqlite_schema WHERE type='table'")
            }

    def require_schema(self, db: sqlite3.Connection) -> None:
        owner = db.execute("PRAGMA application_id").fetchone()[0]
        if owner != self.application_id or any(
            _columns(db, name) != columns for name, columns in self._layout.items()
        ):
            raise DatabaseError(f"Unsupported {self.description} database.")

    def _initialize(self, db: sqlite3.Connection) -> None:
        owner, pages = db.execute(
            "SELECT application_id,page_count FROM pragma_application_id,pragma_page_count"
        ).fetchone()
        if owner == self.application_id:
            self.require_schema(db)
            return
        if owner or pages:
            raise DatabaseError(f"Unsupported {self.description} database.")
        # 仅空文件获取写锁。另一进程可能已经先取得锁并提交了完整结构。
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA application_id").fetchone()[0]:
            self.require_schema(db)
        elif db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
            raise DatabaseError(f"Unsupported {self.description} database.")
        else:
            for statement in self.statements:
                db.execute(statement)
            db.execute(f"PRAGMA application_id={self.application_id}")
        db.commit()

    @contextmanager
    def transaction(self, path: Path, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        if not same_storage_path(path.resolve(), path) or any(
            path.with_name(path.name + suffix).is_symlink()
            for suffix in ("-wal", "-shm", "-journal")
        ):
            raise DatabaseError(
                f"The {self.description} database must be inside its configured directory."
            )
        with wal_connection(path, initialize=self._initialize) as db:
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
