"""受管更新的进度通知契约。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class UpdateProgress:
    stage: str
    message: str
    downloaded: int | None = None
    total: int | None = None


ProgressCallback = Callable[[UpdateProgress], None]
