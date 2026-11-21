"""Durable requests shared by the Vault transaction and resident job workers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from .paths import SyvrioPaths
from .transaction import vault_transaction

JobKind = Literal["context", "distillation", "dream", "title"]


def job_directory(paths: SyvrioPaths, kind: JobKind) -> Path:
    vault = os.path.normcase(str(paths.vault_dir.resolve()))
    binding = hashlib.sha256(vault.encode("utf-8")).hexdigest()
    return paths.jobs_dir / "background-memory" / binding / kind


def job_path(paths: SyvrioPaths, kind: JobKind, key: str) -> Path:
    name = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return job_directory(paths, kind) / f"{name}.json"


def read_job(path: Path) -> dict | None:
    if path.resolve() != path.absolute():
        raise ValueError("Background jobs must stay inside the configured Runtime.")
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return None


def encode_job(record: dict) -> bytes:
    return (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def enqueue_context_fill(paths: SyvrioPaths, memory_id: str, version: str) -> None:
    """Commit the job with the edited fact and removal of the pending entry."""
    path = job_path(paths, "context", memory_id)
    record = {"id": memory_id, "version": version}
    with vault_transaction(paths) as tx:
        existing = read_job(path)
        if existing is None or existing["version"] != version:
            tx.write(path, encode_job(record))
