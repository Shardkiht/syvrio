"""提交模型工具写入时，一并记录本回合可撤回的 Vault 文件变更。"""

from __future__ import annotations

import hashlib
import json
import re
from base64 import b64decode, b64encode
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath

from ..errors import SyvrioError
from ..infra.paths import TURN_UNDO_DIR, SyvrioPaths
from ..infra.transaction import vault_transaction
from .schema import parse_document

_active: ContextVar[TurnFileJournal | None] = ContextVar("turn_file_journal", default=None)
_ID = re.compile(r"[A-Za-z0-9_-]+")
_CHANGE_LOG = "records/memory-changes.jsonl"


class TurnUndoError(SyvrioError, ValueError):
    """无法安全撤回该回合的文件变更。"""


def _bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _digest(payload: bytes | None) -> str | None:
    return None if payload is None else hashlib.sha256(payload).hexdigest()


def _fact_records(relative: str, payload: bytes | None) -> dict[str, str]:
    if not relative.startswith("memories/") or relative == "memories/pending.md" or not payload:
        return {}
    doc = parse_document(payload.decode("utf-8"), relative, relative)
    return {r.entry.id: r.entry.raw_markdown.rstrip("\r\n") for r in doc.records}


class TurnFileJournal:
    def __init__(
        self,
        paths: SyvrioPaths,
        session_id: str,
        turn_id: str,
        references: Callable[[set[str], dict[Path, bytes | None]], dict[str, set[str]]],
    ) -> None:
        if not _ID.fullmatch(session_id) or not _ID.fullmatch(turn_id):
            raise TurnUndoError("Invalid turn identity.")
        self.paths = paths
        self.root = paths.vault_dir.resolve()
        self.path = paths.runtime_dir.resolve() / TURN_UNDO_DIR / session_id / f"{turn_id}.json"
        self.references = references

    @contextmanager
    def capture(self) -> Iterator[None]:
        token = _active.set(self)
        try:
            yield
        finally:
            _active.reset(token)

    def _allowed(self, name: PurePosixPath) -> bool:
        return name.as_posix() in {_CHANGE_LOG, "archived/pending/rejected.md"} or (
            name.suffix == ".md"
            and (
                (len(name.parts) >= 2 and name.parts[0] == "memories")
                or (len(name.parts) == 3 and name.parts[0] == "inferences")
            )
        )

    def _target(self, relative: str) -> Path:
        name = PurePosixPath(relative)
        if (
            name.is_absolute()
            or ".." in name.parts
            or "\\" in relative
            or ":" in relative
            or not self._allowed(name)
        ):
            raise TurnUndoError("Invalid Vault path in the undo journal.")
        path = self.root.joinpath(*name.parts)
        if path.resolve() != path or not path.is_relative_to(self.root):
            raise TurnUndoError("An affected Vault path is redirected.")
        return path

    def _load(self) -> dict:
        if self.path.resolve() != self.path:
            raise TurnUndoError("The undo journal path is redirected.")
        payload = _bytes(self.path)
        if payload is None:
            return {"version": 3, "vault": str(self.root), "files": {}, "references": {}}
        try:
            data = json.loads(payload)
            if (
                data["version"] != 3
                or data["vault"] != str(self.root)
                or not isinstance(data["files"], dict)
                or not isinstance(data["references"], dict)
            ):
                raise ValueError
            files = data["files"]
            if "archived/pending.md" in files:
                if "archived/pending/rejected.md" in files:
                    raise ValueError
                files["archived/pending/rejected.md"] = files.pop("archived/pending.md")
            return data
        except (KeyError, TypeError, ValueError) as exc:
            raise TurnUndoError("The undo journal could not be read.") from exc

    def has_effects(self) -> bool:
        with vault_transaction(self.paths):
            return bool(self._load()["files"])

    def record_file(self, path: Path, after: bytes) -> None:
        if not path.absolute().is_relative_to(self.root):
            return
        relative = path.absolute().relative_to(self.root).as_posix()
        if not self._allowed(PurePosixPath(relative)):
            return
        path = self._target(relative)
        with vault_transaction(self.paths) as tx:
            before = _bytes(path)
            if before == after:
                return
            data = self._load()
            files = data["files"]
            earlier = files.get(relative)
            if earlier and _digest(before) != earlier["after"]:
                # 等待模型时，另一回合写入了同一文件。保留文件现状，将本回合标记为不可撤回。
                data["conflict"] = True
            references = data["references"]
            old, new = _fact_records(relative, before), _fact_records(relative, after)
            touched = {
                key
                for key in old.keys() | new.keys()
                if old.get(key) != new.get(key) and key not in references
            }
            if touched:
                baseline = self.references(touched, {})
                references.update({key: sorted(baseline[key]) for key in touched})
            if earlier:
                revision = earlier
            elif relative == _CHANGE_LOG:
                # 权威事件日志只追加记录，校验并恢复原有前缀即可，无需在每回合复制全部历史。
                revision = {"prefix_size": len(before or b""), "prefix_hash": _digest(before)}
            else:
                revision = {
                    "original": None if before is None else b64encode(before).decode("ascii")
                }
            revision["after"] = _digest(after)
            files[relative] = revision
            tx.write(self.path, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def plan(self) -> dict[Path, bytes | None]:
        """撤回前检查每个文件和恢复后仍会保留的所有外部引用。"""
        with vault_transaction(self.paths):
            data = self._load()
            if data.get("conflict"):
                raise TurnUndoError("Another turn modified an affected Vault file.")
            files = {}
            touched, removed = set(), set()
            try:
                for relative, revision in data["files"].items():
                    path = self._target(relative)
                    current = _bytes(path)
                    if _digest(current) != revision["after"]:
                        raise TurnUndoError(
                            "An affected Vault file changed after the tool write. Undo left the turn intact."
                        )
                    if relative == _CHANGE_LOG:
                        size, expected = revision["prefix_size"], revision["prefix_hash"]
                        if type(size) is not int or not 0 <= size <= len(current or b""):
                            raise ValueError("Invalid log prefix size.")
                        original = None if expected is None else (current or b"")[:size]
                        if _digest(original) != expected or (original is None and size != 0):
                            raise ValueError("The original change-log prefix does not match.")
                    else:
                        b64 = revision["original"]
                        original = None if b64 is None else b64decode(b64, validate=True)
                    if current == original:
                        continue
                    files[path] = original
                    live, restored = (
                        _fact_records(relative, current),
                        _fact_records(relative, original),
                    )
                    removed.update(live.keys() - restored.keys())
                    touched.update(
                        key
                        for key in live.keys() | restored.keys()
                        if live.get(key) != restored.get(key)
                    )
                if touched:
                    remaining = self.references(touched, files)
                    for key in touched:
                        allowed = set() if key in removed else set(data["references"][key])
                        if remaining[key] - allowed:
                            raise TurnUndoError(
                                "Another record now references an affected memory. Undo left the turn intact."
                            )
            except TurnUndoError:
                raise
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise TurnUndoError("The undo journal could not be read.") from exc
            return files


def capture_file_write(path: Path, after: bytes) -> None:
    if journal := _active.get():
        journal.record_file(path, after)
