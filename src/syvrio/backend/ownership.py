"""按物理 Vault 判定进程所有权，兼容路径别名及不同的 Runtime。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..infra.storage.locking import VaultLock, client_instance_lock
from ..infra.storage.paths import syvrio_home


def physical_vault_lock(vault: Path) -> VaultLock:
    """按物理目录标识复用后端和离线核验的独占权。"""
    vault = vault.resolve()
    stat = vault.stat()
    # 指向同一 Vault 的重命名目录、目录联接和符号链接共用同一标识。
    identity = f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else os.path.normcase(str(vault))
    key = hashlib.sha256(os.fsencode(identity)).hexdigest()
    return VaultLock(syvrio_home() / "owners" / (key + ".lock"))


class BackendLease:
    def __init__(self, paths):
        self.paths = paths
        self.runtime_lock = client_instance_lock(paths)
        self.vault_lock: VaultLock | None = None

    def acquire(self, timeout=0):
        self.paths.vault_dir.mkdir(parents=True, exist_ok=True)
        self.vault_lock = physical_vault_lock(self.paths.vault_dir)
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
