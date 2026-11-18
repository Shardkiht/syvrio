"""Record reversible Vault effects of a model turn in the local Runtime."""

from __future__ import annotations

import hashlib
import json
import re
from base64 import b64decode, b64encode
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath

from ..errors import SyvrioError
from ..infra.paths import SyvrioPaths
from .models import Inference

_active: ContextVar[TurnFileJournal | None] = ContextVar("turn_file_journal", default=None)
TURN_UNDO_DIR = "turn-undo"
_ID = re.compile(r"[A-Za-z0-9_-]+")


class TurnUndoError(SyvrioError, ValueError):
    """A turn's file changes cannot be safely reversed."""


def _bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _digest(payload: bytes | None) -> str | None:
    return None if payload is None else hashlib.sha256(payload).hexdigest()


def _original(current, changes, message: str):
    """Verify consecutive revisions, then find the matching committed state.

    A prepared write may have failed, or a tool's exception handler may have
    already restored an earlier revision. Both leave a known state on disk.
    """
    before, _ = changes[0]
    prior = before
    for left, right in changes:
        if left != prior:
            raise TurnUndoError(message)
        prior = right
    if current != before and not any(current == right for _, right in changes):
        raise TurnUndoError(message)
    return before


class TurnFileJournal:
    def __init__(self, paths: SyvrioPaths, session_id: str, turn_id: str) -> None:
        if not _ID.fullmatch(session_id) or not _ID.fullmatch(turn_id):
            raise TurnUndoError("Invalid turn identity.")
        self.vault_path = paths.vault_dir.absolute()
        self.root = paths.vault_dir.resolve()
        parent = paths.runtime_dir.resolve()
        self.path = parent / TURN_UNDO_DIR / session_id / f"{turn_id}.json"
        if self.path.resolve() != self.path:
            raise TurnUndoError("The local undo journal path is redirected.")
        self.files: dict[str, list[list]] = {}
        self.inferences: dict[str, list[list]] = {}

    @contextmanager
    def capture(self) -> Iterator[None]:
        token = _active.set(self)
        try:
            yield
        finally:
            _active.reset(token)

    def _allowed(self, relative: PurePosixPath) -> bool:
        return (
            not relative.is_absolute()
            and ".." not in relative.parts
            and (
                relative.as_posix() == "records/memory/pending.jsonl"
                or (
                    len(relative.parts) >= 2
                    and relative.parts[0] == "memories"
                    and relative.suffix == ".md"
                )
            )
        )

    def _target(self, relative: str) -> Path:
        name = PurePosixPath(relative)
        if not self._allowed(name):
            raise TurnUndoError("The local undo journal contains an invalid Vault path.")
        path = self.root.joinpath(*name.parts)
        if path.resolve() != path or not path.is_relative_to(self.root):
            raise TurnUndoError("An affected Vault path is redirected.")
        return path

    def record_file(self, path: Path, after: bytes) -> None:
        try:
            relative = path.absolute().relative_to(self.vault_path).as_posix()
        except ValueError:
            return
        if not self._allowed(PurePosixPath(relative)):
            return
        path = self._target(relative)
        before = _bytes(path)
        if before != after:
            change = [None if before is None else b64encode(before).decode("ascii"), _digest(after)]
            self._prepare(self.files, relative, change)

    def record_inferences(self, previous: list[Inference], updates: list[Inference]) -> None:
        old = {item.id: item for item in previous}
        for item in updates:
            self._prepare(self.inferences, item.id, [old[item.id].model_dump(), item.model_dump()])

    def _prepare(self, mapping: dict, key: str, change: list) -> None:
        from .store import atomic_write

        revisions = mapping.setdefault(key, [])
        revisions.append(change)
        try:
            payload = {
                "version": 1,
                "vault": str(self.root),
                "files": self.files,
                "inferences": self.inferences,
            }
            if self.path.resolve() != self.path:
                raise TurnUndoError("The local undo journal path is redirected.")
            atomic_write(self.path, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except Exception:
            revisions.pop()
            if not revisions:
                del mapping[key]
            raise

    def plan(self, current_inferences) -> tuple[dict[Path, bytes | None], list[Inference]]:
        """Preflight every affected file and inference before applying any rollback."""
        if self.path.resolve() != self.path:
            raise TurnUndoError("The local undo journal path is redirected.")
        payload = _bytes(self.path)
        if payload is None:
            return {}, []
        try:
            data = json.loads(payload)
            if data["version"] != 1 or data["vault"] != str(self.root):
                raise ValueError("Incorrect journal version or Vault")
            files: dict[Path, bytes | None] = {}
            for relative, changes in data["files"].items():
                path = self._target(relative)
                originals = [
                    None if old is None else b64decode(old, validate=True) for old, _ in changes
                ]
                current = _digest(_bytes(path))
                initial = _original(
                    current,
                    [
                        (_digest(old), change[1])
                        for old, change in zip(originals, changes, strict=True)
                    ],
                    "An affected Vault file changed after the tool write. Undo left the turn intact.",
                )
                if current != initial:
                    files[path] = originals[0]
            inferences = []
            if data["inferences"]:
                records = {item.id: item.model_dump() for item in current_inferences()}
                for item_id, changes in data["inferences"].items():
                    current = records.get(item_id)
                    before = _original(
                        current,
                        changes,
                        "An affected inference changed after the tool write. Undo left the turn intact.",
                    )
                    item = Inference.model_validate(before)
                    if item.id != item_id:
                        raise ValueError("Incorrect inference ID")
                    if current != before:
                        inferences.append(item)
            return files, inferences
        except TurnUndoError:
            raise
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise TurnUndoError(
                "The local undo journal could not be read. Nothing was undone."
            ) from exc


def capture_file_write(path: Path, after: bytes) -> None:
    if journal := _active.get():
        journal.record_file(path, after)


def capture_inference_updates(previous: list[Inference], updates: list[Inference]) -> None:
    if journal := _active.get():
        journal.record_inferences(previous, updates)
