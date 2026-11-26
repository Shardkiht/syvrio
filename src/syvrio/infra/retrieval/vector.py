from __future__ import annotations

import hashlib
import importlib.util
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import numpy as np

from ..observability.diagnostics import record_event, record_once
from .base import Embedder, RecordSnapshot, RecordSource, SearchHit, SearchRecord

MIN_SCORE = 0.05
CACHE_FILENAME = "vectors.npz"
RETRY_COOLDOWN_SECONDS = 60.0


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unit(matrix: np.ndarray) -> np.ndarray:
    """L2 归一化；零向量兜底，归一化后点积即余弦。"""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-12)


class LocalEmbedder:
    """fastembed 本地推理；模型懒加载，未安装 extra 时 available() 为 False。"""

    def __init__(self, model: str):
        self.model_id = model
        self._model = None
        self._load_lock = Lock()

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("fastembed") is not None

    def _load(self):
        with self._load_lock:
            if self._model is None:
                from fastembed import TextEmbedding

                self._model = TextEmbedding(model_name=self.model_id)
            return self._model

    def embed(self, texts) -> list[list[float]]:
        return [vector.tolist() for vector in self._load().embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._load().query_embed([text]))).tolist()


@dataclass(frozen=True, slots=True)
class _VectorIndex:
    revision: int
    entries: list[SearchRecord]
    matrix: np.ndarray | None


