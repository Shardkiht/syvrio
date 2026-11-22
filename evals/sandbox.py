"""Keep synthetic evaluation data separate from the invoking user's Syvrio files."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from syvrio.infra.config import Config, EmbeddingConfig, RetrievalConfig

if __package__:
    from .retrieval_fixture import MODEL
else:
    from retrieval_fixture import MODEL


def local_hybrid_config(vault: Path) -> Config:
    """Use the same offline encoder for both field-retrieval benchmarks."""
    return Config(
        vault=vault,
        retrieval=RetrievalConfig(
            method="hybrid",
            embedding=EmbeddingConfig(backend="local", model=MODEL),
        ),
    )


@contextmanager
def eval_environment(root: Path) -> Iterator[Path]:
    """Bind the process to one evaluation's paths and yield its Vault directory."""
    root = root.resolve()
    paths = {
        "SYVRIO_CONFIG": root / "config.toml",
        "SYVRIO_VAULT": root / "vault",
        "SYVRIO_RUNTIME": root / "runtime",
    }
    previous = {key: os.environ.get(key) for key in paths}
    try:
        os.environ.update({key: str(path) for key, path in paths.items()})
        yield paths["SYVRIO_VAULT"]
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
