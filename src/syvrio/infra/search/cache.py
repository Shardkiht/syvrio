from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from ..observability.diagnostics import record_once
from ..storage.cache_database import cache_transaction
from ..storage.database import DatabaseError, encode
from ..storage.paths import SyvrioPaths
from .base import SearchFailure, SearchResult

_MAX_META_ITEMS = 20


@dataclass(slots=True)
class CachedSearch:
    results: list[SearchResult]
    provider: str
    strategy: str = "auto"
    attempted: list[str] = field(default_factory=list)
    contributors: list[str] = field(default_factory=list)
    # complete / incomplete / ""（非 compare 策略）。
    verification: str = ""
    failed: list[SearchFailure] = field(default_factory=list)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)][:_MAX_META_ITEMS]


class SearchCache:
    """在 Runtime 缓存有期限的网页搜索结果。"""

    def __init__(
        self,
        paths: SyvrioPaths,
        ttl: int,
        clock: Callable[[], float] = time.time,
    ):
        self.paths = paths
        self.ttl = ttl
        self.clock = clock

    def get_record(self, key: str) -> CachedSearch | None:
        """返回结果及验证元数据；字段缺失或损坏时重新搜索。"""
        if self.ttl <= 0:
            return None
        try:
            with cache_transaction(self.paths, "web-search", write=False) as db:
                row = db.execute(
                    "SELECT result FROM search_cache WHERE cache_key=? AND expires>?",
                    (key, self.clock()),
                ).fetchone()
        except (OSError, sqlite3.Error, DatabaseError) as exc:
            # 缓存无法读取时，仍由供应商返回这次的搜索结果。
            record_once("degraded.web_search_cache", exc)
            return None
        if not row:
            return None
        try:
            entry = json.loads(row[0])
            if not isinstance(entry, dict) or not isinstance(entry.get("results"), list):
                return None
            results = [
                SearchResult(**item) for item in entry.get("results", []) if isinstance(item, dict)
            ]
        except (TypeError, ValueError):
            # 派生缓存字段损坏时按未命中处理，不能让搜索整体失败。
            return None
        strategy = entry.get("strategy")
        verification = entry.get("verification")
        if strategy not in ("auto", "strict", "compare"):
            return None
        if verification not in (("complete", "incomplete") if strategy == "compare" else ("",)):
            return None
        contributors = _string_list(entry.get("contributors"))
        if verification == "complete" and len(set(contributors)) < 2:
            return None
        failed: list[SearchFailure] = []
        raw_failed = entry.get("failed", [])
        if isinstance(raw_failed, list):
            for item in raw_failed[:_MAX_META_ITEMS]:
                if not isinstance(item, dict):
                    continue
                provider = item.get("provider", "")
                kind = item.get("kind", "")
                detail = item.get("detail", "")
                if not all(isinstance(value, str) for value in (provider, kind, detail)):
                    continue
                failed.append(SearchFailure(provider, kind, detail))
        return CachedSearch(
            results=results,
            provider=str(entry.get("provider", "")),
            strategy=strategy,
            attempted=_string_list(entry.get("attempted")),
            contributors=contributors,
            verification=verification,
            failed=failed,
        )

    def set(
        self,
        key: str,
        results: list[SearchResult],
        provider: str,
        ttl: float | None = None,
        *,
        strategy: str = "auto",
        attempted: list[str] | tuple[str, ...] = (),
        contributors: list[str] | tuple[str, ...] = (),
        verification: str = "",
        failed: list[SearchFailure] | tuple[SearchFailure, ...] = (),
    ) -> None:
        """ttl 为空时用实例默认值；空结果等短命条目传更短的 ttl。"""
        if self.ttl <= 0:
            return
        now = self.clock()
        expires = now + (self.ttl if ttl is None else min(ttl, self.ttl))
        result = dict(
            provider=provider,
            strategy=strategy,
            attempted=list(attempted)[:_MAX_META_ITEMS],
            contributors=list(contributors)[:_MAX_META_ITEMS],
            verification=verification,
            failed=[asdict(item) for item in list(failed)[:_MAX_META_ITEMS]],
            results=[asdict(item) for item in results],
        )
        try:
            with cache_transaction(self.paths, "web-search") as db:
                db.execute("DELETE FROM search_cache WHERE expires<=?", (now,))
                db.execute(
                    "INSERT OR REPLACE INTO search_cache VALUES(?,?,?)",
                    (key, expires, encode(result)),
                )
        except (OSError, sqlite3.Error, DatabaseError) as exc:
            # 本次结果已经取得，缓存写入失败只影响后续复用。
            record_once("degraded.web_search_cache", exc)
