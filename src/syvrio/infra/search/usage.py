"""本地请求保护按日历周期计数，供应商账户余额另行计算。"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Callable

from ..storage.database import transaction
from ..storage.paths import SyvrioPaths


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
        self, limits: dict, paths: SyvrioPaths | None = None, clock: Callable[[], float] = time.time
    ):
        self._limits = limits
        self.paths = paths
        self._clock = clock
        self._counts: dict[str, dict] = {}
        self._mutex = RLock()

    @contextmanager
    def _operation(self, provider, *, write: bool = True):
        if self.paths is None:
            with self._mutex:
                yield
            return
        # 嵌套调用可能已持有主库写锁。所有调用统一先取得数据库事务，再锁实例计数。
        with transaction(self.paths, write=write) as db, self._mutex:
            stored = {}
            for period, window, count in db.execute(
                "SELECT period,window,units FROM usage WHERE provider=?", (provider,)
            ):
                stored[period] = {"window": window, "count": count}
            pause = db.execute(
                "SELECT reason,resume_at FROM quota_pauses WHERE provider=?", (provider,)
            ).fetchone()
            if pause:
                stored["paused"] = {"reason": pause[0], "resume_at": pause[1]}
            entry = {key: row.copy() for key, row in stored.items()} if write else stored
            self._counts[provider] = entry
            yield
            if not write:
                return
            for period in ("daily", "monthly"):
                counter = entry.get(period)
                if not counter or counter == stored.get(period):
                    continue
                if period not in stored and counter["count"] == 0:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO usage VALUES(?,?,?,?)",
                    (provider, period, counter["window"], counter["count"]),
                )
            paused = entry.get("paused")
            if paused == stored.get("paused"):
                return
            if paused:
                db.execute(
                    "INSERT OR REPLACE INTO quota_pauses VALUES(?,?,?)",
                    (provider, paused["reason"], paused.get("resume_at")),
                )
            else:
                db.execute("DELETE FROM quota_pauses WHERE provider=?", (provider,))

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
        with self._operation(provider, write=False):
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
        with self._operation(provider):
            entry = self._entry(provider)
            if self._remaining(self._status(provider, entry)) == 0:
                return None
            self._increment(entry)
            return UsageReservation(provider, entry["daily"]["window"], entry["monthly"]["window"])

    def _increment(self, entry: dict) -> None:
        for period in ("daily", "monthly"):
            entry[period]["count"] += 1

    def record(self, provider: str) -> None:
        with self._operation(provider):
            self._increment(self._entry(provider))

    def release(self, reservation: UsageReservation) -> None:
        """原日或月窗口仍有效时才退回其计数。"""
        with self._operation(reservation.provider):
            if reservation.refunded:
                return
            entry = self._entry(reservation.provider)
            for period, charged_window in (
                ("daily", reservation.daily),
                ("monthly", reservation.monthly),
            ):
                counter = entry[period]
                if counter["window"] == charged_window and counter["count"] > 0:
                    counter["count"] -= 1
            reservation.refunded = True

    def pause(self, provider: str, reason: str, resume_at: float | None = None) -> None:
        with self._operation(provider):
            self._entry(provider)["paused"] = {"reason": reason, "resume_at": resume_at}

    def note_error(self, provider: str, error) -> None:
        if error.pause_reason:
            self.pause(provider, error.pause_reason, error.resume_at)

    def resume(self, provider: str) -> None:
        with self._operation(provider):
            self._entry(provider).pop("paused", None)
