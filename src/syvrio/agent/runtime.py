from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from copy import copy
from dataclasses import dataclass, field, replace
from datetime import date
from itertools import groupby, islice
from threading import Event

from ..infra.config import ChatConfig, Config
from ..infra.latency import TurnLatency, elapsed_ms, track_turn
from ..infra.tokens import truncate_to_tokens
from ..infra.documents import document_paths
from ..llm import (
    AssistantTurn,
    ChatModel,
    ChatModelError,
    GenerationCancelled,
    LiteLLMChatModel,
    OpenAISummarizer,
    Summarizer,
    ToolCall,
    Usage,
)
from ..memory.extractor import split_chunks
from ..memory.detect import redact_secrets
from ..memory.service import MemoryService, load_persona
from .context import ContextManager
from .prompts import build_system_prompt
from .recall import RecallQueryBuilder, direct_recall_field, memory_references, should_auto_recall
from .session import Session, SessionStore, new_message_id
from .tools import (
    PARALLEL_NETWORK_LIMIT, PARALLEL_TOOLS, ToolOutcome, ToolRegistry,
    build_tools, cancelled_tool, compact_tool_outputs,
)


DISTILL_MAX_CHARS = 12000
MEMORIZE_BATCH_LIMIT = 8


@dataclass(frozen=True)
class ThinkingUpdate:
    text: str
    record_id: str | None = None
    seconds: float | None = None


def _merged_memorize_text(calls: list[ToolCall], user_text: str) -> str | None:
    texts = [str(call.arguments.get("text", "")).strip() for call in calls]
    if len(texts) < 2 or not all(texts):
        return None
    distinct = list(dict.fromkeys(texts))
    if len(distinct) == 1:
        return distinct[0]
    # A whole-message write carries a timestamp anchor. Never share it with fragments.
    if user_text in distinct:
        return None
    merged = "\n\n".join(f"独立片段 {index}：\n{text}" for index, text in enumerate(distinct, 1))
    return merged if merged != user_text else None


def _tool_results(
    tools: ToolRegistry, calls: list[ToolCall], on_progress: Callable[[str], None] | None,
    cancel: Event | None, *, user_text: str | None = None,
) -> Iterator[tuple[ToolCall, ToolOutcome]]:
    def ready(call: ToolCall) -> bool:
        if on_progress is not None and not (cancel and cancel.is_set()):
            on_progress(f"Calling tool: {call.name}")
        return not (cancel and cancel.is_set())

    def kind(call: ToolCall) -> str:
        if call.name in PARALLEL_TOOLS:
            return "network"
        return "memorize" if call.name == "memorize" and user_text is not None else "serial"

    for batch_kind, group in groupby(calls, key=kind):
        if batch_kind == "memorize":
            while batch := list(islice(group, MEMORIZE_BATCH_LIMIT)):
                merged = _merged_memorize_text(batch, user_text)
                if merged is None:
                    for call in batch:
                        yield call, tools.execute(call.name, call.arguments) if ready(call) else cancelled_tool()
                    continue
                first, *rest = batch
                if not ready(first):
                    for call in batch:
                        yield call, cancelled_tool()
                    continue
                outcome = tools.execute(first.name, {"text": merged})
                yield first, replace(outcome, display=f"{outcome.display} ({len(batch)} calls batched)")
                for call in rest:
                    payload = {"batched_with": first.id, "ok": outcome.ok}
                    yield call, ToolOutcome(
                        outcome.ok, json.dumps(payload), "See the shared memory write result", data=payload
                    )
            continue
        if batch_kind == "serial":
            for call in group:
                yield call, tools.execute(call.name, call.arguments) if ready(call) else cancelled_tool()
            continue
        # A non-network call is a barrier. Keep each result paired in model order.
        while batch := list(islice(group, PARALLEL_NETWORK_LIMIT)):
            waits = [
                tools.start(call.name, call.arguments, cancel=cancel, shared_deadline=True)
                if ready(call) else cancelled_tool
                for call in batch
            ]
            for call, wait in zip(batch, waits, strict=True):
                yield call, wait()


