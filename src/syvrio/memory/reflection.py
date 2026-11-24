"""读取最近发布的画像，生成工作由 Dream 负责。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..infra.storage.locking import vault_write_lock
from ..infra.storage.paths import SyvrioPaths
from .background.jobs import job_path, read_job


@dataclass(frozen=True)
class ProfileSummaryView:
    text: str
    generated_at: str
    fact_count: int
    unique_count: int


class ProfileReader:
    def __init__(self, paths: SyvrioPaths):
        self.paths = paths
        self.path = paths.vault_dir / "profile.md"

    def _read_published[T](self, read: Callable[[], T]) -> T:
        lock = vault_write_lock(self.paths)
        if not lock.path.exists():
            result = read()
            # 首个写者会先创建锁文件。若它在读取期间出现，在锁内重读已提交的画像。
            if not lock.path.exists():
                return result
        with lock:
            return read()

    def _read_text(self) -> str:
        if self.path.is_symlink():
            raise ValueError("Profile cannot redirect outside the Vault.")
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def read_text(self) -> str:
        return self._read_published(self._read_text)

    def profile_summary(self) -> ProfileSummaryView | None:
        return self._read_published(self._summary)

    def _summary(self) -> ProfileSummaryView | None:
        text = self._read_text().strip()
        if not text:
            return None
        metadata = read_job(job_path(self.paths, "dream", "profile")) or {}
        return ProfileSummaryView(
            text=text,
            generated_at=metadata.get("generated_at", ""),
            fact_count=metadata.get("fact_count", 0),
            unique_count=metadata.get("unique_count", 0),
        )
