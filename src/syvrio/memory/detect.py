"""中日韩文本字面匹配的检索信号和共用的凭据脱敏逻辑。"""

from __future__ import annotations

import re
from functools import lru_cache

from ..infra.secrets import contains_secret as contains_secret
from ..infra.secrets import redact_secrets as redact_secrets

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
