"""本地请求保护按日历周期计数，供应商账户余额另行计算。"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
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


@dataclass(slots=True)
class UsageReservation:
    provider: str
    daily: str
    monthly: str
    refunded: bool = False


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
            self._counts = {
                provider: {
                    field: value[field]
                    for field in ("daily", "monthly", "paused")
                    if isinstance(value.get(field), dict)
                }
                for provider, value in data.items()
                if isinstance(value, dict)
            }

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._counts, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    def _entry(self, provider: str) -> dict:
        now = self._clock()
        entry = self._counts.setdefault(provider, {})
        for period in ("daily", "monthly"):
            key = window_key(period, now)
            counter = entry.get(period)
            if not isinstance(counter, dict) or counter.get("window") != key:
                entry[period] = {"window": key, "count": 0}
            elif not isinstance(counter.get("count"), int):
                counter["count"] = 0
        paused = entry.get("paused")
        if isinstance(paused, dict) and isinstance(paused.get("resume_at"), (int, float)):
            if now >= paused["resume_at"]:
                entry.pop("paused", None)
        return entry

    def status(self, provider: str) -> dict:
        with self._lock:
            self._load()
            entry = self._entry(provider)
            return self._status(provider, entry)

    def _status(self, provider: str, entry: dict) -> dict:
        limit = self._limits.get(provider)
        result = {"paused": dict(entry.get("paused") or {})}
        for period in ("daily", "monthly"):
            maximum = getattr(limit, period, 0)
            count = entry[period]["count"]
            result[period] = {
                "limit": maximum,
                "used": count,
                "remaining": max(0, maximum - count) if maximum > 0 else None,
            }
        return result

    @staticmethod
    def _remaining(status: dict) -> int | None:
        if status["paused"]:
            return 0
        values = [
            status[p]["remaining"]
            for p in ("daily", "monthly")
            if status[p]["remaining"] is not None
        ]
        return min(values) if values else None

    def remaining(self, provider: str) -> int | None:
        return self._remaining(self.status(provider))

    def allows(self, provider: str) -> bool:
        return self.remaining(provider) != 0

    def reason(self, provider: str) -> str:
        status = self.status(provider)
        if status["paused"]:
            return "Official quota pause: " + str(status["paused"].get("reason", "quota exhausted"))
        return "已达到本地调用上限，本次跳过该厂商"

    def acquire(self, provider: str) -> UsageReservation | None:
        """在同一日历时刻检查并预扣额度，记录各自所属的计数周期。"""
        with self._lock:
            self._load()
            entry = self._entry(provider)
            if self._remaining(self._status(provider, entry)) == 0:
                return None
            self._increment(entry)
            return UsageReservation(provider, entry["daily"]["window"], entry["monthly"]["window"])

    def _increment(self, entry: dict) -> None:
        for period in ("daily", "monthly"):
            entry[period]["count"] += 1
        self._save()

    def record(self, provider: str) -> None:
        with self._lock:
            self._load()
            self._increment(self._entry(provider))

    def release(self, reservation: UsageReservation) -> None:
        """原日或月窗口仍有效时才退回其计数。"""
        with self._lock:
            if reservation.refunded:
                return
            self._load()
            entry = self._entry(reservation.provider)
            for period, charged_window in (
                ("daily", reservation.daily),
                ("monthly", reservation.monthly),
            ):
                counter = entry[period]
                if counter["window"] == charged_window and counter["count"] > 0:
                    counter["count"] -= 1
            self._save()
            reservation.refunded = True

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
