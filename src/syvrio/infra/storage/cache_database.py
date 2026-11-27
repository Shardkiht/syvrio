"""可重建的检索缓存各自使用 Runtime 数据库，不参加 Vault 的业务事务。"""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import Literal

from .paths import SyvrioPaths
from .separate_database import SeparateDatabase

CacheName = Literal["vectors", "web-search"]
_CACHE_SCHEMAS = {
    "vectors": SeparateDatabase(
        0x53595643,
        "cache",
        (
            """CREATE TABLE vectors(
            memory_id TEXT NOT NULL, model TEXT NOT NULL, input_digest TEXT NOT NULL,
            dimensions INTEGER NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(memory_id,model))""",
        ),
    ),
    "web-search": SeparateDatabase(
        0x53595343,
        "cache",
        (
            """CREATE TABLE search_cache(
            cache_key TEXT PRIMARY KEY, expires REAL NOT NULL, result TEXT NOT NULL)""",
        ),
    ),
}


def cache_transaction(
    paths: SyvrioPaths, name: CacheName, *, write: bool = True
) -> AbstractContextManager[sqlite3.Connection]:
    return _CACHE_SCHEMAS[name].transaction(paths.cache_dir / f"{name}.sqlite3", write=write)
