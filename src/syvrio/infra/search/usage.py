"""Local request protection; calendar counters are not provider billing balances."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Callable

USAGE_FILENAME = "usage.json"
_LOCKS: dict[str, RLock] = {}
_LOCKS_GUARD = RLock()


def window_key(period: str, now: float) -> str:
    moment = datetime.fromtimestamp(now)
    return moment.strftime("%Y-%m-%d") if period == "daily" else moment.strftime("%Y-%m")


class UsageLimiter:
    def __init__(
        self, limits: dict, path: Path | None = None, clock: Callable[[], float] = time.time
    ):
        self._limits = limits
        self._path = path
        self._clock = clock
        self._counts: dict[str, dict] = {}
        with _LOCKS_GUARD:
            self._lock = (
                _LOCKS.setdefault(os.path.normcase(str(path.resolve())), RLock())
                if path
                else RLock()
            )
        with self._lock:
            self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._counts = {name: value for name, value in data.items() if isinstance(value, dict)}

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._counts, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    def _entry(self, provider: str) -> dict:
        entry = self._counts.setdefault(provider, {})
        # Old records identify their one known period by the window shape.
        old_window = entry.get("window", "")
        if isinstance(old_window, str) and isinstance(entry.get("count"), int):
            period = (
                "daily" if len(old_window) == 10 else "monthly" if len(old_window) == 7 else None
            )
            if period and period not in entry:
                entry[period] = {"window": old_window, "count": entry["count"]}
                entry["legacy_unknown_period"] = "monthly" if period == "daily" else "daily"
        for period in ("daily", "monthly"):
            key = window_key(period, self._clock())
            counter = entry.get(period)
            if not isinstance(counter, dict) or counter.get("window") != key:
                entry[period] = {"window": key, "count": 0}
            elif not isinstance(counter.get("count"), int):
                counter["count"] = 0
        paused = entry.get("paused")
        if isinstance(paused, dict) and isinstance(paused.get("resume_at"), (int, float)):
            if self._clock() >= paused["resume_at"]:
                entry.pop("paused", None)
        return entry

    def status(self, provider: str) -> dict:
        with self._lock:
            self._load()
            entry = self._entry(provider)
            limit = self._limits.get(provider)
            result = {
                "paused": dict(entry.get("paused") or {}),
                "legacy_unknown_period": entry.get("legacy_unknown_period"),
            }
            for period in ("daily", "monthly"):
                maximum = getattr(limit, period, 0)
                count = entry[period]["count"]
                result[period] = {
                    "limit": maximum,
                    "used": count,
                    "remaining": max(0, maximum - count) if maximum > 0 else None,
                }
            return result

    def remaining(self, provider: str) -> int | None:
        status = self.status(provider)
        if status["paused"]:
            return 0
        values = [
            status[p]["remaining"]
            for p in ("daily", "monthly")
            if status[p]["remaining"] is not None
        ]
        return min(values) if values else None

    def allows(self, provider: str) -> bool:
        remaining = self.remaining(provider)
        return remaining is None or remaining > 0

    def reason(self, provider: str) -> str:
        status = self.status(provider)
        if status["paused"]:
            return "Official quota pause: " + str(status["paused"].get("reason", "quota exhausted"))
        return "已达到本地调用上限，本次跳过该厂商"

    def acquire(self, provider: str) -> bool:
        """Atomic in-process check + conservative accounting before dispatch."""
        with self._lock:
            if not self.allows(provider):
                return False
            self.record(provider)
            return True

    def record(self, provider: str) -> None:
        with self._lock:
            self._load()
            entry = self._entry(provider)
            for period in ("daily", "monthly"):
                entry[period]["count"] += 1
            # Preserve the old shape for callers still inspecting legacy records.
            limit = self._limits.get(provider)
            if getattr(limit, "period", None) in ("daily", "monthly"):
                entry.update(entry[limit.period])
            self._save()

    def pause(self, provider: str, reason: str, resume_at: float | None = None) -> None:
        with self._lock:
            self._load()
            self._entry(provider)["paused"] = {"reason": reason, "resume_at": resume_at}
            self._save()

    def note_error(self, provider: str, error) -> None:
        if error.pause_reason:
            self.pause(provider, error.pause_reason, error.resume_at)

    def resume(self, provider: str) -> None:
        with self._lock:
            self._load()
            self._entry(provider).pop("paused", None)
            self._save()
