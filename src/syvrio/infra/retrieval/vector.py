from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

import numpy as np

from ..observability.diagnostics import record_event, record_once
from ..storage.cache_database import cache_transaction
from ..storage.database import DatabaseError, on_commit, transaction, transaction_mode
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
    revision: int | None
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
        self._index_for(snapshot, expected_dim=expected_dim, refresh=True)

    def _index_for(
        self, snapshot: RecordSnapshot, *, expected_dim: int | None = None, refresh: bool = False
    ) -> _VectorIndex | None:
        with self._index_lock:
            if self._degraded and self._clock() < self._retry_at:
                return None
            index = self._index
            if (
                not refresh
                and not self._degraded
                and index.revision == snapshot.revision
                and (
                    expected_dim is None
                    or index.matrix is not None
                    and index.matrix.shape[1] == expected_dim
                )
            ):
                return index
            built = self._build(snapshot, expected_dim=expected_dim)
            if built is None:
                return None
            index, fresh, removed = built
            self._degraded = False
            if index.revision is None or index.revision < self._index.revision:
                return index
            self._index = index

        # 提交后才发布衍生数据，且不在等待磁盘时持有内存索引锁。
        if self.paths is not None and (fresh or removed):

            def persist():
                matrix = index.matrix[-len(fresh) :] if fresh else None
                try:
                    self._save_cache(fresh, matrix, removed, index.revision)
                except (sqlite3.Error, OSError, DatabaseError) as exc:
                    # 结果已在内存中可用，业务提交后失败的缓存写入仅影响下次复用。
                    record_once("degraded.vector_cache", exc)

            if transaction_mode() is None:
                persist()
            else:
                on_commit(persist)
        return index

    def _build(self, snapshot: RecordSnapshot, *, expected_dim: int | None):
        entries = snapshot.records
        # 同 ID 条目只保留首个：历史数据若存在重复 ID，去重避免矩阵行错位。
        unique: dict[str, SearchRecord] = {}
        for entry in entries:
            unique.setdefault(entry.id, entry)
        entries = list(unique.values())
        cache_available = True
        try:
            ids, hashes, cached = self._load_cache()
        except (sqlite3.Error, OSError, DatabaseError) as exc:
            # 磁盘缓存不可用时按输入摘要复用上次成功的内存向量。不覆盖未知文件。
            record_once("degraded.vector_cache", exc)
            cache_available = False
            cached = self._index.matrix
            ids = [entry.id for entry in self._index.entries]
            hashes = [text_hash(entry.embedding_text) for entry in self._index.entries]
        removed = set(ids).difference(unique)
        if expected_dim is not None and cached is not None and cached.shape[1] != expected_dim:
            cached = None
        current = dict(zip(ids, hashes, strict=True)) if cached is not None else {}
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

        # 相同输入可以共用一次嵌入，每个事实仍保留自己的 ID 和检索结果。
        inputs = list(missing.items())
        uses_cached = any(digest in vectors for _, digest in kept + fresh)
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
                        return self._build(snapshot, expected_dim=values.shape[1])
                    raise ValueError("embedding 返回维度与已有向量不一致")
                dimensions = values.shape[1]
                vectors.update(zip((digest for digest, _ in batch), _unit(values), strict=True))
            ordered = kept + fresh
            matrix = np.stack([vectors[digest] for _, digest in ordered]) if ordered else None
        except Exception:
            # 冷却内停用整条向量路；不覆盖成功缓存，恢复后仍可增量复用。
            self._mark_degraded()
            return

        index = _VectorIndex(snapshot.revision, [entry for entry, _ in ordered], matrix)
        if not cache_available:
            return index, [], set()
        return index, [entry for entry, _ in fresh], removed

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
            hits.append(SearchHit.from_record(entry, score))
        return hits

    def _load_cache(self) -> tuple[list[str], list[str], np.ndarray | None]:
        if self.paths is None:
            return [], [], None
        with cache_transaction(self.paths, "vectors", write=False) as db:
            rows = db.execute(
                "SELECT memory_id,input_digest,dimensions,vector FROM vectors WHERE model=? ORDER BY memory_id",
                (self.embedder.model_id,),
            ).fetchall()
        if not rows:
            return [], [], None
        ids = [row["memory_id"] for row in rows]
        dims = rows[0]["dimensions"]
        if (
            not isinstance(dims, int)
            or dims <= 0
            or any(
                r["dimensions"] != dims
                or not isinstance(r["vector"], bytes)
                or len(r["vector"]) != dims * 4
                for r in rows
            )
        ):
            record_once("degraded.vector_cache", "Invalid vector cache row.")
            return ids, [], None
        matrix = np.vstack([np.frombuffer(r["vector"], dtype="<f4") for r in rows])
        if not np.isfinite(matrix).all():
            record_once("degraded.vector_cache", "Non-finite cached vector.")
            return ids, [], None
        return ids, [r["input_digest"] for r in rows], matrix

    def _save_cache(self, entries, matrix, removed, revision):
        if self.paths is None:
            return
        # 先打开主库，再串行发布缓存。取得缓存写锁后才在主库读取版本，
        # 避免较早的嵌入任务覆盖另一进程已发布的结果。
        with (
            transaction(self.paths, write=False) as source,
            cache_transaction(self.paths, "vectors") as db,
        ):
            if (
                source.execute("SELECT version FROM revisions WHERE domain='memories'").fetchone()[
                    0
                ]
                != revision
            ):
                return
            if removed:
                db.executemany(
                    "DELETE FROM vectors WHERE model=? AND memory_id=?",
                    ((self.embedder.model_id, key) for key in removed),
                )
            if matrix is not None:
                db.executemany(
                    """INSERT INTO vectors VALUES(?,?,?,?,?) ON CONFLICT(memory_id,model) DO UPDATE SET
                    input_digest=excluded.input_digest,dimensions=excluded.dimensions,vector=excluded.vector
                    WHERE input_digest<>excluded.input_digest OR dimensions<>excluded.dimensions
                       OR vector<>excluded.vector""",
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
