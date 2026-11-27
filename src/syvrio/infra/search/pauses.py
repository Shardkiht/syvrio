from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..observability.diagnostics import record_event
from ..storage.database import transaction
from ..storage.paths import SyvrioPaths
from .base import ProviderError, ProviderErrorKind, SearchFailure
from .providers.catalog import PROVIDERS


@dataclass(frozen=True, slots=True)
class ProviderPause:
    reason: str
    resume_at: float | None


class ProviderPauses:
    def __init__(self, paths: SyvrioPaths, clock: Callable[[], float] = time.time):
        self.paths = paths
        self.clock = clock

    def get(self, provider: str) -> ProviderPause | None:
        with transaction(self.paths, write=False) as db:
            row = db.execute(
                "SELECT reason, resume_at FROM provider_pauses "
                "WHERE provider=? AND (resume_at IS NULL OR resume_at>?)",
                (provider, self.clock()),
            ).fetchone()
        return ProviderPause(*row) if row else None

    def active(self) -> dict[str, ProviderPause]:
        with transaction(self.paths, write=False) as db:
            rows = db.execute(
                "SELECT provider, reason, resume_at FROM provider_pauses "
                "WHERE resume_at IS NULL OR resume_at>? ORDER BY provider",
                (self.clock(),),
            )
            return {name: ProviderPause(reason, resume_at) for name, reason, resume_at in rows}

    def note_error(self, provider: str, error: ProviderError) -> None:
        if error.pause_reason:
            with transaction(self.paths) as db:
                db.execute(
                    """INSERT INTO provider_pauses(provider,reason,resume_at) VALUES(?,?,?)
                    ON CONFLICT(provider) DO UPDATE SET reason=excluded.reason,
                    resume_at=excluded.resume_at""",
                    (provider, error.pause_reason, error.resume_at),
                )

    def resume(self, provider: str) -> None:
        with transaction(self.paths) as db:
            db.execute("DELETE FROM provider_pauses WHERE provider=?", (provider,))


def _quota_failure(provider: str, reason: str) -> SearchFailure:
    # 上游错误正文可能含凭据。日志只写目录中的厂商，反馈只用适配器识别出的原因。
    record_event(
        "search.quota_exhausted", provider=provider if provider in PROVIDERS else "unknown"
    )
    detail = f"Official quota pause: {reason}" if reason else "Provider quota exhausted"
    return SearchFailure(provider, ProviderErrorKind.QUOTA.value, detail)


def paused_failure(provider: str, pauses: ProviderPauses | None) -> SearchFailure | None:
    pause = pauses.get(provider) if pauses else None
    return _quota_failure(provider, pause.reason) if pause else None


def provider_failure(
    provider: str, error: ProviderError, pauses: ProviderPauses | None
) -> SearchFailure:
    if error.kind == ProviderErrorKind.QUOTA:
        if pauses is not None:
            pauses.note_error(provider, error)
        return _quota_failure(provider, error.pause_reason)
    return SearchFailure.from_error(error)
