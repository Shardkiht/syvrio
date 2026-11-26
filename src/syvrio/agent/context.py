from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable, Literal

from ..infra.config import ChatConfig, LLMConfig
from ..infra.llm import ChatModelError
from ..infra.llm.client import chat_history
from ..infra.llm.tokens import estimate_messages_tokens, estimate_tokens, truncate_to_tokens
from ..infra.observability.diagnostics import record_event
from ..infra.session import Session, SessionStore, new_message_id, transcript
from .summarizer import LLMSummarizer, Summarizer

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
    # 不同供应商的工具编码各异，按实际发送的完整工具定义估算紧凑 JSON 的大小。
    # 请求未携带工具时，此项开销为零。
    return (
        estimate_tokens(json.dumps(schemas, ensure_ascii=False, separators=(",", ":")))
        if schemas
        else 0
    )


def _context_block(role: str, template: str, text: str, budget: int) -> list[dict]:
    """在保留角色和分隔符的前提下，将可选内容裁剪到本次请求的预算内。"""
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
        # 进程可能在工具已执行、结果尚未保存时退出。
        # 此时补全请求历史，避免重复执行工具。
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
        *,
        model_config: LLMConfig | None = None,
    ):
        self.config = config
        self.model_config = model_config
        self.summarizer = summarizer
        self.sessions = sessions
        self.request_distillation = request_distillation
        self._meter_cache: OrderedDict[tuple, int] = OrderedDict()
        self._meter_lock = Lock()

    @property
    def input_budget(self) -> int:
        window = self.config.context_window
        return min(window, max(512, window - self.config.reserve_output_tokens))

    @property
    def trigger_tokens(self) -> int:
        return int(self.input_budget * self.config.compact_threshold)

    def _request_tokens(self, messages: list[dict], *, next_turn: bool = False) -> int:
        # 空闲估算和 /compact 预期用户会开始下一轮，只计算已保存的消息。
        if next_turn:
            messages = [*messages, {"role": "user", "content": ""}]
        outgoing, _ = chat_history(self.model_config, messages)
        return estimate_messages_tokens(outgoing[:-1] if next_turn else outgoing)

    def fits_input(self, messages: list[dict], schemas: list[dict]) -> bool:
        return self._request_tokens(messages) + estimate_tool_tokens(schemas) <= self.input_budget

    def check_input_budget(self, used: int, tool_tokens: int) -> None:
        if used > self.input_budget:
            raise ChatModelError(
                f"This request requires about {used} input tokens, including {tool_tokens} for "
                f"tool definitions. Only {self.input_budget} input tokens are available. "
                "Increase the context window or shorten the message or attachments."
            )

    def estimate_usage(
        self,
        session: Session,
        system_prompt: str,
        tool_tokens: int = 0,
        *,
        active_message_id: str | None = None,
    ) -> int:
        """估算已落盘消息；正在继续工具链时保留本轮可重放的思考。"""
        return self._estimate_history(
            session.summary_text(),
            session.records_after_summary(),
            bool(active_message_id and session.last_user_message_id() == active_message_id),
            system_prompt,
            tool_tokens,
        )

    def estimate_saved_usage(
        self,
        session_id: str,
        system_prompt: str,
        tool_tokens: int,
        *,
        active_message_id: str | None,
    ) -> int:
        _, source = chat_history(self.model_config, [])
        with self.sessions.history_view(session_id) as version:
            key = (session_id, version, source, active_message_id, system_prompt, tool_tokens)
            shared = version is not None
            if shared:
                with self._meter_lock:
                    if key in self._meter_cache:
                        self._meter_cache.move_to_end(key)
                        return self._meter_cache[key]
            summary, history, active = self.sessions.context_history(
                session_id,
                include_reasoning=bool(source),
                active_message_id=active_message_id,
            )
        used = self._estimate_history(summary, history, active, system_prompt, tool_tokens)
        if shared:
            with self._meter_lock:
                self._meter_cache[key] = used
                if len(self._meter_cache) > 16:
                    self._meter_cache.popitem(last=False)
        return used

    def _estimate_history(
        self, summary: str, history: list[dict], active: bool, system_prompt: str, tool_tokens: int
    ) -> int:
        messages = [{"role": "system", "content": system_prompt}]
        if summary:
            messages.append({"role": "system", "content": SUMMARY_TEMPLATE.format(summary)})
        messages.extend(records_to_messages(history))
        # 调度器可能刚开始新回合，也可能还在收尾。核对原话已落盘，且最后仍是工具调用或结果。
        continuing_tools = bool(
            active
            and history
            and (history[-1].get("type") == "tool" or history[-1].get("tool_calls"))
        )
        return tool_tokens + self._request_tokens(messages, next_turn=not continuing_tools)

    def prepare(
        self,
        session: Session,
        system_prompt: str,
        current_text: str | list[dict],
        current_id: str | None = None,
        memory_context: str = "",
        on_compaction: Callable[[CompactionPhase], None] | None = None,
        tool_tokens: int = 0,
        *,
        active_plan: ContextPlan | None = None,
        additional_inputs: list[dict] | None = None,
    ) -> ContextPlan:
        if active_plan is not None:
            used = tool_tokens + self._request_tokens(active_plan.messages)
            if used < self.trigger_tokens:
                self.check_input_budget(used, tool_tokens)
                active_plan.estimated_tokens = used
                return active_plan

        history = session.records_after_summary()
        if active_plan is None and current_id is not None:
            history = [record for record in history if record.get("id") != current_id]
        summary_text = session.summary_text()
        system = [{"role": "system", "content": system_prompt}]
        current = [{"role": "user", "content": current_text}] if current_text else []
        current.extend(additional_inputs or ())
        base_tokens = tool_tokens + estimate_messages_tokens(system + current)
        self.check_input_budget(base_tokens, tool_tokens)
        references = _context_block(
            "user", MEMORY_TEMPLATE, memory_context, self.input_budget - base_tokens
        )
        base_tokens += estimate_messages_tokens(references)
        # 运行中的请求到达这里时，已超过会话阈值。
        history_messages = records_to_messages(history) if active_plan is None else []
        # 仅重放本轮思考的模型需要看到当前输入，才能判断哪些历史属于上一轮。
        history_tokens = (
            tool_tokens
            + self._request_tokens(
                system + history_messages + references + current, next_turn=not current
            )
            - base_tokens
        )
        summary_tokens = (
            estimate_messages_tokens(
                [{"role": "system", "content": SUMMARY_TEMPLATE.format(summary_text)}]
            )
            if summary_text
            else 0
        )
        compacted_records = 0
        events: list[str] = []

        if history and (
            active_plan is not None
            or base_tokens + history_tokens + summary_tokens >= self.trigger_tokens
        ):
            if on_compaction is not None:
                on_compaction("running")
            try:
                summary_text, events = self._summarize(session, history, summary_text, current_id)
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
        # 摘要是单独的系统消息，不改变其余消息的思考重放规则。
        if compacted_records:
            used = tool_tokens + self._request_tokens(messages, next_turn=not current)
        else:
            used = base_tokens + history_tokens + estimate_messages_tokens(summary)
        self.check_input_budget(used, tool_tokens)
        return ContextPlan(
            messages=messages,
            compacted=bool(compacted_records),
            evicted_records=compacted_records,
            summary=summary_text,
            estimated_tokens=used,
            events=events,
        )

    def _summarize(
        self, session: Session, evicted: list[dict], previous: str, active_turn_id: str | None
    ) -> tuple[str, list[str]]:
        events: list[str] = []
        pending_ids = {record["id"] for record in session.undistilled_messages()}
        user_records = [
            record
            for record in evicted
            if record.get("type") == "message"
            and record.get("role") == "user"
            and record.get("id") in pending_ids
            and (record.get("turn_root") or record["id"]) != active_turn_id
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
