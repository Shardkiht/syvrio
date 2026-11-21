from __future__ import annotations

from ..infra.config import ChatConfig, LLMConfig
from ..infra.tokens import estimate_messages_tokens, estimate_tokens, truncate_to_tokens
from .client import complete


class Summarizer:
    def summarize(self, previous: str, transcript: str, max_chars: int = 1200) -> str: ...


SUMMARY_PROMPT = """你是会话压缩器。把较早的对话压缩成中文摘要，供同一个助手继续对话。

必须保留：
- 用户明确陈述的事实、偏好、计划、决定、约束。
- 尚未解决的问题和用户明确提出的要求。
- 工具查证后得到的结论（不要保留工具调用过程）。

不要编造；不要记录助手自己的建议为用户事实。已有摘要可以合并进来。
只输出摘要正文，不要标题或 Markdown 代码块。"""


def _excerpts(text: str, limit: int) -> str:
    """Sample the beginning, middle and end without joining unrelated fragments."""
    if len(text) <= limit:
        return text
    separator = "\n…\n"
    if limit < 20:
        return text[:limit]
    count = min(5, max(2, limit // 120))
    width = (limit - len(separator) * (count - 1)) // count
    starts = [(len(text) - width) * index // (count - 1) for index in range(count)]
    return separator.join(text[start : start + width] for start in starts)


def fallback_summary(previous: str, transcript: str, max_chars: int = 1200) -> str:
    if max_chars <= 0:
        return ""
    transcript = "\n".join(
        line[:160] if line.startswith("工具 ") else line for line in transcript.splitlines()
    )
    entire = "\n".join(part for part in (previous, transcript) if part)
    if len(entire) <= max_chars:
        return entire
    label = "本地节选：\n"
    if max_chars <= len(label):
        return entire[:max_chars]
    max_chars -= len(label)
    if not previous or not transcript:
        return label + _excerpts(previous or transcript, max_chars)
    # Both the existing summary and the newly covered range need a share. A
    # full previous summary must never squeeze the entire new transcript out.
    previous_limit = max(max_chars // 3, max_chars - len(transcript) - 1)
    prior = _excerpts(previous, previous_limit)
    return label + prior + "\n" + _excerpts(transcript, max(0, max_chars - len(prior) - 1))


def _messages(previous: str, chunk: str, max_chars: int) -> list[dict]:
    return [
        {"role": "system", "content": SUMMARY_PROMPT},
        {
            "role": "user",
            "content": (
                f"已有摘要：\n{previous or '（无）'}\n\n需要压缩的对话：\n{chunk}\n\n"
                f"输出不超过 {max_chars} 字。"
            ),
        },
    ]


def _chunk_length(previous: str, source: str, start: int, max_chars: int, budget: int) -> int:
    # The character cap bounds search cost per request. The cursor advances
    # over actual Unicode characters, so a long line also reaches later chunks.
    high = min(16000, len(source) - start)
    if (
        estimate_messages_tokens(_messages(previous, source[start : start + high], max_chars))
        <= budget
    ):
        return high
    low = 0
    while low < high:
        mid = (low + high + 1) // 2
        if (
            estimate_messages_tokens(_messages(previous, source[start : start + mid], max_chars))
            <= budget
        ):
            low = mid
        else:
            high = mid - 1
    return low


class LLMSummarizer:
    def __init__(self, config: LLMConfig):
        self.config = config

    def summarize(
        self,
        previous: str,
        transcript: str,
        max_chars: int = 1200,
        *,
        chat: ChatConfig | None = None,
        input_budget: int | None = None,
    ) -> str:
        if max_chars <= 0:
            return ""
        if self.config.requires_api_key() and not self.config.api_key():
            return fallback_summary(previous, transcript, max_chars)
        chat = chat or ChatConfig()
        budget = (
            input_budget
            if input_budget is not None
            else chat.context_window - chat.reserve_output_tokens
        )
        budget = min(budget, chat.context_window - 1)
        carry_budget = (budget - estimate_messages_tokens(_messages("", "", max_chars))) // 2
        if carry_budget <= 0:
            return fallback_summary(previous, transcript, max_chars)
        # A prior summary from a larger window may need chunking itself.
        if estimate_tokens(previous) > carry_budget:
            transcript = f"此前摘要：{previous}\n\n{transcript}"
            previous = ""
        offset = 0
        # Bound foreground latency on a very large session or a small window.
        for _ in range(8):
            if offset == len(transcript):
                return previous
            length = _chunk_length(previous, transcript, offset, max_chars, budget)
            if not length:
                break
            messages = _messages(previous, transcript[offset : offset + length], max_chars)
            output_tokens = min(
                max_chars, carry_budget, chat.context_window - estimate_messages_tokens(messages)
            )
            try:
                response = complete(
                    self.config, messages, temperature=0.2, max_tokens=output_tokens
                )
                content = (response.choices[0].message.content or "").strip()
            except Exception:
                break
            if not content:
                break
            offset += length
            previous = content[:max_chars]
            if offset == len(transcript):
                return previous
            previous = truncate_to_tokens(previous, carry_budget)
        return fallback_summary(previous, transcript[offset:], max_chars)
