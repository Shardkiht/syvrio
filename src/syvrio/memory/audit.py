from __future__ import annotations

from pathlib import Path

from ..infra.storage.database import encode, transaction
from ..infra.storage.paths import SyvrioPaths, vault_paths
from ..infra.time import utc_timestamp


class AuditLog:
    def __init__(self, vault: Path | SyvrioPaths):
        self.paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)

    def write(self, action: str, **fields) -> None:
        with transaction(self.paths) as db:
            db.execute(
                "INSERT INTO audit(at,action,details) VALUES(?,?,?)",
                (utc_timestamp(), action, encode(fields)),
            )