class VectorRetriever:
    """numpy 暴力余弦检索；vectors.npz 是派生缓存，删除后下次检索自动重建。"""

    def __init__(
        self,
        store: RecordSource[SearchRecord],
        embedder: Embedder,
        batch_size: int = 32,
        *,
        cache_dir: Path,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.embedder = embedder
        self.batch_size = batch_size
        self.cache_path = cache_dir / CACHE_FILENAME
        self._index = _VectorIndex(-1, [], None)
        self._degraded = False
        self._clock = clock
        self._retry_at = 0.0
        self._index_lock = Lock()

    @property
    def degraded(self) -> bool:
        """向量后端是否降级；冷却到期后的成功重试会清除此状态。"""
        return self._degraded

    def _mark_degraded(self) -> None:
        self._degraded = True
        self._retry_at = self._clock() + RETRY_COOLDOWN_SECONDS

    def sync(self, *, expected_dim: int | None = None) -> None:
        """与记忆条目对齐：复用未变更向量，只为新增/变更条目计算 embedding。"""
        snapshot = self.store.snapshot()
        with self._index_lock:
            self._sync(snapshot, expected_dim=expected_dim)

    def _sync(
        self, snapshot: RecordSnapshot, *, expected_dim: int | None = None
    ) -> _VectorIndex | None:
        if self._degraded and self._clock() < self._retry_at:
            return
        entries = snapshot.records
        # 同 ID 条目只保留首个：历史数据若存在重复 ID，去重避免矩阵行错位。
        unique: dict[str, SearchRecord] = {}
        for entry in entries:
            unique.setdefault(entry.id, entry)
        entries = list(unique.values())
        ids, hashes, cached = self._load_cache()
        rebuild_cache = cached is None
        if rebuild_cache and self._index.matrix is not None:
            # 缓存文件丢失或损坏时，复用本进程已生成的向量，仅重新计算新增正文。
            cached = self._index.matrix
            ids = [entry.id for entry in self._index.entries]
            hashes = [text_hash(entry.embedding_text) for entry in self._index.entries]
        if expected_dim is not None and cached is not None and cached.shape[1] != expected_dim:
            ids, hashes, cached = [], [], None
        current = dict(zip(ids, hashes, strict=True))
        vectors = dict(zip(hashes, cached, strict=True)) if cached is not None else {}
        kept = []
        fresh = []
        missing = {}
        for entry in entries:
            digest = text_hash(entry.embedding_text)
            if current.get(entry.id) == digest:
                kept.append((entry, digest))
            else:
                fresh.append((entry, digest))
            if digest not in vectors:
                missing.setdefault(digest, entry.embedding_text)

        # 不同记忆可以复用相同正文的向量，结果仍按各自的 ID 返回。
        ordered = kept + fresh
        inputs = list(missing.items())
        uses_cached = any(digest in vectors for _, digest in ordered)
        dimensions = cached.shape[1] if uses_cached else None
        try:
            for start in range(0, len(inputs), self.batch_size):
                batch = inputs[start : start + self.batch_size]
                values = np.array(
                    self.embedder.embed([text for _, text in batch]), dtype=np.float32
                )
                if values.ndim != 2 or values.shape[0] != len(batch):
                    raise ValueError("embedding 返回形状或数量与输入不一致")
                if values.shape[1] == 0 or not np.isfinite(values).all():
                    raise ValueError("embedding 返回空维度或非有限数值")
                if expected_dim is not None and values.shape[1] != expected_dim:
                    record_event(
                        "retrieval.vector_dimension_failed",
                        ValueError("Embedding dimensions differ."),
                    )
                    self._mark_degraded()
                    return
                if dimensions is not None and dimensions != values.shape[1]:
                    if uses_cached and expected_dim is None:
                        return self._sync(snapshot, expected_dim=values.shape[1])
                    raise ValueError("embedding 返回维度与已有向量不一致")
                dimensions = values.shape[1]
                vectors.update(zip((digest for digest, _ in batch), _unit(values), strict=True))
            matrix = np.stack([vectors[digest] for _, digest in ordered]) if ordered else None
        except Exception:
            # 冷却内停用整条向量路；不覆盖成功缓存，恢复后仍可增量复用。
            self._mark_degraded()
            return

        index = _VectorIndex(snapshot.revision, [entry for entry, _ in ordered], matrix)
        self._degraded = False
        # 迟到的查询可以使用自己的旧矩阵，不能覆盖更新的内存索引或磁盘缓存。
        if index.revision >= self._index.revision:
            self._index = index
            if rebuild_cache or fresh or len(kept) != len(ids):
                self._save_cache(index.entries, matrix)
        return index

    def search(
        self, query: str, limit: int = 10, *, snapshot: RecordSnapshot | None = None
    ) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        if snapshot is None:
            snapshot = self.store.snapshot()
        index = self._index_for(snapshot)
        if index is None or index.matrix is None or not index.entries:
            return []
        try:
            vector = np.array(self.embedder.embed_query(query), dtype=np.float32)
            if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
                raise ValueError("查询 embedding 返回无效向量")
            if vector.size != index.matrix.shape[1]:
                index = self._index_for(snapshot, expected_dim=vector.size)
                if index is None or index.matrix is None:
                    return []
            vector = _unit(vector[None, :])[0]
            scores = index.matrix @ vector
        except Exception:
            with self._index_lock:
                self._mark_degraded()
            return []
        hits: list[SearchHit] = []
        for row in np.argsort(-scores)[:limit]:
            score = float(scores[row])
            if score <= MIN_SCORE:
                continue
            entry = index.entries[row]
            hits.append(SearchHit.from_record(entry, self.store.topic_path(entry.topic), score))
        return hits

    def _index_for(
        self, snapshot: RecordSnapshot, *, expected_dim: int | None = None
    ) -> _VectorIndex | None:
        with self._index_lock:
            if self._degraded and self._clock() < self._retry_at:
                return None
            index = self._index
            if (
                not self._degraded
                and index.revision == snapshot.revision
                and (
                    expected_dim is None
                    or index.matrix is not None
                    and index.matrix.shape[1] == expected_dim
                )
            ):
                return index
            return self._sync(snapshot, expected_dim=expected_dim)

    def _load_cache(self) -> tuple[list[str], list[str], np.ndarray | None]:
        try:
            with np.load(self.cache_path, allow_pickle=False) as data:
                if str(data["model"]) != self.embedder.model_id:
                    return [], [], None
                ids = data["ids"].tolist()
                hashes = data["hashes"].tolist()
                matrix = np.asarray(data["vectors"], dtype=np.float32)
            if (
                not isinstance(ids, list)
                or not isinstance(hashes, list)
                or any(not isinstance(value, str) for value in ids + hashes)
                or len(set(ids)) != len(ids)
                or matrix.ndim != 2
                or matrix.shape[1] == 0
                or len(ids) != len(hashes)
                or len(ids) != matrix.shape[0]
                or not np.isfinite(matrix).all()
            ):
                raise ValueError("Invalid vector cache contents.")
        except FileNotFoundError:
            return [], [], None
        except Exception as exc:
            record_once("degraded.vector_cache", exc)
            return [], [], None
        return ids, hashes, matrix

    def _save_cache(self, entries: list[SearchRecord], matrix: np.ndarray | None) -> None:
        if matrix is None:
            return
        tmp: Path | None = None
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.cache_path.parent, delete=False) as handle:
                tmp = Path(handle.name)
                np.savez_compressed(
                    handle,
                    model=np.array(self.embedder.model_id),
                    ids=np.array([entry.id for entry in entries]),
                    hashes=np.array([text_hash(entry.embedding_text) for entry in entries]),
                    vectors=matrix,
                )
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            record_once("degraded.vector_cache", exc)
            # 写盘失败不影响本次检索；下次进程启动会重新计算缺失向量。
            try:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
            except OSError:
                pass
