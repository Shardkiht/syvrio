"""Vault 写入与 Git 操作的跨进程锁。

同一进程内通过注册表共享同一个重入锁，避免同一文件被重复加锁；
跨进程使用操作系统文件锁（POSIX flock / Windows msvcrt），进程异常退出时
由内核释放，不会留下需要手工清理的锁文件。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .paths import SyvrioPaths, vault_paths

if os.name == "nt":
    import msvcrt
else:
    import fcntl

POLL_INTERVAL = 0.05
DEFAULT_TIMEOUT = 30.0


class LockTimeout(TimeoutError):
    """在超时时间内无法取得锁。"""


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    @staticmethod
    def _busy(error: OSError) -> bool:
        if isinstance(error, BlockingIOError):
            return True
        if os.name == "nt":
            return getattr(error, "winerror", None) in (33, 36) or error.errno in (13, 36)
        return error.errno in (11, 13)

    def _lock(self, fd: int) -> None:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def acquire(self, timeout: float | None = DEFAULT_TIMEOUT) -> None:
        deadline = None if timeout is None or timeout < 0 else time.monotonic() + timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.name == "nt" and os.fstat(fd).st_size == 0:
            # Windows byte-range locks need a byte to lock; zero-length is otherwise racy.
            os.write(fd, b"\0")
        while True:
            try:
                self._lock(fd)
                self._fd = fd
                return
            except OSError as exc:
                if not self._busy(exc):
                    os.close(fd)
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockTimeout(f"锁被其他进程占用：{self.path.name}") from exc
                time.sleep(POLL_INTERVAL)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class VaultLock:
    """进程内重入、跨进程互斥的锁；可作为上下文管理器使用。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._local = threading.RLock()
        self._file = _FileLock(self._path)
        self._depth = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def locked(self) -> bool:
        return self._depth > 0

    def acquire(self, timeout: float | None = DEFAULT_TIMEOUT) -> bool:
        started = time.monotonic()
        if timeout is None or timeout < 0:
            acquired = self._local.acquire()
        else:
            acquired = self._local.acquire(timeout=max(0.0, timeout))
        if not acquired:
            raise LockTimeout(f"锁被同进程其他线程占用：{self._path.name}")
        try:
            if self._depth == 0:
                remaining = None
                if timeout is not None and timeout >= 0:
                    remaining = max(0.0, timeout - (time.monotonic() - started))
                self._file.acquire(remaining)
            self._depth += 1
            return True
        except Exception:
            self._local.release()
            raise

    def release(self) -> None:
        if self._depth <= 0:
            return
        self._depth -= 1
        if self._depth == 0:
            self._file.release()
        self._local.release()

    def __enter__(self) -> "VaultLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


_REGISTRY: dict[str, VaultLock] = {}
_REGISTRY_LOCK = threading.Lock()


def _shared(path: Path) -> VaultLock:
    key = os.path.normcase(os.path.abspath(path))
    with _REGISTRY_LOCK:
        lock = _REGISTRY.get(key)
        if lock is None:
            lock = VaultLock(path)
            _REGISTRY[key] = lock
        return lock


def _lock_path(vault: Path | SyvrioPaths, name: str) -> Path:
    paths = vault if isinstance(vault, SyvrioPaths) else vault_paths(vault)
    paths.validate()
    return paths.lock_dir / name


def vault_write_lock(vault: Path | SyvrioPaths) -> VaultLock:
    """保护记忆文件、会话追加与 Git 快照阶段。"""
    return _shared(_lock_path(vault, "vault.lock"))


def git_operation_lock(vault: Path | SyvrioPaths) -> VaultLock:
    """保护 Stage/Commit/Push 等 Git 操作之间的互斥。"""
    return _shared(_lock_path(vault, "git.lock"))


def client_instance_lock(vault: Path | SyvrioPaths) -> VaultLock:
    """Single-client marker: only one TUI or Web process may write the Vault.

    Unlike vault.lock this is held for the whole client lifetime rather than a
    single write. The OS releases it when the process exits, so a crash cannot
    leave a stale lock behind.
    """
    return _shared(_lock_path(vault, "client.lock"))
