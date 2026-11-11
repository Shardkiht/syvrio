#!/usr/bin/env python3
"""Normalize known Syvrio timestamps while preserving historical calendar fields."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from syvrio.infra.locking import client_instance_lock, git_operation_lock, vault_write_lock
from syvrio.infra.paths import SyvrioPaths
from syvrio.memory.inference import evidence_v2, source_fingerprint
from syvrio.memory.schema import _span_code, parse_document
from syvrio.memory.store import atomic_write
from syvrio.time import parse_timestamp, serialize_timestamp

RECORD_FIELDS = {
    "pending.jsonl": ("created_at",),
    "resolved.jsonl": ("created_at", "resolved_at"),
    "audit.jsonl": ("at",),
    "inferences.jsonl": ("created_at", "updated_at"),
}
RUNTIME_FIELDS = {
    "state/git-sync.json": ("last_commit_at", "last_push_at", "last_sync_at", "retry_after"),
    "state/reflection.json": (
        "last_checked_at",
        "profile.last_attempt_at",
        "profile.last_success_at",
        "profile.next_retry_at",
        "dream.last_attempt_at",
        "dream.last_success_at",
        "dream.next_retry_at",
    ),
    "cache/profile/profile-summary.json": ("generated_at",),
    "jobs/dreaming/jobs.jsonl": ("at",),
    "state/fingerprints.json": (),
}


@dataclass(frozen=True)
class Target:
    scope: str
    root: Path
    path: Path
    fields: tuple[str, ...] = ()

    @property
    def relative(self) -> Path:
        return self.path.relative_to(self.root)


@dataclass(frozen=True)
class Change:
    target: Target
    before: bytes
    after: bytes
    fields: int


def normalize(value: str, source_zone: ZoneInfo | None) -> str:
    if not isinstance(value, str):
        raise ValueError("expected an ISO 8601 string")
    try:
        wall = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid ISO 8601 timestamp") from None
    if wall.utcoffset() is not None:
        return serialize_timestamp(parse_timestamp(value))
    if len(value) == 10:
        raise ValueError("absolute timestamp contains only a calendar date")
    if source_zone is None:
        raise ValueError("missing source timezone for timestamp without an offset")

    instants = set()
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=source_zone, fold=fold)
        instant = candidate.astimezone(UTC)
        if instant.astimezone(source_zone).replace(tzinfo=None) == wall:
            instants.add(instant)
    if not instants:
        raise ValueError("nonexistent local time in source timezone")
    if len(instants) != 1:
        raise ValueError("ambiguous local time in source timezone")
    return serialize_timestamp(instants.pop())


def unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def migrate_fields(
    obj: dict, fields: tuple[str, ...], source_zone: ZoneInfo | None, location: str
) -> int:
    changed = 0
    for field in fields:
        owner = obj
        *parents, key = field.split(".")
        for parent in parents:
            if parent not in owner:
                owner = {}
                break
            owner = owner[parent]
            if not isinstance(owner, dict):
                raise ValueError(f"{location} {parent}: expected a JSON object")
        old = owner.get(key)
        if old is None or old == "":
            continue
        try:
            value = normalize(old, source_zone)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"{location} {field}: {exc}") from None
        if old != value:
            owner[key] = value
            changed += 1
    return changed


def update_fingerprints(obj: dict, key: str, evidence: dict[str, tuple[str, str]]) -> int:
    values = obj.get(key)
    if values is None:
        return 0
    if not isinstance(values, dict):
        raise ValueError(f"{key}: expected a JSON object")
    changed = 0
    for source, current in values.items():
        if source in evidence and current == evidence[source][0]:
            values[source] = evidence[source][1]
            changed += 1
    return changed


def migrate_object(
    line: str,
    target: Target,
    source_zone: ZoneInfo | None,
    location: str,
    evidence: dict[str, tuple[str, str]],
) -> tuple[str, int]:
    try:
        obj = json.loads(line, object_pairs_hook=unique_object)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"{location}: invalid JSON object ({type(exc).__name__})") from None
    if not isinstance(obj, dict):
        raise ValueError(f"{location}: expected a JSON object")

    changed = 0
    if target.scope == "vault" and target.path.name == "inferences.jsonl":
        snapshots = obj.get("records") if obj.get("type") == "revision" else [obj]
        if not isinstance(snapshots, list) or not snapshots:
            raise ValueError(f"{location} records: expected inference snapshots")
        for index, snapshot in enumerate(snapshots):
            label = location if snapshots[0] is obj else f"{location} records[{index}]"
            if not isinstance(snapshot, dict):
                raise ValueError(f"{label}: expected an inference snapshot")
            changed += migrate_fields(snapshot, target.fields, source_zone, label)
            changed += update_fingerprints(snapshot, "source_fingerprints", evidence)
    else:
        changed = migrate_fields(obj, target.fields, source_zone, location)
        if target.scope == "runtime" and target.relative.as_posix() == "state/fingerprints.json":
            changed += update_fingerprints(obj, "fingerprints", evidence)

    if not changed:
        return line, 0
    if line.endswith("\r\n"):
        newline = "\r\n"
    elif line.endswith("\n"):
        newline = "\n"
    else:
        newline = ""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + newline, changed


def replace_spans(raw: str, replacements: list[tuple[int, int, str]]) -> str:
    for start, end, value in reversed(replacements):
        raw = raw[:start] + value + raw[end:]
    return raw


def migrate_markdown(
    raw: str, location: str, source_zone: ZoneInfo | None, evidence: dict[str, tuple[str, str]]
) -> tuple[str, int]:
    before = parse_document(raw, location, location).records
    replacements = []
    for record in before:
        old = record.entry.timestamp
        span = record.migration_spans.get("syvrio.recorded_at")
        if span is None:
            continue
        try:
            if len(old) == 10:
                # Some v2 imports retain recorded dates at day precision.
                date.fromisoformat(old)
                continue
            value = normalize(old, source_zone)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"{location}:{record.entry.line} syvrio.recorded_at: {exc}") from None
        if old != value:
            replacements.append((*span, _span_code(value)))
    if not replacements:
        return raw, 0

    updated = replace_spans(raw, replacements)
    markers = []
    for old, new in zip(before, parse_document(updated, location, location).records, strict=True):
        if old.entry.timestamp == new.entry.timestamp:
            continue
        previous = source_fingerprint(old.entry)
        digest = evidence_v2(new.entry)
        span = new.migration_spans.get("migration.evidence_v2")
        if span and previous != evidence_v2(old.entry):
            # Keep an already-valid Vault v2 evidence alias valid after rewriting its timestamp.
            markers.append((*span, _span_code(digest)))
        else:
            if new.entry.id in evidence:
                raise ValueError(f"{location}:{new.entry.line}: duplicate migrated memory ID")
            evidence[new.entry.id] = (previous, digest)
    return replace_spans(updated, markers), len(replacements) + len(markers)


def targets(args: argparse.Namespace) -> Iterator[Target]:
    if args.vault:
        vault = args.vault
        for path in sorted((vault / "memories").rglob("*.md")):
            yield Target("vault", vault, path)
        for name, fields in RECORD_FIELDS.items():
            yield Target("vault", vault, vault / "records" / "memory" / name, fields)
    sessions = args.sessions or (args.vault / "sessions" if args.vault else None)
    if sessions:
        for path in sorted(sessions.rglob("*.jsonl")):
            yield Target("sessions", sessions, path, ("started_at", "at"))
    if args.runtime:
        runtime = args.runtime
        for name, fields in RUNTIME_FIELDS.items():
            yield Target("runtime", runtime, runtime / name, fields)


def inspect_target(
    target: Target, zone: ZoneInfo | None, evidence: dict[str, tuple[str, str]]
) -> Change | None:
    path = target.path
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError(f"{target.scope}/{target.relative}: symbolic links are unsupported")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"{target.scope}/{target.relative}: expected a regular file")
    old = path.read_bytes()
    raw = old.decode("utf-8")
    label = f"{target.scope}/{target.relative}"
    if path.suffix == ".md":
        updated, changed = migrate_markdown(raw, label, zone, evidence)
    elif path.suffix == ".jsonl":
        rows = []
        changed = 0
        for number, line in enumerate(old.splitlines(keepends=True), 1):
            line = line.decode("utf-8")
            if line.strip():
                line, count = migrate_object(line, target, zone, f"{label}:{number}", evidence)
                changed += count
            rows.append(line)
        updated = "".join(rows)
    else:
        updated, changed = migrate_object(raw, target, zone, label, evidence)
    return Change(target, old, updated.encode("utf-8"), changed) if changed else None


def apply(changes: list[Change], backup: Path) -> None:
    for change in changes:
        if change.target.path.read_bytes() != change.before:
            raise ValueError(f"{change.target.scope}/{change.target.relative}: changed during scan")
    backup.mkdir(mode=0o700, parents=True)
    for change in changes:
        atomic_write(backup / change.target.scope / change.target.relative, change.before)
    applied: list[Change] = []
    try:
        for change in changes:
            atomic_write(change.target.path, change.after)
            applied.append(change)
    except BaseException:
        for change in reversed(applied):
            atomic_write(change.target.path, change.before)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview only (default)")
    mode.add_argument("--apply", action="store_true", help="rewrite with backups")
    for scope in ("vault", "sessions", "runtime"):
        parser.add_argument(f"--{scope}", type=Path)
        parser.add_argument(f"--{scope}-source-timezone")
    parser.add_argument("--source-timezone", help="IANA timezone for records without an offset")
    parser.add_argument(
        "--timezone-for",
        action="append",
        default=[],
        metavar="SCOPE/PATH=IANA",
        help="override the source timezone for a file or directory",
    )
    parser.add_argument("--backup-dir", type=Path, help="new directory outside the target roots")
    args = parser.parse_args(argv)
    roots = [path for path in (args.vault, args.sessions, args.runtime) if path is not None]
    if not roots:
        parser.error("specify at least one of --vault, --sessions, --runtime")
    for root in roots:
        if not root.is_dir() or any(p.is_symlink() for p in (root, *root.absolute().parents)):
            parser.error(f"expected a real directory: {root}")
    if args.apply:
        if args.backup_dir is None:
            parser.error("--apply requires --backup-dir")
        backup = args.backup_dir.resolve()
        if backup.exists() or any(backup.is_relative_to(root.resolve()) for root in roots):
            parser.error("backup directory must be new and outside target roots")
    zones = {}
    for scope in ("vault", "sessions", "runtime"):
        name = getattr(args, scope + "_source_timezone") or args.source_timezone
        try:
            zones[scope] = ZoneInfo(name) if name else None
        except (ZoneInfoNotFoundError, ValueError):
            parser.error(f"unknown IANA timezone: {name}")
    overrides: dict[PurePosixPath, ZoneInfo] = {}
    for spec in args.timezone_for:
        prefix, separator, name = spec.partition("=")
        path = PurePosixPath(prefix)
        if (
            not separator
            or len(path.parts) < 2
            or path.parts[0] not in zones
            or ".." in path.parts
            or path in overrides
        ):
            parser.error(f"invalid or duplicate --timezone-for target: {prefix}")
        try:
            overrides[path] = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            parser.error(f"unknown IANA timezone: {name}")
    try:
        with ExitStack() as stack:
            if args.apply and args.vault and args.runtime:
                paths = SyvrioPaths(args.vault, args.runtime)
                for lock in (client_instance_lock, git_operation_lock, vault_write_lock):
                    stack.enter_context(lock(paths))
            changes = []
            errors = []
            scanned = 0
            evidence: dict[str, tuple[str, str]] = {}
            matched = set()
            seen = set()
            for target in targets(args):
                label = PurePosixPath(target.scope, target.relative.as_posix())
                if target.path.is_file():
                    resolved = target.path.resolve()
                    if resolved in seen:
                        errors.append(f"{label}: path appears in more than one target scope")
                        continue
                    seen.add(resolved)
                    scanned += 1
                prefixes = [prefix for prefix in overrides if label.is_relative_to(prefix)]
                if target.path.is_file():
                    matched.update(prefixes)
                zone = zones[target.scope]
                if prefixes:
                    zone = overrides[max(prefixes, key=lambda prefix: len(prefix.parts))]
                try:
                    change = inspect_target(target, zone, evidence)
                except (OSError, ValueError) as exc:
                    errors.append(f"{target.scope}/{target.relative}: {exc}")
                else:
                    if change:
                        changes.append(change)
            for unused in sorted(overrides.keys() - matched):
                errors.append(f"{unused}: timezone override did not match a migration target")
            for change in changes:
                print(
                    f"{change.target.scope}/{change.target.relative}: {change.fields} timestamp(s)"
                )
            for error in errors:
                print(error, file=sys.stderr)
            print(f"{scanned} files scanned, {len(changes)} files to change, {len(errors)} errors")
            if errors:
                return 1
            if args.apply and changes:
                apply(changes, backup)
                print(f"Applied {len(changes)} files. Backup: {backup}")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
