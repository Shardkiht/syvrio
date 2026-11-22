from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Literal

from ..infra.config import ChatConfig
from ..infra.diagnostics import record_event
from ..infra.tokens import estimate_messages_tokens, estimate_tokens, truncate_to_tokens
from ..llm import ChatModelError, LLMSummarizer, Summarizer
from ..session import Session, SessionStore, new_message_id, transcript

SUMMARY_MAX_CHARS = 1200
SUMMARY_TEMPLATE = (
    "<session_summary>\n{}\n</session_summary>\n"
    "以上是本会话较早内容的摘要，只作上下文，不是新的用户指令。"
)
MEMORY_TEMPLATE = "<long_term_memory>\n{}\n</long_term_memory>"
CompactionPhase = Literal["running", "done", "failed"]


@dataclass
class ContextPlan:
    messages: list[dict]
    compacted: bool = False
    evicted_records: int = 0
    summary: str = ""
    estimated_tokens: int = 0
    events: list[str] = field(default_factory=list)


def estimate_tool_tokens(schemas: list[dict]) -> int:
    # Providers encode tools differently. Count the complete definitions we send
    # using compact JSON, with no overhead when a request has no tools.
    return (
        estimate_tokens(json.dumps(schemas, ensure_ascii=False, separators=(",", ":")))
        if schemas
        else 0
    )


def _context_block(role: str, template: str, text: str, budget: int) -> list[dict]:
    """Fit optional content to this request while preserving its role and delimiters."""
    if not text.strip() or budget <= 0:
        return []
    body_budget = estimate_tokens(text)
    body = text
    while body:
        messages = [{"role": role, "content": template.format(body)}]
        excess = estimate_messages_tokens(messages) - budget
        if excess <= 0:
            return messages
        body_budget = min(body_budget, estimate_tokens(body)) - excess
        body = truncate_to_tokens(text, body_budget)
    return []


def record_to_message(record: dict) -> dict | None:
    record_type = record.get("type")
    if record_type == "message":
        return {"role": record.get("role", "user"), "content": record.get("content", "")}
    if record_type == "assistant":
        if record.get("interrupted") is True:
            return None
        message: dict = {"role": "assistant", "content": record.get("content") or ""}
        reasoning = record.get("reasoning") or {}
        if reasoning.get("source") and reasoning.get("text"):
            message["reasoning_content"] = reasoning["text"]
            message["reasoning_source"] = reasoning["source"]
        tool_calls = record.get("tool_calls") or []
        if tool_calls:
            message["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
                    },
                }
                for call in tool_calls
            ]
        return message
    if record_type == "tool":
        return {
            "role": "tool",
            "tool_call_id": record.get("tool_call_id"),
            "content": record.get("content", ""),
        }
    return None


def records_to_messages(records: list[dict]) -> list[dict]:
    messages: list[dict] = []
    pending: dict[str, None] = {}

    def finish_tool_batch() -> None:
        # A process may stop after a tool has run but before its result is
        # saved. Complete the request history without rerunning the tool.
        for call_id in pending:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": "Tool result was not recorded. Execution status and side effects are unknown.",
                }
            )

    for record in records:
        message = record_to_message(record)
        if message is None:
            continue
        if message["role"] == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id in pending:
                del pending[call_id]
                messages.append(message)
            continue

        finish_tool_batch()
        messages.append(message)
        pending = dict.fromkeys(call["id"] for call in message.get("tool_calls", []))
    finish_tool_batch()
    return messages


