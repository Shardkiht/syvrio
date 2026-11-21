"""Upgrade the old archive layout under the Vault and Git locks."""

from __future__ import annotations

from pathlib import Path

from ..errors import SyvrioError
from .files import sync_directory


class ArchiveLayoutError(SyvrioError, ValueError):
    pass


def migrate_archive(vault: Path) -> None:
    legacy = vault / "archive"
    archived = vault / "archived"
    old_pending = archived / "pending.md"
    if archived.is_symlink():
        raise ArchiveLayoutError("Replace the archived directory link before opening the Vault.")
    if not any(p.exists() or p.is_symlink() for p in (legacy, old_pending)):
        return
    if legacy.is_symlink() or (legacy.exists() and not legacy.is_dir()):
        raise ArchiveLayoutError("Replace the legacy archive with a Vault directory.")
    moves: dict[Path, Path] = {}
    directories: list[Path] = []
    if old_pending.exists() or old_pending.is_symlink():
        if old_pending.is_symlink() or not old_pending.is_file():
            raise ArchiveLayoutError("Resolve the legacy rejected-candidate file before migrating.")
        moves[archived / "pending" / "rejected.md"] = old_pending
    for source in sorted(legacy.rglob("*")):
        rel = source.relative_to(legacy)
        if source.is_symlink() or not (source.is_dir() or source.is_file()):
            raise ArchiveLayoutError(
                f"Resolve the unsupported archive entry before migrating: {rel}"
            )
        if source.is_dir():
            directories.append(source)
            continue
        target = archived / rel if rel.parts[0] == "sessions" else archived / "other" / rel
        if target in moves:
            raise ArchiveLayoutError(
                f"Resolve the archive filename conflict before migrating: {target}"
            )
        moves[target] = source
    # Check every destination before moving the first file. Renames are retryable.
    for target in moves:
        if (
            target.exists()
            or target.resolve() != target
            or any(
                parent in moves or (parent.exists() and not parent.is_dir())
                for parent in target.parents
                if parent.is_relative_to(archived)
            )
        ):
            raise ArchiveLayoutError(
                f"Resolve the archive filename conflict before migrating: {target}"
            )
    for target, source in moves.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        sync_directory(target.parent)
        sync_directory(source.parent)
    for directory in reversed(directories):
        directory.rmdir()
    if legacy.exists():
        legacy.rmdir()
    sync_directory(vault)
