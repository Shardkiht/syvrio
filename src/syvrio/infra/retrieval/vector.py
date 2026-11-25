from __future__ import annotations

import hashlib
import importlib.util
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

import numpy as np

from ..observability.diagnostics import record_event
from ..storage.database import transaction
from ..storage.paths import SyvrioPaths
from .base import Embedder, RecordSnapshot, RecordSource, SearchHit, SearchRecord

MIN_SCORE = 0.05
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

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("fastembed") is not None

    def _load(self):
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
    """基于一致事实快照的余弦检索。"""

    def __init__(
        self,
        store: RecordSource[SearchRecord],
        embedder: Embedder,
        batch_size: int = 32,
        *,
        paths: SyvrioPaths | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.paths = paths
        self.embedder = embedder
        self.batch_size = batch_size
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
        if expected_dim is not None and cached is not None and cached.shape[1] != expected_dim:
            ids, hashes, cached = [], [], None
        known = {entry_id: index for index, entry_id in enumerate(ids)}
        keep: list[int] = []
        fresh: list[SearchRecord] = []
        for entry in entries:
            index = known.get(entry.id)
            if index is not None and hashes[index] == text_hash(entry.embedding_text):
                keep.append(index)
            else:
                fresh.append(entry)

        if cached is None:
            cached = np.zeros((0, 0), dtype=np.float32)
        rows = [cached[keep]] if keep else []
        try:
            for start in range(0, len(fresh), self.batch_size):
                batch = fresh[start : start + self.batch_size]
                vectors = np.array(
                    self.embedder.embed([entry.embedding_text for entry in batch]), dtype=np.float32
                )
                if vectors.ndim != 2 or vectors.shape[0] != len(batch):
                    raise ValueError("embedding 返回形状或数量与输入不一致")
                if vectors.shape[1] == 0 or not np.isfinite(vectors).all():
                    raise ValueError("embedding 返回空维度或非有限数值")
                if expected_dim is not None and vectors.shape[1] != expected_dim:
                    record_event(
                        "retrieval.vector_dimension_failed",
                        ValueError("Embedding dimensions differ."),
                    )
                    self._mark_degraded()
                    return
                if keep and expected_dim is None and vectors.shape[1] != cached.shape[1]:
                    return self._sync(snapshot, expected_dim=vectors.shape[1])
                if rows and vectors.shape[1] != rows[0].shape[1]:
                    raise ValueError("embedding 返回维度与已有向量不一致")
                rows.append(_unit(vectors))
            matrix = np.vstack(rows) if rows else None
        except Exception:
            # 冷却内停用整条向量路；不覆盖成功缓存，恢复后仍可增量复用。
            self._mark_degraded()
            return

        kept_ids = {ids[index] for index in keep}
        ordered = [entry for entry in entries if entry.id in kept_ids] + fresh
        index = _VectorIndex(snapshot.revision, ordered, matrix)
        self._degraded = False
        # 迟到的查询可以使用自己的旧矩阵，不能覆盖更新的内存索引或磁盘缓存。
        if index.revision >= self._index.revision:
            self._index = index
            if fresh or len(keep) != len(ids):
                self._save_cache(ordered, matrix, snapshot.revision)
        return index

    def search(
        self, query: str, limit: int = 10, *, snapshot: RecordSnapshot | None = None
    ) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        if snapshot is None:
            snapshot = self.store.snapshot()
        with self._index_lock:
            return self._search(query, limit, snapshot)

    def _search(self, query: str, limit: int, snapshot: RecordSnapshot) -> list[SearchHit]:
        if self._degraded and self._clock() < self._retry_at:
            return []
        index = self._index
        if snapshot.revision != index.revision or self._degraded:
            index = self._sync(snapshot)
        if index is None or index.matrix is None or not index.entries:
            return []
        try:
            vector = np.array(self.embedder.embed_query(query), dtype=np.float32)
            if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
                raise ValueError("查询 embedding 返回无效向量")
            if vector.size != index.matrix.shape[1]:
                index = self._sync(snapshot, expected_dim=vector.size)
                if index is None or index.matrix is None:
                    return []
            vector = _unit(vector[None, :])[0]
            scores = index.matrix @ vector
        except Exception:
            self._mark_degraded()
            return []
        hits: list[SearchHit] = []
        for row in np.argsort(-scores)[:limit]:
            score = float(scores[row])
            if score <= MIN_SCORE:
                continue
            entry = index.entries[row]
            hits.append(SearchHit.from_record(entry, score))
        return hits

    def _load_cache(self) -> tuple[list[str], list[str], np.ndarray | None]:
        if self.paths is None:
            return [], [], None
        with transaction(self.paths, write=False) as db:
            rows = db.execute(
                "SELECT memory_id,input_digest,dimensions,vector FROM vectors WHERE model=? ORDER BY memory_id",
                (self.embedder.model_id,),
            ).fetchall()
        if not rows:
            return [], [], None
        dims = rows[0]["dimensions"]
        if dims <= 0 or any(r["dimensions"] != dims or len(r["vector"]) != dims * 4 for r in rows):
            return [], [], None
        matrix = np.vstack([np.frombuffer(r["vector"], dtype="<f4") for r in rows])
        if not np.isfinite(matrix).all():
            return [], [], None
        return [r["memory_id"] for r in rows], [r["input_digest"] for r in rows], matrix

    def _save_cache(self, entries, matrix, revision):
        if self.paths is None:
            return
        with transaction(self.paths) as db:
            if (
                db.execute("SELECT version FROM revisions WHERE domain='memories'").fetchone()[0]
                != revision
            ):
                return
            db.execute(
                "DELETE FROM vectors WHERE model=? AND memory_id IN (SELECT id FROM memories WHERE status!='active')",
                (self.embedder.model_id,),
            )
            if matrix is not None:
                db.executemany(
                    """INSERT INTO vectors VALUES(?,?,?,?,?) ON CONFLICT(memory_id,model) DO UPDATE SET
                    input_digest=excluded.input_digest,dimensions=excluded.dimensions,vector=excluded.vector""",
                    (
                        (
                            e.id,
                            self.embedder.model_id,
                            text_hash(e.embedding_text),
                            len(v),
                            v.astype("<f4").tobytes(),
                        )
                        for e, v in zip(entries, matrix, strict=True)
                    ),
                )
