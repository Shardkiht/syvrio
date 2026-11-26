"""提供跨存储操作的一致读视图与原子写入边界。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from .database import transaction
from .paths import SyvrioPaths


@dataclass(frozen=True)
class VaultWork:
    paths: SyvrioPaths

    @contextmanager
    def read(self) -> Iterator[None]:
        with transaction(self.paths, write=False):
            yield

    @contextmanager
    def write(self) -> Iterator[None]:
        with transaction(self.paths):
            yield
