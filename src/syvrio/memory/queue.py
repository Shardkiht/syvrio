from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..infra.paths import vault_paths
from ..time import utc_timestamp
from .models import PendingItem


@contextmanager
def rollback_files(paths: list[Path]) -> Iterator[None]:
    """Compensate failed local writes; caller must serialize writers.

    This is exception recovery, not a durable or cross-process transaction.
    Only the explicitly affected files are read, and original bytes are kept.
    """
    snapshots = {path: path.read_bytes() if path.exists() else None for path in paths}
    try:
        yield
    except Exception as failure:
        errors: list[Exception] = []
        for path, original in reversed(list(snapshots.items())):
            temporary: Path | None = None
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                elif not path.exists() or path.read_bytes() != original:
                    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                        temporary = Path(handle.name)
                        handle.write(original)
                    os.replace(temporary, path)
            except Exception as error:
                errors.append(error)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        if errors:
            raise ExceptionGroup(
                "Write failed and rollback was incomplete", [failure, *errors]
            ) from failure
        raise


class PendingQueue:
    def __init__(self, vault: Path):
        self.vault = Path(vault)
        self.dir = vault_paths(self.vault).records_dir
        self.pending_path = self.dir / "pending.jsonl"
        self.resolved_path = self.dir / "resolved.jsonl"

    def _load(self) -> list[PendingItem]:
        if not self.pending_path.exists():
            return []
        items: list[PendingItem] = []
        for line in self.pending_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                items.append(PendingItem.model_validate_json(line))
        return items

    def _save(self, items: list[PendingItem]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        content = "\n".join(item.model_dump_json() for item in items)
        temporary: Path | None = None
        try:
            # 使用独立临时文件，只在完整写入并关闭后替换 pending。
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.dir,
                prefix=f".{self.pending_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(content + ("\n" if content else ""))
            os.replace(temporary, self.pending_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def all(self) -> list[PendingItem]:
        return self._load()

    def get(self, item_id: str) -> PendingItem | None:
        return next((item for item in self._load() if item.id == item_id), None)

    def add(self, item: PendingItem) -> None:
        items = self._load()
        items.append(item)
        self._save(items)

    def resolve(self, item_id: str, status: str) -> PendingItem | None:
        items = self._load()
        target = next((item for item in items if item.id == item_id), None)
        if target is None:
            return None
        with rollback_files([self.pending_path, self.resolved_path]):
            self.dir.mkdir(parents=True, exist_ok=True)
            record = target.model_dump()
            record.update(status=status, resolved_at=utc_timestamp())
            with self.resolved_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._save([item for item in items if item is not target])
        return target
