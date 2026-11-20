"""Crash-recoverable groups of local Vault and background-job writes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from base64 import b64decode, b64encode
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import get_ident, local

from ..errors import SyvrioError
from .files import atomic_write, safe_component, sync_directory
from .locking import vault_write_lock
from .paths import TURN_UNDO_DIR, SyvrioPaths


class TransactionError(SyvrioError, ValueError):
    pass


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _owner():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return get_ident(), task


class VaultTransaction:
    """Write through under the Vault lock. Removing the journal commits the entire group."""

    def __init__(self, paths: SyvrioPaths) -> None:
        self.owner = _owner()
        self.closed = False
        self.vault = paths.vault_dir.resolve()
        self.jobs = paths.jobs_dir.resolve()
        self.roots = {
            "vault": self.vault,
            "jobs": self.jobs,
            "undo": paths.runtime_dir.resolve() / TURN_UNDO_DIR,
        }
        identity = hashlib.sha256(os.fsencode(os.path.normcase(str(self.vault)))).hexdigest()
        self.journal = self.jobs / "vault-transactions" / (identity + ".json")
        self.files: dict[str, dict] = {}
        self.failed = False

    def _target(self, key: str) -> Path:
        root_name, sep, name = key.partition("/")
        if not sep or root_name not in self.roots:
            raise TransactionError("Invalid path in Vault transaction.")
        root = self.roots[root_name]
        rel = Path(name)
        if (
            not rel.parts
            or rel.is_absolute()
            or rel.drive
            or ".." in rel.parts
            or any(not safe_component(p) for p in rel.parts)
            or (root_name == "jobs" and rel.parts[0].casefold() == "vault-transactions")
            or (root_name == "undo" and (len(rel.parts) != 2 or rel.suffix != ".json"))
        ):
            raise TransactionError("Invalid path in Vault transaction.")
        path = root / rel
        if path.resolve() != path or not path.is_relative_to(root):
            raise TransactionError("Redirected path in Vault transaction.")
        return path

    def _key(self, target: Path) -> str:
        target = target.absolute()
        for name, root in self.roots.items():
            if target.is_relative_to(root):
                key = f"{name}/{target.relative_to(root).as_posix()}"
                if self._target(key) == target:
                    return key
        raise TransactionError("The transaction target is outside the Vault and Runtime jobs.")

    def _save(self) -> None:
        if self.journal.resolve() != self.journal:
            raise TransactionError("Redirected Vault transaction journal.")
        data = {"version": 1, "vault": str(self.vault), "files": self.files}
        atomic_write(self.journal, json.dumps(data, separators=(",", ":")).encode())

    def _forget(self) -> None:
        if self.files:
            self.journal.unlink(missing_ok=True)
            sync_directory(self.journal.parent)
            self.files.clear()

    def write(self, path: Path, payload: bytes | None) -> None:
        if self.closed or self.owner != _owner():
            raise TransactionError(
                "This Vault transaction is no longer active in the current task."
            )
        key = self._key(path)
        current = _read(path)
        if current == payload:
            return
        before = _digest(current)
        if key not in self.files:
            self.files[key] = {
                "original": b64encode(current).decode("ascii") if current is not None else None,
                "before": before,
            }
        record = self.files[key]
        # The process can die on either side of the rename. Accept only those two
        # revisions or the original (if an earlier recovery already restored it).
        record.update(previous=before, after=_digest(payload))
        self._save()
        if payload is None:
            path.unlink()
            sync_directory(path.parent)
        else:
            atomic_write(path, payload)

    def append(self, path: Path, payload: bytes) -> None:
        self._key(path)
        self.write(path, (_read(path) or b"") + payload)

    def recover(self) -> None:
        if self.journal.resolve() != self.journal:
            raise TransactionError("Redirected Vault transaction journal.")
        saved = _read(self.journal)
        if saved is None:
            return
        try:
            manifest = json.loads(saved)
            if manifest["version"] != 1 or manifest["vault"] != str(self.vault):
                raise ValueError
            self.files = manifest["files"]
            if not isinstance(self.files, dict) or not self.files:
                raise ValueError
            restore = []
            for key, record in self.files.items():
                path = self._target(key)
                old = record["original"]
                old = b64decode(old, validate=True) if old is not None else None
                if _digest(old) != record["before"]:
                    raise ValueError
                now = _digest(_read(path))
                if now not in {record["before"], record["previous"], record["after"]}:
                    raise TransactionError(
                        "A file changed outside the unfinished Vault transaction."
                    )
                if now != record["before"]:
                    restore.append((path, old))
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, TransactionError):
                raise
            raise TransactionError("Invalid Vault transaction journal.") from exc
        for path, payload in reversed(restore):
            if payload is None:
                path.unlink(missing_ok=True)
                sync_directory(path.parent)
            else:
                atomic_write(path, payload)
        self._forget()


_thread_state = local()
_current: ContextVar[VaultTransaction | None] = ContextVar("vault_transaction", default=None)


@contextmanager
def vault_transaction(paths: SyvrioPaths) -> Iterator[VaultTransaction]:
    parent = _current.get()
    if parent is not None and not parent.closed:
        if parent.owner != _owner():
            raise TransactionError(
                "Cannot share a running Vault transaction between tasks or threads."
            )
        if parent.vault != paths.vault_dir.absolute() or parent.jobs != paths.jobs_dir.absolute():
            raise TransactionError("Cannot nest a transaction for another Vault or Runtime.")
        try:
            yield parent
        except BaseException:
            parent.failed = True
            raise
        return
    if getattr(_thread_state, "transaction", None) is not None:
        raise TransactionError("Cannot interleave independent transactions on the same thread.")
    with vault_write_lock(paths):
        tx = VaultTransaction(paths)
        tx.recover()
        token = _current.set(tx)
        _thread_state.transaction = tx
        try:
            yield tx
            if tx.failed:
                raise TransactionError("A nested Vault operation failed.")
            tx._forget()
        except BaseException as failure:
            try:
                tx.recover()
            except BaseException as recovery:
                raise BaseExceptionGroup(
                    "Vault write and recovery both failed", [failure, recovery]
                ) from failure
            raise
        finally:
            tx.closed = True
            del _thread_state.transaction
            _current.reset(token)


def recover_vault_transaction(paths: SyvrioPaths) -> None:
    with vault_transaction(paths):
        pass