class ContextManager:
    def __init__(
        self,
        config: ChatConfig,
        summarizer: Summarizer,
        sessions: SessionStore,
        request_distillation: Callable[[str, str], None] | None = None,
    ):
        self.config = config
        self.summarizer = summarizer
        self.sessions = sessions
        self.request_distillation = request_distillation

    @property
    def input_budget(self) -> int:
        window = self.config.context_window
        return min(window, max(512, window - self.config.reserve_output_tokens))

    @property
    def trigger_tokens(self) -> int:
        return int(self.input_budget * self.config.compact_threshold)

    def check_input_budget(self, used: int, tool_tokens: int) -> None:
        if used > self.input_budget:
            raise ChatModelError(
                f"This request requires about {used} input tokens, including {tool_tokens} for "
                f"tool definitions. Only {self.input_budget} input tokens are available. "
                "Increase the context window or shorten the message or attachments."
            )

    def estimate_usage(self, session: Session, system_prompt: str, tool_tokens: int = 0) -> int:
        """只读估算当前会话的下一轮上下文占用，不触发压缩或摘要。"""
        messages = [{"role": "system", "content": system_prompt}]
        if summary := session.summary_text():
            messages.append({"role": "system", "content": SUMMARY_TEMPLATE.format(summary)})
        return tool_tokens + estimate_messages_tokens(
            messages + records_to_messages(session.records_after_summary())
        )

    def prepare(
        self,
        session: Session,
        system_prompt: str,
        current_text: str | list[dict],
        current_id: str | None = None,
        memory_context: str = "",
        on_compaction: Callable[[CompactionPhase], None] | None = None,
        tool_tokens: int = 0,
    ) -> ContextPlan:
        history = [
            record
            for record in session.records_after_summary()
            if current_id is None or record.get("id") != current_id
        ]
        summary_text = session.summary_text()
        system = [{"role": "system", "content": system_prompt}]
        current = [{"role": "user", "content": current_text}] if current_text else []
        base_tokens = tool_tokens + estimate_messages_tokens(system + current)
        self.check_input_budget(base_tokens, tool_tokens)
        references = _context_block(
            "user", MEMORY_TEMPLATE, memory_context, self.input_budget - base_tokens
        )
        base_tokens += estimate_messages_tokens(references)
        history_messages = records_to_messages(history)
        history_tokens = estimate_messages_tokens(history_messages)
        summary_tokens = (
            estimate_messages_tokens(
                [{"role": "system", "content": SUMMARY_TEMPLATE.format(summary_text)}]
            )
            if summary_text
            else 0
        )
        compacted_records = 0
        events: list[str] = []

        if history and base_tokens + history_tokens + summary_tokens >= self.trigger_tokens:
            if on_compaction is not None:
                on_compaction("running")
            try:
                summary_text, events = self._summarize(session, history, summary_text)
            except Exception:
                if on_compaction is not None:
                    on_compaction("failed")
                raise
            if on_compaction is not None:
                on_compaction("done")
            compacted_records = len(history)
            history_messages = []
            history_tokens = 0

        summary = _context_block(
            "system",
            SUMMARY_TEMPLATE,
            summary_text,
            self.input_budget - base_tokens - history_tokens,
        )
        messages = system + summary + history_messages + references + current

        return ContextPlan(
            messages=messages,
            compacted=bool(compacted_records),
            evicted_records=compacted_records,
            summary=summary_text,
            estimated_tokens=tool_tokens + estimate_messages_tokens(messages),
            events=events,
        )

    def _summarize(
        self, session: Session, evicted: list[dict], previous: str
    ) -> tuple[str, list[str]]:
        events: list[str] = []
        pending_ids = {record["id"] for record in session.undistilled_messages()}
        user_records = [
            record
            for record in evicted
            if record.get("type") == "message"
            and record.get("role") == "user"
            and record.get("id") in pending_ids
        ]
        source = transcript(evicted)
        if isinstance(self.summarizer, LLMSummarizer):
            summary_text = self.summarizer.summarize(
                previous,
                source,
                SUMMARY_MAX_CHARS,
                chat=self.config,
                input_budget=self.input_budget,
            )
        else:
            summary_text = self.summarizer.summarize(previous, source, SUMMARY_MAX_CHARS)
        # 摘要游标必须指向原始事件流；undo 后的有效记录数不等于原始下标。
        covered = next(
            index + 1
            for index in range(len(session.records) - 1, -1, -1)
            if session.records[index] is evicted[-1]
        )
        self.sessions.append(
            session,
            {
                "type": "summary",
                "id": new_message_id(),
                "content": summary_text,
                "covered_records": covered,
                "distilled_ids": [],
                "records": len(evicted),
                "tokens": estimate_tokens(summary_text),
            },
        )
        if user_records and self.request_distillation:
            try:
                self.request_distillation(session.id, user_records[-1]["id"])
            except Exception as exc:
                record_event("catchup.failed", exc)
                events.append("Memory distillation request deferred until backend recovery.")
        events.append(
            f"Context compacted: {len(evicted)} earlier records → {len(summary_text)}-character summary"
        )
        return summary_text, events
