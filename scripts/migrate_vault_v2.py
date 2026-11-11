#!/usr/bin/env python3
"""Migrate into a fresh Vault/Runtime pair. Never edits the input or uses Git remotes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import yaml

from syvrio.agent.session import SESSION_ID_RE
from syvrio.infra.paths import SyvrioPaths
from syvrio.memory.inference import evidence_v2
from syvrio.memory.legacy import FRONTMATTER_RE, parse_legacy_line
from syvrio.memory.schema import Core, SchemaError, parse_document, render_record

RECORDS = frozenset({"pending.jsonl", "resolved.jsonl", "audit.jsonl", "inferences.jsonl"})
RUNTIME = {
    "reflection-jobs.jsonl": "jobs/dreaming/jobs.jsonl",
    "reflection-state.json": "state/reflection.json",
    "profile-fingerprints.json": "state/fingerprints.json",
    "profile-summary.json": "cache/profile/profile-summary.json",
    "search-usage.json": "state/usage.json",
    "usage.json": "state/usage.json",
    "git-sync-state.json": "state/git-sync.json",
    "search-cache.json": "cache/search/search-cache.json",
    "vectors.npz": "indexes/vectors.npz",
}
REPORT = "migration-v2.json"


def inventory(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f"Unsupported filesystem entry: {rel}")
        if path.is_file():
            with path.open("rb") as handle:
                result[rel] = hashlib.file_digest(handle, "sha256").hexdigest()
        else:
            result[rel + "/"] = "directory"
    return result


def legacy_memory(raw: bytes, old_path: str, new_path: str) -> tuple[bytes, list[str], int]:
    original = raw.decode("utf-8")
    if not original.startswith("---") and old_path == new_path:
        try:
            doc = parse_document(original, old_path, old_path)
        except SchemaError:
            pass
        else:
            return raw, [r.entry.id for r in doc.records], 0
    normalized = original.replace("\r\n", "\n")
    match = FRONTMATTER_RE.match(normalized)
    if normalized.startswith("---") and not match:
        raise ValueError(f"{old_path}: invalid legacy frontmatter")
    if match:
        header = yaml.safe_load(match.group(1))
        if not isinstance(header, dict):
            raise ValueError(f"{old_path}: invalid legacy frontmatter")
        normalized = normalized[match.end() :]
        if header.get("title") and not re.search(r"^# ", normalized, re.M):
            title = " ".join(str(header["title"]).splitlines())
            normalized = f"# {title}\n\n{normalized}"
    ids = []
    results = []
    reviews = 0
    for _number, line in enumerate(normalized.splitlines(keepends=True), 1):
        entry = parse_legacy_line(line, old_path) if line.startswith("- ") else None
        if entry is None:
            results.append(line)
            continue
        old = dict(entry.metadata)
        source = old.pop("source", "")
        confidence = old.pop("confidence", "")
        needs_review = (
            not source.strip() or any(char in source for char in "\n\r\0") or not confidence
        )
        try:
            score = float(confidence)
            if not 0 <= score <= 1:
                raise ValueError
        except ValueError:
            score = 0.0
            needs_review = True
        context = ()
        try:
            if entry.context:
                context = entry.context.markdown_fields()
                old.pop("context", None)
        except ValueError:
            needs_review = True
        extensions = [("syvrio.recorded_at", entry.timestamp)]
        if entry.supersedes:
            extensions.append(("syvrio.supersedes", entry.supersedes))
        if needs_review:
            reviews += 1
            extensions.append(("migration.review_required", "true"))
            if source:
                extensions.append(("migration.source_percent_encoded", quote(source, safe="")))
            if confidence:
                extensions.append(
                    ("migration.confidence_percent_encoded", quote(confidence, safe=""))
                )
        for key, value in old.items():
            if (
                re.fullmatch(r"[a-z][a-z0-9_]*", key)
                and value.strip()
                and not any(c in value for c in "\r\n\0")
            ):
                extensions.append((f"syvrio.{key}", value))
            else:
                extensions.append(
                    (
                        "migration.legacy_field_percent_encoded",
                        quote(key, safe="") + "=" + quote(value, safe=""),
                    )
                )
        core = Core(
            id=entry.id,
            status=entry.status,
            source=source
            if source.strip() and not any(c in source for c in "\n\r\0")
            else "migration:unknown",
            confidence=score,
        )
        draft = render_record(entry.text, core, context, extensions)
        projected = parse_document(draft, new_path, new_path).records[0].entry
        # Reproduce the v1 semantic snapshot used by existing inference records,
        # without putting session/record offsets in future evidence identities.
        payload = entry.model_dump(
            include={"id", "topic", "text", "timestamp", "status", "supersedes", "metadata"}
        )
        payload["topic"] = Path(old_path).stem
        previous = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        extensions.extend(
            (("migration.evidence_v1", previous), ("migration.evidence_v2", evidence_v2(projected)))
        )
        results.extend(("\n", render_record(entry.text, core, context, extensions), "\n"))
        ids.append(entry.id)
    result = "".join(results)
    parsed = parse_document(result, new_path, new_path)
    if [record.entry.id for record in parsed.records] != ids:
        raise ValueError(f"{old_path}: memory records did not round trip")
    return result.encode("utf-8"), ids, reviews


def session_target(data: bytes, path: Path) -> str:
    if not SESSION_ID_RE.fullmatch(path.stem):
        raise ValueError(f"Unsafe session ID: {path.name}")
    first = next((line for line in data.splitlines() if line.strip()), b"")
    date = None
    if first:
        try:
            metadata = json.loads(first)
        except (ValueError, UnicodeError):
            metadata = None
        if isinstance(metadata, dict) and metadata.get("type") == "meta":
            if metadata.get("id", path.stem) != path.stem:
                raise ValueError(f"Session ID differs from filename: {path.name}")
            try:
                date = datetime.fromisoformat(metadata.get("started_at", ""))
            except (ValueError, TypeError):
                pass
    directory = f"{date.year:04d}/{date.month:02d}" if date else "legacy/undated"
    return f"sessions/{directory}/{path.name}"


def build(
    source: Path,
    vault: Path,
    runtime: Path,
    source_runtime: Path | None = None,
    bound_vault: Path | None = None,
) -> dict:
    if (source / ".git").is_file() or (
        source / ".git" / "objects" / "info" / "alternates"
    ).exists():
        raise ValueError(
            "Migration requires a self-contained Git directory without alternate object stores"
        )
    before = inventory(source)
    before_runtime = inventory(source_runtime) if source_runtime else {}
    origins: dict[str, str] = {}
    ids: set[str] = set()
    sessions: set[str] = set()
    counts: Counter[str] = Counter()

    def put(bucket: str, rel: str, original: Path, data: bytes | None = None) -> None:
        label = f"{bucket}/{rel}"
        if label in origins:
            raise ValueError(f"Destination collision: {label}")
        origins[label] = (
            original.relative_to(source).as_posix()
            if original.is_relative_to(source)
            else ("runtime/" + original.relative_to(source_runtime).as_posix())
        )
        output = (vault if bucket == "vault" else runtime) / rel
        output.parent.mkdir(parents=True, exist_ok=True)
        if data is None:
            shutil.copy2(original, output)
        else:
            output.write_bytes(data)
            shutil.copymode(original, output)

    vault.mkdir()
    runtime.mkdir()
    for name in before:
        path = source / name
        rel = Path(name)
        if name.endswith("/"):
            # Git needs directories such as objects and refs even in an empty repo.
            if rel.parts[0] not in {".syvrio", "sessions", "memories"}:
                (vault / rel).mkdir(parents=True, exist_ok=True)
            continue
        if rel.parts[0] == ".syvrio":
            if len(rel.parts) == 2 and path.name in RECORDS:
                put("vault", f"records/memory/{path.name}", path)
            elif len(rel.parts) == 2 and path.name in RUNTIME:
                put("runtime", RUNTIME[path.name], path)
            else:
                put("runtime", "legacy/vault/" + name, path)
        elif rel.parts[0] == "memories" and path.suffix == ".md":
            dest = f"memories/general/{path.name}" if len(rel.parts) == 2 else name
            data = path.read_bytes()
            migrated, found, reviews = legacy_memory(data, name, dest)
            if ids.intersection(found) or len(set(found)) != len(found):
                raise ValueError(f"Duplicate memory ID in {name}")
            ids.update(found)
            counts["manual_review"] += reviews
            put("vault", dest, path, migrated)
            if migrated != data:
                put("runtime", "legacy/vault/" + name, path)
        elif rel.parts[0] == "sessions" and path.suffix == ".jsonl":
            if path.stem in sessions:
                raise ValueError(f"Duplicate session ID: {path.stem}")
            sessions.add(path.stem)
            put("vault", session_target(path.read_bytes(), path), path)
        else:
            put("vault", name, path)
    for name in before_runtime:
        if name in {REPORT, "state/vault-binding.json"}:
            put("runtime", "legacy/runtime/" + name, source_runtime / name)
            continue
        if name.endswith("/"):
            (runtime / name).mkdir(parents=True, exist_ok=True)
        else:
            put("runtime", name, source_runtime / name)
    for name in RECORDS:
        record = vault / "records" / "memory" / name
        counts[name.removesuffix(".jsonl")] = (
            sum(bool(line.strip()) for line in record.read_bytes().splitlines())
            if record.is_file()
            else 0
        )
    if bound_vault is not None:
        state = runtime / "state"
        state.mkdir(exist_ok=True)
        (state / "vault-binding.json").write_text(
            json.dumps({"version": 2, "vault": str(bound_vault)}), encoding="utf-8"
        )
    if (
        before != inventory(source)
        or source_runtime
        and before_runtime != inventory(source_runtime)
    ):
        raise ValueError("Source changed during migration; stop Syvrio and retry")
    return {
        "schema_version": 2,
        "counts": {
            **counts,
            "memory": len(ids),
            "session": len(sessions),
            "source_files": sum(not p.endswith("/") for p in before),
            "source_runtime_files": sum(not p.endswith("/") for p in before_runtime),
        },
        "memory_ids": sorted(ids),
        "session_ids": sorted(sessions),
        "source": before,
        "source_runtime": before_runtime,
        "origins": origins,
        "vault": inventory(vault),
        "runtime": inventory(runtime),
    }


def verify(report: dict, vault: Path, runtime: Path) -> None:
    if not vault.is_dir() or not runtime.is_dir():
        raise ValueError("Destination Vault/Runtime pair is incomplete")
    actual = inventory(runtime)
    actual.pop(REPORT, None)
    if report["vault"] != inventory(vault) or report["runtime"] != actual:
        raise ValueError("Destination differs from the verified migration plan")
    existing = json.loads((runtime / REPORT).read_text(encoding="utf-8"))
    if existing != report:
        raise ValueError("Migration report differs from the current source")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--source-runtime", type=Path)
    parser.add_argument("--runtime-destination", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag in ("dry-run", "apply", "verify"):
        mode.add_argument("--" + flag, action="store_true")
    args = parser.parse_args(argv)
    src, dst = args.source.resolve(), args.destination.resolve()
    run = (args.runtime_destination or dst.with_name(dst.name + "-runtime")).resolve()
    old_run = args.source_runtime.resolve() if args.source_runtime else None
    roots = [src, dst, run, *([old_run] if old_run else [])]
    if any(
        a.is_relative_to(b) or b.is_relative_to(a)
        for i, a in enumerate(roots)
        for b in roots[i + 1 :]
    ):
        parser.error("source Vault, destination Vault and Runtime directories must not overlap")
    if not src.is_dir() or old_run and not old_run.is_dir():
        parser.error("source directory does not exist")
    # Keep both renames on one filesystem. Runtime is published first: an
    # interrupted run can never expose a Vault without its prepared Runtime.
    if dst.parent != run.parent or not dst.parent.is_dir():
        parser.error("destination Vault and Runtime must share an existing parent directory")
    try:
        SyvrioPaths(dst, run).validate()
        staging_parent = dst.parent if args.apply else None
        with tempfile.TemporaryDirectory(prefix=".syvrio-v2-", dir=staging_parent) as stage:
            draft_vault, draft_runtime = Path(stage) / "vault", Path(stage) / "runtime"
            report = build(src, draft_vault, draft_runtime, old_run, dst)
            if args.verify or dst.exists():
                verify(report, dst, run)
            elif args.apply:
                if run.exists():
                    if (
                        not run.is_dir()
                        or json.loads((run / REPORT).read_text(encoding="utf-8")) != report
                    ):
                        raise ValueError(
                            "Runtime destination already exists with a different migration"
                        )
                    previous = inventory(run)
                    previous.pop(REPORT, None)
                    if previous != report["runtime"]:
                        raise ValueError("Interrupted Runtime destination has changed")
                else:
                    with (draft_runtime / REPORT).open("x", encoding="utf-8") as handle:
                        json.dump(report, handle, ensure_ascii=False, indent=2)
                    os.rename(draft_runtime, run)
                os.rename(draft_vault, dst)
                verify(report, dst, run)
            elif run.exists():
                raise ValueError("Runtime destination already exists")
            print(
                json.dumps(
                    {
                        "status": "dry-run" if args.dry_run else "verified",
                        "counts": report["counts"],
                    }
                )
            )
    except (ValueError, OSError, yaml.YAMLError) as exc:
        parser.exit(1, f"Migration refused: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
