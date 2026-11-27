from __future__ import annotations

from pathlib import Path

from ..infra.storage.audit import append_audit
from ..infra.storage.database import transaction
from ..infra.storage.paths import SyvrioPaths, vault_paths


class AuditLog:
    def __init__(self, vault: Path | SyvrioPaths):
        self.paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)

    def write(self, action: str, **fields) -> None:
        with transaction(self.paths) as db:
            append_audit(db, action, fields)
