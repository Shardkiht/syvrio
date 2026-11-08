from __future__ import annotations

import re

from ..memory.detect import bigram_coverage, has_literal_bigrams
from .base import Retriever, SearchHit

RRF_K = 60
# keyword 命中的字面质量闸：查询的 CJK bigram 覆盖率低于该值的命中视为虚匹配，
# 不参与 RRF（否则干扰项的 BM25 高名次会把向量路的正确结果挤下去，
# 见 evals/ 数字类失败用例："投递了多少份申请" → "申请里的经历的措辞…"）。
MIN_QUERY_COVERAGE = 0.3
_ASCII_TERM = re.compile(r"[a-z0-9][a-z0-9_+.#-]*[a-z0-9_+#]", re.IGNORECASE | re.ASCII)


class HybridRetriever:
    """BM25 + 向量 RRF 融合；向量路失败或为空时退化为纯 BM25。"""

    def __init__(self, keyword: Retriever, vector: Retriever):
        self.keyword = keyword
        self.vector = vector

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        deep = max(limit * 3, 12)
        keyword_hits = self.keyword.search(query, deep)
        vector_hits = self.vector.search(query, deep)
        # 查询本身也可能触发降级；纯关键词兜底不套用于融合降噪的字面闸。
        if not vector_hits or getattr(self.vector, "degraded", False):
            return keyword_hits[:limit]
        # 中文 bigram 与技术名称都可以提供字面证据。
        literal_gate = has_literal_bigrams(query)
        ascii_terms = set(_ASCII_TERM.findall(query.lower())) if literal_gate else set()
        exact_fields = {hit.id: hit.matched_field for hit in keyword_hits if hit.matched_field}
        keyword_hits = [
            hit for hit in keyword_hits
            if (hit.id in exact_fields or not literal_gate
                or (ascii_terms and not ascii_terms.isdisjoint(
                    _ASCII_TERM.findall((hit.index_text or hit.text).lower())
                ))
                or bigram_coverage(query, hit.index_text or hit.text) >= MIN_QUERY_COVERAGE)
        ]
        if not keyword_hits:
            return vector_hits[:limit]

        scores: dict[str, float] = {}
        for hits in (keyword_hits, vector_hits):
            for rank, hit in enumerate(hits):
                scores[hit.id] = scores.get(hit.id, 0.0) + 1.0 / (RRF_K + rank)
        vector_rank = {hit.id: rank for rank, hit in enumerate(vector_hits)}
        # RRF 完全平局（如 BM25 只剩虚词噪声时）按向量名次决胜；展示取向量侧命中，分数更有语义。
        ordered = sorted(
            scores.items(),
            key=lambda item: (item[0] not in exact_fields, -item[1],
                              vector_rank.get(item[0], len(vector_rank))),
        )[:limit]
        by_id: dict[str, SearchHit] = {}
        for hits in (vector_hits, keyword_hits):
            for hit in hits:
                by_id.setdefault(hit.id, hit)
        return [
            by_id[entry_id].model_copy(update={"matched_field": exact_fields[entry_id]})
            if entry_id in exact_fields else by_id[entry_id]
            for entry_id, _ in ordered
        ]
