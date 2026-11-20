"""Process ownership independent of the chosen Runtime and Vault path aliases."""

from __future__ import annotations

import hashlib
import os

from ..infra.locking import VaultLock, client_instance_lock
from ..infra.paths import syvrio_home


class BackendLease:
    def __init__(self, paths):
        self.paths = paths
        self.runtime_lock = client_instance_lock(paths)
        self.vault_lock: VaultLock | None = None

    def acquire(self, timeout=0):
        vault = self.paths.vault_dir.resolve()
        vault.mkdir(parents=True, exist_ok=True)
        stat = vault.stat()
        # A renamed directory, junction, or symlink identifies the same Vault.
        identity = f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else os.path.normcase(str(vault))
        key = hashlib.sha256(os.fsencode(identity)).hexdigest()
        self.vault_lock = VaultLock(syvrio_home() / "owners" / (key + ".lock"))
        self.vault_lock.acquire(timeout)
        try:
            self.runtime_lock.acquire(timeout)
        except BaseException:
            self.vault_lock.release()
            self.vault_lock = None
            raise

    def release(self):
        try:
            self.runtime_lock.release()
        finally:
            if self.vault_lock:
                self.vault_lock.release()
                self.vault_lock = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
