from __future__ import annotations

import json
from pathlib import Path

from ..infra.paths import vault_paths
from ..time import utc_timestamp


class AuditLog:
    def __init__(self, vault: Path):
        self.path = vault_paths(vault).records_dir / "audit.jsonl"

    def write(self, action: str, **fields) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"at": utc_timestamp(), "action": action, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
