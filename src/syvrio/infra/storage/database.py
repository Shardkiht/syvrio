"""按线程和 asyncio 任务划分连接；子操作加入当前工作单元并使用保存点。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import SyvrioError
from .paths import SyvrioPaths, same_storage_path

APPLICATION_ID = 0x53595652


class DatabaseError(SyvrioError, ValueError):
    """数据库结构、归属或事务用法无效。"""


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass
class Unit:
    path: Path
    connection: sqlite3.Connection
    owner: tuple
    writable: bool
    depth: int = 0
    preparing: list[Callable[[sqlite3.Connection], None]] = field(default_factory=list)
    committed: list = field(default_factory=list)
    reversals: list[Callable[[], None]] = field(default_factory=list)


_current: ContextVar[Unit | None] = ContextVar("syvrio_db_unit", default=None)


def _owner() -> tuple:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


def database_path(paths: SyvrioPaths) -> Path:
    return paths.vault_dir / "syvrio.sqlite3"


def _check_layout(db: sqlite3.Connection) -> None:
    # 只编译查询来核对当前读写需要的结构，不扫描聊天记录。
    try:
        db.execute("""SELECT e.distilled, e.reasoning, e.tool_preview, e.tool_length, s.ready,
                        a.size, u.reason, u.resume_at, g.state,
                        v.reserved, m.commit_id, i.commit_id, tv.commit_id, l.memory_version,
                        log.reverts, log.memory_change, j.checkpoint, j.evidence_commit,
                        ms.first_revision, ms.second_revision, ms.kind, ms.state
                   FROM session_events e, search_signatures s, attachments a,
                        provider_pauses u, git_sync g, revisions v, memory_versions m,
                        inference_versions i, topic_versions tv, inference_sources l,
                        audit log, jobs j, maintenance_suggestions ms LIMIT 0""")
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode != sqlite3.SQLITE_ERROR:
            raise
        raise DatabaseError("The Vault database does not have the current Syvrio layout.") from exc


def require_current_schema(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise DatabaseError("The configured database does not belong to Syvrio.")
    _check_layout(db)


def _initialize(db: sqlite3.Connection) -> None:
    owner, pages = db.execute(
        "SELECT application_id,page_count FROM pragma_application_id,pragma_page_count"
    ).fetchone()
    if owner == APPLICATION_ID:
        _check_layout(db)
        return
    if owner or pages:
        raise DatabaseError("Choose an empty database or an existing Syvrio Vault.")
    # 只为新库取得写锁。并发首次打开时，等待方复核先取得锁的一方已提交的结构。
    db.execute("BEGIN IMMEDIATE")
    owner = db.execute("PRAGMA application_id").fetchone()[0]
    if owner == APPLICATION_ID:
        _check_layout(db)
    elif owner or db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
        raise DatabaseError("The configured database does not belong to Syvrio.")
    else:
        _create_schema(db)
    db.commit()


def _create_schema(db: sqlite3.Connection) -> None:
    # 逐条执行，保证当前表、索引、触发器和应用标识在同一事务内发布。
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
    _install_projections(db)


def _install_projections(db: sqlite3.Connection) -> None:
    from . import evidence_schema, projection_revisions, session_indexes

    evidence_schema.install(db)
    session_indexes.install(db)
    projection_revisions.install(db)


def keep_wal_open(paths: SyvrioPaths) -> sqlite3.Connection:
    """由持有 Vault 的后端保留无事务连接，短事务关闭时无需反复清空 WAL。"""
    with transaction(paths):
        pass
    db = sqlite3.connect(database_path(paths), isolation_level=None, check_same_thread=False)
    try:
        db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone()
        return db
    except BaseException:
        db.close()
        raise


def transaction_mode() -> str | None:
    """返回当前任务的事务模式，用于决定是否可以共享或发布缓存。"""
    unit = _current.get()
    if unit is None or unit.owner != _owner():
        return None
    return "write" if unit.writable else "read"


def before_commit(callback: Callable[[sqlite3.Connection], None]) -> None:
    """登记当前写事务提交前的动作；保存点回滚时同时撤销登记。"""
    unit = _current.get()
    if unit is None or unit.owner != _owner() or not unit.writable:
        raise DatabaseError("Commit preparation outside its write transaction.")
    unit.preparing.append(callback)


def on_rollback(callback: Callable[[], None]) -> None:
    """写事务或登记它的保存点回滚时，按倒序撤回关联的内存变更。"""
    unit = _current.get()
    if unit is None or unit.owner != _owner() or not unit.writable:
        raise DatabaseError("Rollback callback outside its write transaction.")
    unit.reversals.append(callback)


def _revert(unit: Unit, start: int = 0) -> None:
    with ExitStack() as cleanup:
        for callback in unit.reversals[start:]:
            cleanup.callback(callback)
        del unit.reversals[start:]


def on_commit(callback):
    unit = _current.get()
    if unit is None or unit.owner != _owner():
        raise DatabaseError("Commit callback outside its transaction.")
    unit.committed.append(callback)


@contextmanager
def committed_read(paths: SyvrioPaths) -> Iterator[sqlite3.Connection]:
    """为对外发布读取已提交快照，屏蔽调用方尚未提交的写入。"""
    token = _current.set(None)
    try:
        with transaction(paths, write=False) as db:
            yield db
    finally:
        _current.reset(token)


def after_commit(callback: Callable[[], None]) -> None:
    """没有活动事务时立即发布，嵌套事务回滚时随之丢弃。"""
    if transaction_mode() is None:
        callback()
    else:
        on_commit(callback)


@contextmanager
def wal_connection(
    path: Path, *, initialize: Callable[[sqlite3.Connection], None] | None = None
) -> Iterator[sqlite3.Connection]:
    """主库和派生缓存使用独立的 WAL，同时遵守同一套并发打开和刷盘设置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        if initialize is not None:
            initialize(db)
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            deadline = time.monotonic() + 10
            while True:
                try:
                    mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                    break
                except sqlite3.OperationalError as exc:
                    if (
                        exc.sqlite_errorcode & 255 != sqlite3.SQLITE_BUSY
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(0.025)
            if mode != "wal":
                raise DatabaseError("WAL is unavailable for this database.")
        db.execute("PRAGMA synchronous=FULL")
        yield db
    finally:
        db.close()


@contextmanager
def transaction(paths: SyvrioPaths, *, write: bool = True) -> Iterator[sqlite3.Connection]:
    if sqlite3.sqlite_version_info < (3, 38, 0):
        raise DatabaseError("Syvrio requires SQLite 3.38 or later for built-in JSON support.")
    path = database_path(paths)
    owner = _owner()
    parent = _current.get()
    if parent is not None and parent.owner == owner:
        if not same_storage_path(path, parent.path) or write and not parent.writable:
            raise DatabaseError(
                "A nested operation cannot switch databases or promote a read snapshot."
            )
        db = parent.connection
        reversals = len(parent.reversals)
        preparations = len(parent.preparing)
        callbacks = len(parent.committed)
        parent.depth += 1
        savepoint = f"nested_{parent.depth}"
        try:
            db.execute(f"SAVEPOINT {savepoint}")
            try:
                yield db
            except BaseException:
                del parent.preparing[preparations:]
                del parent.committed[callbacks:]
                try:
                    _revert(parent, reversals)
                finally:
                    db.execute(f"ROLLBACK TO {savepoint}")
                raise
            finally:
                db.execute(f"RELEASE {savepoint}")
        finally:
            parent.depth -= 1
        return
    # 嵌套操作已经绑定同一个已打开的句柄；只有重新打开文件才需检查路径。
    if not same_storage_path(path.resolve(), path):
        raise DatabaseError("The database must be inside the configured Vault.")
    with wal_connection(path, initialize=_initialize) as db:
        if not write:
            db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        unit = Unit(path, db, owner, write)
        token = _current.set(unit)
        try:
            yield db
            for callback in unit.preparing:
                callback(db)
            db.commit()
        except BaseException:
            try:
                _revert(unit)
            finally:
                db.rollback()
            raise
        finally:
            _current.reset(token)
        for callback in unit.committed:
            callback()