@dataclass
class AgentReply:
    content: str
    events: list[str] = field(default_factory=list)
    steps: int = 0
    compacted: bool = False
    usage: Usage = field(default_factory=Usage)
    estimated_tokens: int = 0


class AgentRuntime:
    def __init__(
        self,
        service: MemoryService,
        model: ChatModel,
        sessions: SessionStore,
        *,
        summarizer: Summarizer | None = None,
        config: ChatConfig | None = None,
    ):
        self.service = service
        self.model = model
        self.sessions = sessions
        self.config = config or service.config.chat
        self.recall_queries = RecallQueryBuilder(
            service.config.llm,
            rewrite=self.config.recall_rewrite,
            context_turns=self.config.recall_context_turns,
        )
        self.summarizer = summarizer or OpenAISummarizer(service.config.llm)
        self.context = ContextManager(
            self.config,
            self.summarizer,
            sessions,
            flush=self._flush_memories,
        )
        self.session: Session | None = None
        self.last_plan = None
        self.last_plan_session: str | None = None
        self._closed = False
        # 请求进行中的会话与被延迟打关闭标记的会话（切换不阻塞生成）。
        self._active_ask: Session | None = None
        self._deferred_close: Session | None = None

    def start(self) -> None:
        """进入新对话。会话文件在第一条消息发送时才创建。"""
        self.session = None
        self.last_plan = None
        self.last_plan_session = None
        self._closed = False

    def ensure_session(self) -> Session:
        if self.session is None:
            self.session = self.sessions.create()
            self._closed = False
        return self.session

    @property
    def has_session(self) -> bool:
        return self.session is not None

    def resume(self, session_id: str, *, close_current: bool = False) -> Session:
        session = self.sessions.load(session_id)
        if close_current:
            self.close(distill=False)
        self.session = session
        self.last_plan = None
        self.last_plan_session = None
        self._closed = False
        return self.session

    def reload_llm(self, config: "Config | None" = None) -> None:
        """配置变更后热更新模型、抽取器和摘要器，不重启会话。

        默认从 service 记住的配置路径重读；调用方持有新配置对象时直接传入，
        避免落盘与重读之间的竞态把切换回滚。
        """
        from ..llm import LLMSummarizer, LiteLLMChatModel
        from ..memory.extractor import LLMExtractor

        if config is None:
            from ..infra.config import load_config

            path = self.service.config_path
            config = load_config(path) if path else load_config()
        self.service.config.llm = config.llm
        self.service.config.chat = config.chat
        self.service.extractor = LLMExtractor(config.llm)
        self.model = LiteLLMChatModel(config.llm)
        self.summarizer = LLMSummarizer(config.llm)
        self.config = config.chat
        self.recall_queries = RecallQueryBuilder(
            config.llm,
            rewrite=config.chat.recall_rewrite,
            context_turns=config.chat.recall_context_turns,
        )
        self.context.config = config.chat
        self.context.summarizer = self.summarizer

    @property
    def model_label(self) -> str:
        return f"{self.service.config.llm.provider}/{self.service.config.llm.model}"

    @property
    def current(self) -> Session:
        if self.session is None:
            raise RuntimeError("会话尚未开始")
        return self.session

    def ask(
        self,
        text: str,
        *,
        session: Session | None = None,
        on_session: Callable[[Session], None] | None = None,
        on_user_message: Callable[[str], None] | None = None,
        on_event: Callable[[str], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_thinking: Callable[[ThinkingUpdate], None] | None = None,
        on_progress: Callable[[str], None] | None = None,
        cancel: Event | None = None,
    ) -> AgentReply:
        """执行一轮对话。

        session 显式传入时（后台/排队任务）所有读写都绑定该会话，不触碰
        self.session——切换会话不影响进行中的请求；否则使用当前会话。
        """
        text = redact_secrets(text.strip())
        if not text:
            raise ValueError("消息为空。")
        if session is None:
            session = self.ensure_session()
        if on_session is not None:
            on_session(session)
        self._active_ask = session
        try:
            with track_turn(cancel) as latency:
                return self._run_ask(
                    session, text, on_event, on_delta, on_progress, cancel,
                    latency, on_user_message, on_thinking,
                )
        except GenerationCancelled as exc:
            partial = getattr(exc, "partial_content", "")
            self._append_assistant(session, AssistantTurn(
                content=partial + "\n\n*(generation stopped by user)*",
                reasoning_content=getattr(exc, "partial_reasoning", ""),
            ), on_thinking, interrupted=True)
            raise
        finally:
            self._active_ask = None
            deferred = self._deferred_close
            self._deferred_close = None
            if deferred is session:
                # 切走/新建时该会话仍在生成：关闭标记延迟到此刻补打，
                # 保证排在最后的 assistant 记录之后；沉淀由启动补记兜底。
                if session.user_texts_after_distilled():
                    self.sessions.append(session, {"type": "distill_deferred"})
                self.sessions.append(session, {"type": "closed"})

    def _run_ask(
        self,
        session: Session,
        text: str,
        on_event: Callable[[str], None] | None,
        on_delta: Callable[[str], None] | None,
        on_progress: Callable[[str], None] | None,
        cancel: Event | None,
        latency: TurnLatency,
        on_user_message: Callable[[str], None] | None = None,
        on_thinking: Callable[[ThinkingUpdate], None] | None = None,
    ) -> AgentReply:
        # Settings can change while this turn waits on a model or tool.
        model = self.model
        context = copy(self.context)
        config = context.config = replace(self.config)
        recall_queries = self.recall_queries

        def progress(phase: str) -> None:
            if cancel is not None and cancel.is_set():
                raise GenerationCancelled("Stopped by user")
            if on_progress is not None:
                on_progress(phase)
            if cancel is not None and cancel.is_set():
                raise GenerationCancelled("Stopped by user")

        message_id = new_message_id()
        self.sessions.append(
            session,
            {"type": "message", "id": message_id, "role": "user", "content": text},
        )
        if on_user_message is not None:
            # 立即上报本条用户消息 ID；生成期间 /undo 用它精确撤销当前回合。
            on_user_message(message_id)

        progress("Preparing context")
        recall_events: list[str] = []
        started = time.monotonic()
        latency.event("recall.started")
        memory_context = self._auto_recall(session, text, config, recall_queries, recall_events.append)
        latency.event("recall.finished", elapsed_ms=elapsed_ms(started))
        if cancel is not None and cancel.is_set():
            raise GenerationCancelled("Stopped by user")
        started = time.monotonic()
        latency.event("context.started")
        plan = context.prepare(
            session,
            self._system_prompt(),
            text,
            current_id=message_id,
            memory_context=memory_context,
        )
        latency.event("context.finished", elapsed_ms=elapsed_ms(started),
                      estimated_tokens=plan.estimated_tokens, compacted=plan.compacted)
        self.last_plan = plan
        self.last_plan_session = session.id
        events: list[str] = []

        def emit(event: str) -> None:
            # 事件实时通知界面，同时保留在 reply.events 里供测试与调用方复用。
            events.append(event)
            if on_event is not None:
                on_event(event)

        for event in recall_events + plan.events:
            emit(event)
        if plan.flushed:
            flushed = plan.flushed
            if flushed.get("error"):
                emit(f"Memory distillation failed: {flushed['error']}")
            elif flushed.get("admitted") or flushed.get("queued"):
                emit(
                    f"Distilled before compaction: {flushed.get('admitted', 0)} written, "
                    f"{flushed.get('queued', 0)} pending"
                )

        tools = build_tools(self.service, session, timeout=config.tool_timeout,
                            file_paths=document_paths(text))
        messages = plan.messages
        usage = Usage()
        steps = 0
        final = ""
        completed = False

        def request_model(schemas: list[dict]) -> AssistantTurn:
            with latency.model_step() as started:
                try:
                    turn = self._chat_with_retry(
                        model, messages, schemas, on_delta=on_delta, cancel=cancel, on_thinking=on_thinking,
                    )
                except GenerationCancelled:
                    raise
                except ChatModelError as exc:
                    if reasoning := getattr(exc, "partial_reasoning", ""):
                        self._append_assistant(session, AssistantTurn(
                            content=getattr(exc, "partial_content", "") + "\n\n*(generation interrupted)*",
                            reasoning_content=reasoning,
                        ), on_thinking, interrupted=True)
                    raise
                latency.event("model.finished", ok=True, elapsed_ms=elapsed_ms(started),
                              prompt_tokens=turn.usage.prompt_tokens,
                              completion_tokens=turn.usage.completion_tokens,
                              tool_count=len(turn.tool_calls))
                return turn

        while steps < config.max_tool_steps:
            progress("Waiting for model")
            turn = request_model(tools.schemas())
            usage.add(turn.usage)
            steps += 1
            record = self._append_assistant(session, turn, on_thinking)
            messages.append(self._message_from_record(record))
            if not turn.tool_calls:
                final = turn.content
                completed = True
                break
            started = time.monotonic()
            latency.event("tools.started", tool_count=len(turn.tool_calls))
            results = _tool_results(tools, turn.tool_calls, on_progress, cancel, user_text=text)
            for index, (call, outcome) in enumerate(results, 1):
                latency.event("tool.finished", tool_index=index, ok=outcome.ok,
                              elapsed_ms=outcome.duration_ms)
                content = truncate_to_tokens(outcome.content, 2000)
                self.sessions.append(
                    session,
                    {
                        "type": "tool",
                        "id": new_message_id(),
                        "tool_call_id": call.id,
                        "name": call.name,
                        "ok": outcome.ok,
                        "duration_ms": outcome.duration_ms,
                        "content": content,
                    },
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": content}
                )
                emit(outcome.display)
            # 工具结果预算随上下文窗口走；200K 窗口下 8K 会过早丢证据。
            compact_tool_outputs(messages, max_tokens=max(8000, config.context_window // 4))
            latency.event("tools.finished", elapsed_ms=elapsed_ms(started))

        if not completed:
            progress("Generating final answer")
            messages.append(
                {
                    "role": "user",
                    "content": "已达到工具调用上限。请基于以上信息直接给出最终回答，不要再调用工具。",
                    **({"_tool_wrap_up": True} if isinstance(model, LiteLLMChatModel) else {}),
                }
            )
            turn = request_model([])
            usage.add(turn.usage)
            steps += 1
            self._append_assistant(session, turn, on_thinking)
            final = turn.content or "(no answer generated)"
            emit("Tool-step limit reached; forced wrap-up")

        return AgentReply(
            content=final,
            events=events,
            steps=steps,
            compacted=plan.compacted,
            usage=usage,
            estimated_tokens=plan.estimated_tokens,
        )

    def force_compact(self) -> list[str]:
        if self.session is None:
            return ["This session has no records to compact."]
        plan = self.context.prepare(
            self.current,
            self._system_prompt(),
            "",
            force=True,
        )
        events = list(plan.events)
        if plan.flushed and plan.flushed.get("admitted") is not None:
            events.append(
                f"Distilled before compaction: {plan.flushed.get('admitted', 0)} written, "
                f"{plan.flushed.get('queued', 0)} pending"
            )
        if not events:
            events.append("No earlier records to compact.")
        return events

    def close(self, distill: bool = True) -> dict:
        empty = {"admitted": 0, "queued": 0, "duplicates": 0, "blocked": 0, "error": None}
        if self.session is None or self._closed:
            return empty
        session = self.session
        self._closed = True
        stats = dict(empty)
        if session is self._active_ask:
            # 该会话仍有请求在写：不在此处落标记/抽取，延迟到请求结束（ask finally）
            # 补打，保证 closed 排在最后的 assistant 记录之后；沉淀由启动补记兜底。
            self._deferred_close = session
            return stats
        # 游标感知：只抽取上次成功沉淀之后的新内容，反复切换不重复抽取。
        user_texts = session.distillation_texts()
        if not distill:
            # 切换会话走快速路径：只做记账，沉淀延后（标记供补沉淀拾取）。
            if user_texts:
                self.sessions.append(session, {"type": "distill_deferred"})
            self.sessions.append(session, {"type": "closed"})
            return stats
        if self.config.extract_on_exit and user_texts:
            joined = "\n---\n".join(user_texts)
            try:
                # 分块抽完再推进游标；直接截断会把最旧的内容永久跳过。
                for chunk in split_chunks(joined, max_chars=DISTILL_MAX_CHARS):
                    result = self.service.memorize(
                        chunk,
                        source=f"session:{session.id}:exit",
                        session_id=session.id,
                        message_id=session.last_user_message_id(),
                    )
                    stats["admitted"] += len(result.admitted)
                    stats["queued"] += len(result.queued)
                    stats["duplicates"] += len(result.duplicates)
                    stats["blocked"] += len(result.blocked)
                self.sessions.append(
                    session,
                    {"type": "distilled", "up_to": session.last_user_message_id()},
                )
            except Exception as exc:
                stats["error"] = str(exc)
                # 留下失败标记：启动补沉淀据此识别"有 closed 但没沉淀成"的会话。
                self.sessions.append(
                    session, {"type": "extraction_error", "error": str(exc)[:200]}
                )
        self.sessions.append(session, {"type": "closed"})
        return stats

    def consolidate_pending(self, limit: int = 3, min_new_messages: int = 1) -> dict:
        """补沉淀：抽取异常终止、退出抽取失败或切换时延后的会话。

        游标之后新用户消息不足 min_new_messages 的会话跳过（标记保留，零成本
        攒批），避免频繁切换触发大量小抽取。当前活跃会话不参与。
        """
        result = {"scanned": 0, "consolidated": 0, "admitted": 0, "queued": 0, "errors": []}
        if not self.config.extract_on_exit:
            return result
        exclude = self.session.id if self.session is not None else None
        for session in self.sessions.unconsolidated(
            limit, exclude=exclude, min_new_messages=min_new_messages
        ):
            if self.session is not None and session.id == self.session.id:
                # 后台补记期间用户可能切进这个会话；活跃会话不在后台追加记录。
                continue
            result["scanned"] += 1
            texts = session.distillation_texts()
            if not texts:
                # 空会话没有可抽取内容，直接标记收尾，避免每次启动都重复扫描。
                self.sessions.append(session, {"type": "consolidated"})
                continue
            if len(texts) < min_new_messages:
                continue
            if texts:
                joined = "\n---\n".join(texts)
                try:
                    for chunk in split_chunks(joined, max_chars=DISTILL_MAX_CHARS):
                        extracted = self.service.memorize(
                            chunk,
                            source=f"session:{session.id}:catchup",
                            session_id=session.id,
                            message_id=session.last_user_message_id(),
                        )
                        result["admitted"] += len(extracted.admitted)
                        result["queued"] += len(extracted.queued)
                except Exception as exc:
                    result["errors"].append(f"{session.id}: {exc}")
                    self.sessions.append(
                        session, {"type": "extraction_error", "error": str(exc)[:200]}
                    )
                    continue
                self.sessions.append(
                    session,
                    {"type": "distilled", "up_to": session.last_user_message_id()},
                )
            self.sessions.append(session, {"type": "consolidated"})
            result["consolidated"] += 1
        return result

    def status(self) -> dict:
        session = self.current
        summary = session.latest_summary() or {}
        return {
            "session_id": session.id,
            "title": session.title(),
            "model": self.model_label,
            "retrieval": self.service.retrieval_mode,
            "messages": len(session.messages),
            "records": len(session.records),
            "summaries": sum(1 for record in session.records if record.get("type") == "summary"),
            "summary_chars": len(session.summary_text()),
            "covered_records": session.covered_records(),
            "estimated_tokens": session.estimated_tokens(),
            "context_window": self.config.context_window,
            "pending": len(self.service.pending()),
            "last_summary_at": summary.get("at", ""),
        }

    def summary_text(self) -> str:
        return self.current.summary_text()

    def context_usage(self) -> int:
        """当前会话的上下文占用；无会话或尚未发送消息时为 0。"""
        if self.session is None:
            return 0
        return self.context.estimate_usage(self.session, self._system_prompt())

    def rename(self, title: str) -> None:
        self.sessions.rename(self.current, title)

    def undo(
        self,
        session: Session | None = None,
        message_id: str | None = None,
    ) -> str | None:
        """撤销指定会话的一轮用户消息；默认当前会话最后一条用户消息。

        传入 message_id 时只撤销该条，避免生成期间切换会话后误伤其他回合。
        """
        target_session = session or self.current
        records = target_session.active_records()
        if message_id:
            target = next(
                (
                    record
                    for record in records
                    if record.get("id") == message_id
                    and record.get("type") == "message"
                    and record.get("role") == "user"
                ),
                None,
            )
        else:
            target = next(
                (
                    record
                    for record in reversed(records)
                    if record.get("type") == "message"
                    and record.get("role") == "user"
                ),
                None,
            )
        if target is None:
            return None
        self.sessions.append(
            target_session,
            {
                "type": "undo",
                "id": new_message_id(),
                "message_id": target["id"],
            },
        )
        if self.session is not None and target_session.id == self.session.id:
            self.last_plan = None
            self.last_plan_session = None
        return str(target.get("content") or "")

    def _system_prompt(self) -> str:
        """每轮读 vault/SOUL.md，人设改动下一轮即生效；拼接规则见 prompts.py。"""
        prompt = build_system_prompt(load_persona(self.service.config.vault))
        if self.service.config.search.enabled:
            prompt += "\n联网能力已启用：search 搜索公开信息，HTTP 传输下 fetch_web 可读取公开网页。用户提供网页时应尝试可用工具，不要声称没有联网能力。实际可调用能力以本轮工具定义为准。"
        # 联网结果常只给到月/日；给出当前日期，模型才能判断“最新”是否成立。
        return f"{prompt}\n\n当前日期：{date.today().isoformat()}"

    def _auto_recall(
        self, session: Session, text: str, config: ChatConfig, recall_queries: RecallQueryBuilder,
        on_event: Callable[[str], None] | None = None,
    ) -> str:
        if config.auto_recall <= 0:
            return ""
        # 显式绑定请求的会话：进行中切换会话时不得读错当前会话的近期消息。
        recent = session.user_texts()[:-1]
        if not should_auto_recall(text, recent):
            return ""
        field = direct_recall_field(text)
        exact = self.service.recall_exact_field(field) if field else None
        if exact is not None:
            hits = [exact]
        else:
            recall = recall_queries.build(text, recent)
            limit = config.auto_recall
            if recall.cue or recall.rewrite_timed_out:
                limit = max(limit, 8)
            if recall.original and recall.original != recall.text:
                hits = self.service.recall(recall.text, limit, original_query=recall.original)
            else:
                hits = self.service.recall(recall.text, limit)
        text = memory_references(hits, max_tokens=min(2400, config.context_window // 8))
        try:
            profile = self.service.reflection.context({hit.id for hit in hits})
        except (ValueError, OSError):
            # 独立推断日志故障不能阻断事实召回与聊天，也不把损坏内容送入模型。
            profile = ""
            if on_event is not None:
                on_event("画像读取失败，本轮仅使用事实记忆；请检查推断日志。")
        return text + ("\n\n" + profile if profile else "")

    def _flush_memories(self, texts: list[str], session: Session) -> dict:
        # 沉淀来源绑定发起压缩的会话：prepare 期间切走会话不得关联错来源。
        joined = "\n---\n".join(texts)
        totals = {"admitted": 0, "queued": 0, "duplicates": 0, "blocked": 0}
        for chunk in split_chunks(joined, max_chars=DISTILL_MAX_CHARS):
            result = self.service.memorize(
                chunk,
                source=f"session:{session.id}:compact",
                session_id=session.id,
                message_id=session.last_user_message_id(),
            )
            for key in totals:
                totals[key] += len(getattr(result, key))
        return totals

    def _append_assistant(
        self, session: Session, turn: AssistantTurn,
        on_thinking: Callable[[ThinkingUpdate], None] | None = None, *, interrupted: bool = False,
    ) -> dict:
        record = {
            "type": "assistant",
            "id": new_message_id(),
            "content": turn.content,
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in turn.tool_calls
            ],
        }
        if interrupted:
            record["interrupted"] = True
        if turn.reasoning_content or turn.reasoning_fragments:
            record["reasoning"] = {
                "text": turn.reasoning_content, "fragments": turn.reasoning_fragments,
                "source": turn.reasoning_source, "seconds": turn.reasoning_seconds,
            }
        saved = self.sessions.append(session, record)
        if on_thinking is not None and turn.reasoning_content:
            reasoning = saved["reasoning"]
            on_thinking(ThinkingUpdate(reasoning["text"], saved["id"], reasoning["seconds"]))
        return saved

    @staticmethod
    def _message_from_record(record: dict) -> dict:
        from .context import record_to_message

        return record_to_message(record) or {"role": "assistant", "content": ""}

    def _chat_with_retry(
        self,
        model: ChatModel,
        messages: list[dict],
        tools: list[dict],
        on_delta: Callable[[str], None] | None = None,
        cancel: Event | None = None,
        on_thinking: Callable[[ThinkingUpdate], None] | None = None,
    ) -> AssistantTurn:
        native_adapter = isinstance(model, LiteLLMChatModel)
        if not native_adapter and any("reasoning_source" in message for message in messages):
            messages = [
                {key: value for key, value in message.items() if key not in {"reasoning_source", "reasoning_content"}}
                for message in messages
            ]
        if on_delta is not None or on_thinking is not None:
            # 流式不做循环级重试：中断重发会让界面收到重复 delta。
            parts: list[str] = []
            thoughts: list[str] = []

            def delta(text: str) -> None:
                if cancel is not None and cancel.is_set():
                    raise GenerationCancelled("Stopped by user")
                parts.append(text)
                if on_delta is not None:
                    on_delta(text)

            def reasoning(text: str) -> None:
                if cancel is not None and cancel.is_set():
                    raise GenerationCancelled("Stopped by user")
                thoughts.append(text)
                if on_thinking is not None:
                    on_thinking(ThinkingUpdate(text))

            try:
                kwargs = {"cancel": cancel, "on_reasoning": reasoning} if native_adapter else {}
                turn = model.chat(messages, tools, on_delta=delta, **kwargs)
                if cancel is not None and cancel.is_set():
                    raise GenerationCancelled("Stopped by user")
                return turn
            except ChatModelError as exc:
                exc.partial_content = "".join(parts)
                exc.partial_reasoning = "".join(thoughts)
                raise
            except Exception as exc:
                error = ChatModelError(f"LLM call failed: {exc}")
                error.partial_content = "".join(parts)
                error.partial_reasoning = "".join(thoughts)
                raise error from exc
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return model.chat(messages, tools)
            except ChatModelError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        raise ChatModelError(f"LLM call failed: {last_error}")
