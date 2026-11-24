"""检索使用的字段标签和中日韩字面匹配。"""

from __future__ import annotations

import re
from functools import lru_cache

_SHORT_FIELD = re.compile(
    r"(?P<label>[\u3400-\u4dbf\u4e00-\u9fffA-Za-z][\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9 _-]{1,17})"
    r"\s*[:：]\s*(?P<value>[^\n\r。！？]{1,64})。?"
)


def short_field(text: str) -> tuple[str, str] | None:
    """根据明确边界识别独立字段，避免猜测含义。"""
    match = _SHORT_FIELD.fullmatch(text.strip())
    if match and (value := match["value"].strip()):
        return match["label"].strip(), value
    return None


_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}")


def bigram_coverage(query: str, text: str) -> float:
    """查询中相邻中日韩字符对在正文中出现的比例。"""
    query_set = _literal_bigrams(query)
    if not query_set:
        return 0.0
    return len(query_set & _literal_bigrams(text)) / len(query_set)


def has_literal_bigrams(text: str) -> bool:
    return bool(_literal_bigrams(text))


@lru_cache(maxsize=2048)
def _literal_bigrams(text: str) -> frozenset[str]:
    return frozenset(
        run[i : i + 2] for run in _CJK_RUN_RE.findall(text) for i in range(len(run) - 1)
    )
