"""按线程和 asyncio 任务划分连接；子操作加入当前工作单元并使用保存点。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import SyvrioError
from .paths import SyvrioPaths

SCHEMA_VERSION = 1
APPLICATION_ID = 0x53595652


class DatabaseError(SyvrioError, ValueError):
    """数据库版本、归属或事务用法无效。"""


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass
class Unit:
    path: Path
    connection: sqlite3.Connection
    owner: tuple
    writable: bool
    depth: int = 0
    committed: list = field(default_factory=list)


_current: ContextVar[Unit | None] = ContextVar("syvrio_db_unit", default=None)


def _owner() -> tuple:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


def database_path(paths: SyvrioPaths) -> Path:
    return paths.vault_dir / "syvrio.sqlite3"


def _schema(db: sqlite3.Connection) -> None:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    app = db.execute("PRAGMA application_id").fetchone()[0]
    if (version, app) == (SCHEMA_VERSION, APPLICATION_ID):
        return
    if version or app or db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
        raise DatabaseError(
            "Unsupported database. Open a Syvrio SQLite backup or an empty database."
        )
    # executescript 会隐式提交，逐条执行，建库及版本号同一事务发布。
    sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise DatabaseError("Incomplete schema.")
    for domain in ("memories", "inferences", "candidates"):
        for event in ("insert", "update", "delete"):
            db.execute(f"""CREATE TRIGGER rev_{domain}_{event} AFTER {event} ON {domain}
                BEGIN UPDATE revisions SET version=version+1 WHERE domain='{domain}'; END""")
    db.execute(f"PRAGMA application_id={APPLICATION_ID}")
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def on_commit(callback):
    unit = _current.get()
    if unit is None or unit.owner != _owner():
        raise DatabaseError("Commit callback outside its transaction.")
    unit.committed.append(callback)


@contextmanager
def transaction(paths: SyvrioPaths, *, write: bool = True) -> Iterator[sqlite3.Connection]:
    if sqlite3.sqlite_version_info < (3, 38, 0):
        raise DatabaseError("Syvrio requires SQLite 3.38 or later for built-in JSON support.")
    path = database_path(paths)
    if path.resolve() != path:
        raise DatabaseError("The database must be inside the configured Vault.")
    owner = _owner()
    parent = _current.get()
    if parent is not None and parent.owner == owner:
        if path != parent.path or write and not parent.writable:
            raise DatabaseError(
                "A nested operation cannot switch databases or promote a read snapshot."
            )
        db = parent.connection
        callbacks = len(parent.committed)
        parent.depth += 1
        savepoint = f"nested_{parent.depth}"
        try:
            db.execute(f"SAVEPOINT {savepoint}")
            try:
                yield db
            except BaseException:
                del parent.committed[callbacks:]
                db.execute(f"ROLLBACK TO {savepoint}")
                raise
            finally:
                db.execute(f"RELEASE {savepoint}")
        finally:
            parent.depth -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            deadline = time.monotonic() + 10
            while True:
                try:
                    mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                    break
                except sqlite3.OperationalError as exc:
                    if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.025)
            if mode != "wal":
                raise DatabaseError("WAL is unavailable for this Vault.")
        db.execute("PRAGMA synchronous=FULL")
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            db.execute("BEGIN IMMEDIATE")
            _schema(db)
            db.commit()
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise DatabaseError("The configured database belongs to another application.")
        if not write:
            db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        unit = Unit(path, db, owner, write)
        token = _current.set(unit)
        try:
            yield db
            db.commit()
        finally:
            _current.reset(token)
        for callback in unit.committed:
            callback()
    finally:
        db.close()
