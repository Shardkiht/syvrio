from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

# 全部会话共享上限；执行器只在首次收到长对话后创建工作线程。
_TOKEN_WORKERS = 4
_TOKEN_POOL = ThreadPoolExecutor(max_workers=_TOKEN_WORKERS, thread_name_prefix="syvrio-tokens")

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_ENCODING = None


def _encoding():
    """优先用 tiktoken；首次使用时下载编码表失败则退回字符估算。"""
    global _ENCODING
    if _ENCODING is None:
        try:
            import tiktoken

            _ENCODING = tiktoken.get_encoding("cl100k_base")
        except Exception as exc:
            # 降级后会按字符数估算词元数量，若不记录事件，事后就难以定位计数偏差。
            from ..observability.diagnostics import record_once

            record_once("degraded.tiktoken", exc)
            _ENCODING = False
    return _ENCODING or None


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    encoding = _encoding()
    if encoding is not None:
        try:
            return len(encoding.encode_ordinary(text))
        except Exception:
            pass
    cjk = len(CJK_RE.findall(text))
    return int(cjk + (len(text) - cjk) / 4) + 1


def _count_parts(parts: list[str]) -> int:
    return sum(map(estimate_tokens, parts))


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 4 * len(messages)
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            # 图片块单独计费，传输用的 Base64 编码不参与文本分词。
            for part in content:
                if part.get("type") == "image_url":
                    total += 1024
                elif text := part.get("text"):
                    parts.append(text)
        elif content:
            parts.append(str(content))
        if reasoning := message.get("reasoning_content"):
            parts.append(reasoning)
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            parts.extend((str(function.get("name", "")), str(function.get("arguments", ""))))
    # 原生分词器可并行。各字段仍独立编码，避免拼接文本改变词元边界。
    if len(parts) >= 64 and sum(map(len, parts)) >= 65_536 and _encoding():
        batches = (parts[index::_TOKEN_WORKERS] for index in range(_TOKEN_WORKERS))
        return total + sum(_TOKEN_POOL.map(_count_parts, batches))
    return total + _count_parts(parts)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    suffix = "\n…（已截断）"
    # 截断标记也占预算：先预留，否则返回值必然超过 max_tokens。
    budget = max_tokens - estimate_tokens(suffix)
    if budget < 1:
        suffix = ""
        budget = max_tokens
    encoding = _encoding()
    if encoding is not None:
        try:
            tokens = encoding.encode(text, disallowed_special=())
            prefix = encoding.decode(tokens[:budget], errors="ignore")
            while prefix and estimate_tokens(prefix + suffix) > max_tokens:
                prefix = prefix[:-1]
            return prefix + suffix
        except Exception:
            pass
    # 字符估算含固定开销，极小预算可能连一个汉字也放不下。
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle] + suffix) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low] + suffix
