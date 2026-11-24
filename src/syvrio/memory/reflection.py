"""读取最近发布的画像，生成工作由 Dream 负责。"""

from __future__ import annotations

from dataclasses import dataclass

from ..infra.jobs import job_path, read_job
from ..infra.locking import vault_write_lock
from ..infra.paths import SyvrioPaths


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

    def read_text(self) -> str:
        with vault_write_lock(self.paths):
            if self.path.is_symlink():
                raise ValueError("Profile cannot redirect outside the Vault.")
            try:
                return self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return ""

    def profile_summary(self) -> ProfileSummaryView | None:
        with vault_write_lock(self.paths):
            text = self.read_text().strip()
            if not text:
                return None
            metadata = read_job(job_path(self.paths, "dream", "profile")) or {}
            return ProfileSummaryView(
                text=text,
                generated_at=metadata.get("generated_at", ""),
                fact_count=metadata.get("fact_count", 0),
                unique_count=metadata.get("unique_count", 0),
            )
