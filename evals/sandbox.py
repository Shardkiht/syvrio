"""将合成评测数据与调用者的 Syvrio 文件分开存放。"""

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
    """两种字段检索基准共用同一离线编码器。"""
    return Config(
        vault=vault,
        retrieval=RetrievalConfig(
            method="hybrid",
            embedding=EmbeddingConfig(backend="local", model=MODEL),
        ),
    )


@contextmanager
def eval_environment(root: Path) -> Iterator[Path]:
    """将进程绑定到本次评测的路径，并提供对应的 Vault 目录。"""
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
