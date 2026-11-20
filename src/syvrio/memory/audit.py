from __future__ import annotations

import json
from pathlib import Path

from ..infra.paths import SyvrioPaths, vault_paths
from ..infra.transaction import vault_transaction
from ..time import utc_timestamp


class AuditLog:
    def __init__(self, vault: Path | SyvrioPaths):
        self.paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
        self.path = self.paths.records_dir / "audit.jsonl"

    def write(self, action: str, **fields) -> None:
        record = {"at": utc_timestamp(), "action": action, **fields}
        with vault_transaction(self.paths) as tx:
            tx.append(self.path, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
