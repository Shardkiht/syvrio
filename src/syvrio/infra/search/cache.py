from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Callable

from .base import SearchFailure, SearchResult

CACHE_VERSION = 2
_MAX_META_ITEMS = 20
_WRITE_LOCK = Lock()


@dataclass(slots=True)
class CachedSearch:
    results: list[SearchResult]
    provider: str
    strategy: str = "auto"
    attempted: list[str] = field(default_factory=list)
    contributors: list[str] = field(default_factory=list)
    # complete / incomplete / unknown / ""（非 compare 策略）。
    verification: str = ""
    failed: list[SearchFailure] = field(default_factory=list)


def _expires(entry: dict) -> float:
    try:
        return float(entry.get("expires", 0))
    except (TypeError, ValueError):
        return 0.0


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)][:_MAX_META_ITEMS]


class SearchCache:
    """搜索结果的文件缓存；只是派生数据，损坏或过期都可直接丢弃。"""

    def __init__(
        self,
        path: Path,
        ttl: int,
        clock: Callable[[], float] = time.time,
    ):
        self.path = path
        self.ttl = ttl
        self.clock = clock

    def get(self, key: str) -> tuple[list[SearchResult], str] | None:
        record = self.get_record(key)
        return None if record is None else (record.results, record.provider)

    def get_record(self, key: str) -> CachedSearch | None:
        """返回结果及验证元数据；旧格式或损坏条目不猜测验证状态。"""
        if self.ttl <= 0:
            return None
        entry = self._load().get("entries", {}).get(key)
        if not isinstance(entry, dict):
            return None
        if _expires(entry) <= self.clock():
            return None
        try:
            results = [
                SearchResult(**item)
                for item in entry.get("results", [])
                if isinstance(item, dict)
            ]
        except (TypeError, ValueError):
            # 派生缓存字段损坏时按未命中处理，不能让搜索整体失败。
            return None
        strategy = entry.get("strategy", "auto")
        if not isinstance(strategy, str):
            strategy = "auto"
        verification = entry.get("verification", "")
        if not isinstance(verification, str) or verification not in (
            "complete", "incomplete", "unknown", ""
        ):
            verification = "unknown" if strategy == "compare" else ""
        if strategy == "compare" and not verification:
            # 缺少验证字段的 compare 缓存绝不能被当成已完成。
            verification = "unknown"
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
            contributors=_string_list(entry.get("contributors")),
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
        # Parallel tools may share this cache. Merge and publish under one lock.
        with _WRITE_LOCK:
            data = self._load()
            now = self.clock()
            entries = {
                item_key: item
                for item_key, item in data.get("entries", {}).items()
                if isinstance(item, dict) and _expires(item) > now
            }
            entries[key] = {
                "expires": now + (self.ttl if ttl is None else min(ttl, self.ttl)),
                "provider": provider,
                "strategy": strategy,
                "attempted": list(attempted)[:_MAX_META_ITEMS],
                "contributors": list(contributors)[:_MAX_META_ITEMS],
                "verification": verification,
                "failed": [asdict(item) for item in list(failed)[:_MAX_META_ITEMS]],
                "results": [asdict(result) for result in results],
            }
            data = {"version": CACHE_VERSION, "entries": entries}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                # 缓存写失败不影响搜索结果；下次直接重新请求。
                return

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": CACHE_VERSION, "entries": {}}
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            # v1 没有验证元数据；整体失效，避免把未知状态猜成完成。
            return {"version": CACHE_VERSION, "entries": {}}
        if not isinstance(data.get("entries"), dict):
            return {"version": CACHE_VERSION, "entries": {}}
        return data
