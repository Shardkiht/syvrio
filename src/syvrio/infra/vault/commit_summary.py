from __future__ import annotations

import re

from ..config import LLMConfig
from ..llm.client import complete, effective_context_window, limit_output_tokens, model_metadata
from ..llm.tokens import estimate_messages_tokens, estimate_tokens, truncate_to_tokens

SUMMARY_PROMPT = """你是个人记忆库的提交总结器。根据 git diff 生成给本人审阅的中文摘要。

要求：
- 2 到 4 条短句，说明这次新增、更新或删除了哪些记忆、涉及哪些主题。
- 只依据 diff 内容，不要推测动机或编造。
- 不要输出 Markdown 列表符号或标题，每行一条。
- 如果只有格式或元数据变化，直接说明。"""


def fallback_summary(staged_stat: str) -> str:
    files: list[str] = []
    total = ""
    for line in staged_stat.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            files.append(line.split("|", 1)[0].strip())
        elif "changed" in line:
            total = line
    if not files:
        return "memory: 更新记忆库"
    listed = "、".join(files[:6]) + ("等" if len(files) > 6 else "")
    head = f"memory: 更新 {listed}"
    return f"{head}\n（{total}）" if total else head


def _clean(text: str) -> str:
    lines = []
    for line in text.strip().splitlines():
        line = re.sub(r"^[-*•\d.、\s]+", "", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines[:4])


def _summary_messages(diff: str, stat: str, budget: int) -> list[dict]:
    messages = [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": f"变更统计：\n{stat}\n\n{diff}"},
    ]
    if estimate_messages_tokens(messages) <= budget:
        return messages
    # 超长补丁先保留完整文件统计，再按文件分摊节选空间，避免排在后面的文件被吞掉。
    overview = f"变更统计：\n{stat}\n\n以下为各文件的差异节选，请只依据可见内容概括：\n"
    messages[-1]["content"] = overview
    available = budget - estimate_messages_tokens(messages)
    if available <= 0:
        return []
    patches = [part for part in re.split(r"(?m)(?=^diff --git )", diff) if part]
    excerpts = [""] * len(patches)
    smallest_first = sorted(range(len(patches)), key=lambda index: len(patches[index]))
    for position, index in enumerate(smallest_first):
        remaining = len(patches) - position
        excerpt = truncate_to_tokens(patches[index], max(0, available // remaining - 1))
        excerpts[index] = excerpt
        available -= estimate_tokens(excerpt) + 1
    messages[-1]["content"] += "\n".join(excerpts)
    if estimate_messages_tokens(messages) > budget:
        messages[-1]["content"] = overview
    return messages


def summarize_diff(diff: str, config: LLMConfig, staged_stat: str, context_window: int) -> str:
    fallback = fallback_summary(staged_stat)
    if not diff.strip() or (config.requires_api_key() and not config.api_key()):
        return fallback
    try:
        window = effective_context_window(config, context_window)
        output_tokens = limit_output_tokens(
            model_metadata(config.litellm_model()), min(1024, window // 4)
        )
        budget = window - output_tokens
        messages = _summary_messages(diff, staged_stat, budget)
        if not messages:
            return fallback
        response = complete(config, messages, temperature=0.2, max_tokens=output_tokens)
        content = _clean(response.choices[0].message.content or "")
        return content or fallback
    except Exception:
        return fallback
