from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from copy import copy
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from itertools import groupby, islice
from threading import Event

from ..infra.config import ChatConfig, Config, LLMConfig, load_config
from ..infra.llm import (
    AssistantTurn,
    ChatModel,
    ChatModelError,
    GenerationCancelled,
    LiteLLMChatModel,
    ToolCall,
    Usage,
)
from ..infra.llm.client import chat_history, effective_context_window
from ..infra.observability.latency import TurnLatency, elapsed_ms, track_turn
from ..infra.security.secrets import redact_secrets
from ..infra.session import Session, SessionStore, new_message_id
from ..infra.session.attachments import AttachmentBatch
from ..infra.storage.database import on_commit, transaction
from ..infra.vault.files import load_persona
from ..memory.background.titles import rename_session
from ..memory.service import MemoryService
from ..memory.turn_undo import TurnJournal, TurnUndoError
from .context import (
    CompactionPhase,
    ContextManager,
    ContextPlan,
    estimate_tool_tokens,
    record_to_message,
)
from .evidence import RecallContext
from .file_tools import document_paths
from .insertion import TurnInsertion
from .prompts import build_system_prompt
from .recall import (
    RecallQueryBuilder,
    direct_recall_field,
    memory_references,
    should_auto_recall,
)
from .summarizer import LLMSummarizer, Summarizer
from .tools import (
    PARALLEL_NETWORK_LIMIT,
    PARALLEL_TOOLS,
    ToolOutcome,
    ToolRegistry,
    build_tools,
    cancelled_tool,
)


@dataclass(frozen=True)
class ThinkingUpdate:
    text: str
    record_id: str | None = None
    seconds: float | None = None


def _tool_results(
    tools: ToolRegistry,
    calls: list[ToolCall],
    on_progress: Callable[[str], None] | None,
    cancel: Event | None,
    *,
    user_text: str | None = None,
) -> Iterator[tuple[ToolCall, ToolOutcome]]:
    def ready(call: ToolCall) -> bool:
        if on_progress is not None and not (cancel and cancel.is_set()):
            on_progress(f"Calling tool: {call.name}")
        return not (cancel and cancel.is_set())

    def kind(call: ToolCall) -> str:
        if call.name in PARALLEL_TOOLS:
            return "network"
        return "serial"

    for batch_kind, group in groupby(calls, key=kind):
        if batch_kind == "serial":
            for call in group:
                yield (
                    call,
                    tools.execute(call.name, call.arguments) if ready(call) else cancelled_tool(),
                )
            continue
        # 遇到非网络工具调用时，先完成之前的调用，并按模型给出的次序关联结果。
        while batch := list(islice(group, PARALLEL_NETWORK_LIMIT)):
            waits = [
                tools.start(call.name, call.arguments, cancel=cancel, shared_deadline=True)
                if ready(call)
                else cancelled_tool
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


@dataclass(frozen=True)
class TurnSettings:
    model: ChatModel
    llm_config: LLMConfig
    context: ContextManager
    recall_queries: RecallQueryBuilder


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
        self.model_config = service.config.llm
        self.sessions = sessions
        self.config = config or service.config.chat
        self.recall_queries = RecallQueryBuilder(
            service.config.llm,
            rewrite=self.config.recall_rewrite,
            context_turns=self.config.recall_context_turns,
            rewrite_timeout=self.config.rewrite_timeout,
        )
        self.on_distillation: Callable[[str, str | None], None] | None = None
        self.on_title_needed: Callable[[str], None] | None = None
        self.summarizer = summarizer or LLMSummarizer(service.config.llm)
        self.context = ContextManager(
            self.config,
            self.summarizer,
            sessions,
            request_distillation=self._request_distillation,
            model_config=model.config if isinstance(model, LiteLLMChatModel) else None,
        )
        self.session: Session | None = None
        self.last_plan = None
        self.last_plan_session: str | None = None
        self._meter_tools: tuple[tuple[dict, dict], int] | None = None
        self._closed = False
        # 请求进行中的会话与被延迟打关闭标记的会话（切换不阻塞生成）。
        self._active_ask: Session | None = None
        self._deferred_close: Session | None = None
        self.allow_local_paths = True

    def start(self) -> None:
        """进入新对话。会话文件在第一条消息发送时才创建。"""
        self.session = None
        self.last_plan = None
        self.last_plan_session = None
        self._closed = False

    def turn_settings(self) -> TurnSettings:
        context = copy(self.context)
        window = effective_context_window(self.service.config.llm, self.config.context_window)
        context.config = replace(self.config, context_window=window)
        return TurnSettings(self.model, self.model_config, context, self.recall_queries)

    def ensure_session(self) -> Session:
        if self.session is None:
            self.session = self.sessions.create()
            self._closed = False
        return self.session

    @property
    def has_session(self) -> bool:
        return self.session is not None

    def resume(self, session_id: str, *, close_current: bool = False) -> Session:
        with transaction(self.service.paths):
            if close_current and self.session is not None and self.session.id == session_id:
                return self.session
            active = self._active_ask
            session = (
                active
                if active is not None and active.id == session_id
                else self.sessions.load_snapshot(session_id)
            )
            if close_current:
                self.close(distill=False)
            if self._deferred_close is session:
                self._deferred_close = None
            self.session = session
            self.last_plan = None
            self.last_plan_session = None
            self._closed = False
            return session

    def reload_llm(self, config: "Config | None" = None) -> None:
        """配置变更后热更新聊天模型和摘要器，不重启会话。

        默认从 service 记住的配置路径重读；调用方持有新配置对象时直接传入，
        避免落盘与重读之间的竞态把切换回滚。
        """
        if config is None:
            path = self.service.config_path
            config = load_config(path) if path else load_config()
        self.service.config.llm = config.llm
        self.service.config.chat = config.chat
        self.model = LiteLLMChatModel(config.llm)
        self.model_config = config.llm
        self.summarizer = LLMSummarizer(config.llm)
        self.config = config.chat
        self.recall_queries = RecallQueryBuilder(
            config.llm,
            rewrite=config.chat.recall_rewrite,
            context_turns=config.chat.recall_context_turns,
            rewrite_timeout=config.chat.rewrite_timeout,
        )
        self.context.model_config = config.llm
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
        turn_settings: TurnSettings | None = None,
        attachments: AttachmentBatch | None = None,
        replace_message_id: str | None = None,
        on_session: Callable[[Session], None] | None = None,
        on_user_message: Callable[[str], None] | None = None,
        on_event: Callable[[str], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_thinking: Callable[[ThinkingUpdate], None] | None = None,
        on_progress: Callable[[str], None] | None = None,
        on_tool: Callable[[bool], None] | None = None,
        on_compaction: Callable[[CompactionPhase], None] | None = None,
        cancel: Event | None = None,
        insertion: TurnInsertion | None = None,
        on_inserted: Callable[[str, str], None] | None = None,
    ) -> AgentReply:
        """执行一轮对话。

        session 显式传入时（后台/排队任务）所有读写都绑定该会话，不触碰
        self.session——切换会话不影响进行中的请求；否则使用当前会话。
        """
        text = redact_secrets(text.strip())
        if not text:
            raise ValueError("消息为空。")
        settings = turn_settings or self.turn_settings()
        with transaction(self.service.paths):
            if session is None:
                session = self.ensure_session()
            self._active_ask = session
        try:
            if on_session is not None:
                on_session(session)
            with track_turn(cancel) as latency:
                return self._run_ask(
                    session,
                    text,
                    on_event,
                    on_delta,
                    on_progress,
                    cancel,
                    latency,
                    on_user_message,
                    on_thinking,
                    on_tool,
                    on_compaction,
                    insertion,
                    on_inserted,
                    attachments,
                    settings=settings,
                    replace_message_id=replace_message_id,
                )
        except GenerationCancelled as exc:
            partial = getattr(exc, "partial_content", "")
            self._append_assistant(
                session,
                AssistantTurn(
                    content=partial + "\n\n*(generation stopped by user)*",
                    reasoning_content=getattr(exc, "partial_reasoning", ""),
                ),
                on_thinking,
                interrupted=True,
            )
            raise
        finally:
            with transaction(self.service.paths):
                self._active_ask = None
                deferred = self._deferred_close
                self._deferred_close = None
                if deferred is session:
                    self._request_distillation(session.id, session.last_user_message_id())
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
        on_tool: Callable[[bool], None] | None = None,
        on_compaction: Callable[[CompactionPhase], None] | None = None,
        insertion: TurnInsertion | None = None,
        on_inserted: Callable[[str, str], None] | None = None,
        attachments: AttachmentBatch | None = None,
        *,
        settings: TurnSettings,
        replace_message_id: str | None = None,
    ) -> AgentReply:
        model, context, recall_queries = settings.model, settings.context, settings.recall_queries
        config = context.config

        def progress(phase: str) -> None:
            if cancel is not None and cancel.is_set():
                raise GenerationCancelled("Stopped by user")
            if on_progress is not None:
                on_progress(phase)
            if cancel is not None and cancel.is_set():
                raise GenerationCancelled("Stopped by user")

        message_id = new_message_id()
        journal = TurnJournal(self.service.paths, session.id, message_id)
        record = {"type": "message", "id": message_id, "role": "user", "content": text}
        if attachments:
            from ..infra.session.attachment_objects import release, reserve

            token = message_id
            reserve(self.service.paths, token, attachments.files)
            record["attachments"] = [asdict(item) for item in attachments.files]
        try:
            if attachments:
                attachments.publish(self.service.paths)
            with transaction(self.service.paths):
                if replace_message_id is None:
                    self.sessions.append(session, record)
                else:
                    self.undo(session, expected_latest=replace_message_id, replacement=record)
                if attachments:
                    release(self.service.paths, token)
        except BaseException:
            if attachments:
                release(self.service.paths, token)
                attachments.discard()
            raise
        if attachments:
            attachments.finish()
        if on_user_message is not None:
            # 立即上报本条用户消息 ID；生成期间 /undo 用它精确撤销当前回合。
            on_user_message(message_id)

        progress("Preparing context")
        recall_events: list[str] = []
        started = time.monotonic()
        latency.event("recall.started")
        memory_context = self._auto_recall(
            session, text, config, recall_queries, recall_events.append
        )
        latency.event("recall.finished", elapsed_ms=elapsed_ms(started))
        if cancel is not None and cancel.is_set():
            raise GenerationCancelled("Stopped by user")
        started = time.monotonic()
        latency.event("context.started")
        paths = (
            attachments.documents()
            if attachments
            else document_paths(text)
            if self.allow_local_paths
            else []
        )
        evidence = RecallContext(self.service)
        tools = build_tools(
            self.service,
            session,
            timeout=config.tool_timeout,
            file_paths=paths,
            evidence=evidence,
            llm_config=settings.llm_config,
        )
        schema_tokens = estimate_tool_tokens(tools.schemas())
        if not paths:
            self._meter_tools = self._tool_settings(), schema_tokens
        system_prompt = self._system_prompt()
        user_text = text
        current_input = attachments.model_content(text) if attachments else text
        additional_inputs: list[dict] = []

        def prepare_context(
            tool_tokens: int, active_plan: ContextPlan | None = None
        ) -> ContextPlan:
            prepared = context.prepare(
                session,
                system_prompt,
                current_input,
                current_id=message_id,
                memory_context=memory_context,
                on_compaction=on_compaction,
                tool_tokens=tool_tokens,
                active_plan=active_plan,
                additional_inputs=additional_inputs,
            )
            if prepared is not active_plan or attachments:
                self.last_plan = prepared
                if attachments:
                    # 保留用户原文，避免缓存图片字节。
                    cached = [
                        {**msg, "content": user_text} if msg["content"] is current_input else msg
                        for msg in prepared.messages
                    ]
                    self.last_plan = replace(prepared, messages=cached)
                self.last_plan_session = session.id
            return prepared

        plan = prepare_context(schema_tokens)
        latency.event(
            "context.finished",
            elapsed_ms=elapsed_ms(started),
            estimated_tokens=plan.estimated_tokens,
            compacted=plan.compacted,
        )
        events: list[str] = []

        def emit(event: str) -> None:
            # 事件实时通知界面，同时保留在 reply.events 里供测试与调用方复用。
            events.append(event)
            if on_event is not None:
                on_event(event)

        for event in recall_events + plan.events:
            emit(event)
        usage = Usage()
        steps = 0
        final = ""
        completed = False
        needs_wrap_up = True

        def request_model(schemas: list[dict]) -> AssistantTurn:
            nonlocal plan
            if steps:
                prepared = prepare_context(estimate_tool_tokens(schemas), plan)
                if prepared is not plan:
                    plan = prepared
                    evidence.invalidate()
                    for event in plan.events:
                        emit(event)
            evidence.begin_model_request()
            with latency.model_step() as started:
                try:
                    turn = self._chat_with_retry(
                        model,
                        plan.messages,
                        schemas,
                        on_delta=on_delta,
                        cancel=cancel,
                        on_thinking=on_thinking,
                    )
                except GenerationCancelled:
                    raise
                except ChatModelError as exc:
                    reasoning = getattr(exc, "partial_reasoning", "")
                    partial = getattr(exc, "partial_content", "")
                    if reasoning or partial:
                        self._append_assistant(
                            session,
                            AssistantTurn(
                                content=partial + "\n\n*(generation interrupted)*",
                                reasoning_content=reasoning,
                            ),
                            on_thinking,
                            interrupted=True,
                        )
                    raise
                latency.event(
                    "model.finished",
                    ok=True,
                    elapsed_ms=elapsed_ms(started),
                    prompt_tokens=turn.usage.prompt_tokens,
                    completion_tokens=turn.usage.completion_tokens,
                    tool_count=len(turn.tool_calls),
                )
                return turn

        def apply_insertion() -> bool:
            nonlocal text, tools
            pending = insertion.pending if insertion is not None else None
            if pending is None or (cancel is not None and cancel.is_set()):
                return False
            supplement = redact_secrets(pending.text)
            candidate = {"role": "user", "content": supplement}
            # 每次输入只授权访问该次输入提供的文件路径。
            next_tools = build_tools(
                self.service,
                session,
                timeout=config.tool_timeout,
                file_paths=document_paths(supplement) if self.allow_local_paths else [],
                evidence=evidence,
                llm_config=settings.llm_config,
            )
            next_schemas = next_tools.schemas() if steps < config.max_tool_steps else []
            if not context.fits_input([*plan.messages, candidate], next_schemas):
                insertion.settle()
                return False
            text = supplement
            message = self.sessions.append(
                session,
                {"type": "message", "id": new_message_id(), "turn_root": message_id, **candidate},
            )
            additional_inputs.append(candidate)
            plan.messages.append(self._message_from_record(message))
            insertion.settle(applied=True)
            if on_inserted is not None:
                on_inserted(message["id"], text)
            evidence.invalidate()
            tools = next_tools
            return True

        while steps < config.max_tool_steps:
            progress("Waiting for model")
            if insertion is None:
                turn = request_model(tools.schemas())
            else:
                with insertion.model_request():
                    turn = request_model(tools.schemas())
            usage.add(turn.usage)
            steps += 1
            record = self._append_assistant(session, turn, on_thinking)
            plan.messages.append(self._message_from_record(record))
            if not turn.tool_calls:
                if apply_insertion():
                    needs_wrap_up = False
                    continue
                final = turn.content
                completed = True
                break
            needs_wrap_up = True
            started = time.monotonic()
            latency.event("tools.started", tool_count=len(turn.tool_calls))
            results = self._journaled_tool_results(
                journal, tools, turn.tool_calls, on_progress, cancel, text
            )
            for index, (call, outcome) in enumerate(results, 1):
                latency.event(
                    "tool.finished", tool_index=index, ok=outcome.ok, elapsed_ms=outcome.duration_ms
                )
                saved = self.sessions.append(
                    session,
                    {
                        "type": "tool",
                        "id": new_message_id(),
                        "tool_call_id": call.id,
                        "name": call.name,
                        "ok": outcome.ok,
                        "duration_ms": outcome.duration_ms,
                        "content": outcome.content,
                        **({"turn_undo_recorded": True} if journal.has_effects() else {}),
                    },
                )
                plan.messages.append(self._message_from_record(saved))
                # 紧接着结果状态发送对应文本，界面即可将二者配对，
                # 无需解析消息正文。
                if on_tool is not None:
                    on_tool(outcome.ok)
                emit(outcome.display)
            latency.event("tools.finished", elapsed_ms=elapsed_ms(started))
            apply_insertion()

        if not completed:
            progress("Generating final answer")
            if needs_wrap_up:
                wrap_up = {
                    "role": "user",
                    "content": "已达到工具调用上限。请基于以上信息直接给出最终回答，不要再调用工具。",
                    **({"_tool_wrap_up": True} if isinstance(model, LiteLLMChatModel) else {}),
                }
                additional_inputs.append(wrap_up)
                plan.messages.append(wrap_up)
            turn = request_model([])
            usage.add(turn.usage)
            steps += 1
            self._append_assistant(session, turn, on_thinking)
            final = turn.content
            if needs_wrap_up:
                emit("Tool-step limit reached; forced wrap-up")

        if not final.strip():
            raise ChatModelError(
                "The model returned no visible answer. Retry or select another model."
            )

        if self.on_title_needed is not None and not session.saved_title():
            try:
                self.on_title_needed(session.id)
            except Exception as exc:
                from ..infra.observability.diagnostics import record_event

                record_event("session.title_failed", exc)
        return AgentReply(
            content=final,
            events=events,
            steps=steps,
            compacted=plan.compacted,
            usage=usage,
            estimated_tokens=plan.estimated_tokens,
        )

    @staticmethod
    def _journaled_tool_results(journal, tools, calls, on_progress, cancel, text):
        with journal.capture():
            yield from _tool_results(tools, calls, on_progress, cancel, user_text=text)

    def force_compact(
        self, on_compaction: Callable[[CompactionPhase], None] | None = None
    ) -> list[str]:
        if self.session is None:
            return ["This session has no records to compact."]
        context = self.turn_settings().context
        plan = context.prepare(
            self.current,
            self._system_prompt(),
            "",
            on_compaction=on_compaction,
            tool_tokens=self._ordinary_tool_tokens(self.current),
        )
        if plan.compacted:
            return plan.events
        if plan.estimated_tokens < context.trigger_tokens:
            return [
                f"Context usage is {plan.estimated_tokens:,} tokens. "
                f"Compaction starts at {context.trigger_tokens:,} tokens."
            ]
        return ["There is no new conversation history to summarize."]

    def _request_distillation(self, session_id: str, through: str | None) -> None:
        if self.on_distillation is not None and through is not None:
            try:
                self.on_distillation(session_id, through)
            except Exception as exc:
                from ..infra.observability.diagnostics import record_event

                record_event("catchup.failed", exc)

    def close(self, distill: bool = True) -> dict:
        """将剩余工作加入队列，切换或退出时无需等待记忆提取。"""
        result = {"admitted": 0, "queued": 0, "duplicates": 0, "blocked": 0, "error": None}
        if self.session is None or self._closed:
            return result
        session = self.session
        if self._active_ask is not None and session.id == self._active_ask.id:
            self._deferred_close = self._active_ask
        else:
            self.sessions.append(session, {"type": "closed"})
            if not distill or self.config.extract_on_exit:
                self._request_distillation(session.id, session.last_user_message_id())
        self._closed = True
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
        return self.estimate_usage(self.session)

    def estimate_usage(
        self,
        session: Session,
        context: ContextManager | None = None,
        *,
        active_message_id: str | None = None,
    ) -> int:
        return (context or self.context).estimate_usage(
            session,
            self._system_prompt(),
            self._ordinary_tool_tokens(session),
            active_message_id=active_message_id,
        )

    def _ordinary_tool_tokens(self, session: Session) -> int:
        # 附件和文件工具的授权仅对单次输入有效。空闲时，
        # 用下一次普通请求估算会话的上下文用量。
        settings = self._tool_settings()
        cached = self._meter_tools
        if cached is None or cached[0] != settings:
            tools = build_tools(self.service, session, timeout=self.config.tool_timeout)
            cached = self._meter_tools = settings, estimate_tool_tokens(tools.schemas())
        return cached[1]

    def _tool_settings(self) -> tuple[dict, dict]:
        config = self.service.config
        return asdict(config.search), asdict(config.fetch)

    def rename(self, title: str) -> None:
        rename_session(self.sessions, self.current, title)

    def undo(
        self,
        session: Session | None = None,
        message_id: str | None = None,
        *,
        expected_latest: str | None = None,
        replacement: dict | None = None,
    ) -> str | None:
        """撤销指定会话的一轮用户消息；默认当前会话最后一条用户消息。

        传入 message_id 时校验该条消息所属回合。编辑重发时在同一事务中保存替代消息。
        """
        with transaction(self.service.paths):
            target_session = session or self.current
            # 在追加和回滚使用的同一把锁内重新核对已保存的末尾记录。
            live = self.sessions.load(target_session.id)
            if expected_latest is not None and live.last_user_message_id() != expected_latest:
                raise TurnUndoError(
                    "This message is no longer the latest. Refresh the conversation."
                )
            result = self._undo_locked(live, message_id, replacement)
            on_commit(lambda: setattr(target_session, "records", live.records))
            return result

    def _undo_locked(
        self, target_session: Session, message_id: str | None, replacement: dict | None
    ) -> str | None:
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
                    if record.get("type") == "message" and record.get("role") == "user"
                ),
                None,
            )
        if target is None:
            return None
        inputs = target_session.latest_turn_inputs()
        if not any(record.get("id") == target["id"] for record in inputs):
            raise TurnUndoError("Only the latest turn can be undone.")
        root_id = inputs[0]["id"]
        events = [{"type": "undo", "id": new_message_id(), "message_id": root_id}]
        if replacement is not None:
            events.append(replacement)
        journal = TurnJournal(self.service.paths, target_session.id, root_id)
        with transaction(self.service.paths):
            first = records.index(inputs[0])
            if not journal.has_effects() and any(
                record.get("turn_undo_recorded") is True for record in records[first:]
            ):
                raise TurnUndoError(
                    "This turn's undo records are missing. Restore a complete backup to undo it."
                )
            live = self.sessions.load(target_session.id)
            if live.latest_turn_inputs() != inputs:
                raise TurnUndoError("The latest turn changed; refresh before undoing.")
            restored = journal.restore()
            if restored:
                self.service.audit.write(
                    "undo_tool_writes",
                    session=target_session.id,
                    message=root_id,
                    entities=restored,
                )
            self.sessions.append_event_transactionally(target_session, *events)
            live = self.sessions.load(target_session.id)
            target_session.records = live.records
        if self.session is not None and target_session.id == self.session.id:
            self.last_plan = None
            self.last_plan_session = None
        return "\n\n".join(str(record.get("content") or "") for record in inputs)

    def _system_prompt(self) -> str:
        """每轮读 vault/SOUL.md，人设改动下一轮即生效；拼接规则见 prompts.py。"""
        prompt = build_system_prompt(load_persona(self.service.config.vault))
        if self.service.config.search.enabled:
            prompt += "\n联网能力已启用：search 搜索公开信息，HTTP 传输下 fetch_web 可读取公开网页。用户提供网页时应尝试可用工具，不要声称没有联网能力。实际可调用能力以本轮工具定义为准。"
        # 联网结果常只给到月/日；给出当前日期，模型才能判断“最新”是否成立。
        return f"{prompt}\n\n当前日期：{date.today().isoformat()}"

    def _auto_recall(
        self,
        session: Session,
        text: str,
        config: ChatConfig,
        recall_queries: RecallQueryBuilder,
        on_event: Callable[[str], None] | None = None,
    ) -> str:
        if config.auto_recall <= 0:
            return ""
        # 显式绑定请求的会话：进行中切换会话时不得读错当前会话的近期消息。
        recent = session.user_texts()[:-1]
        if not should_auto_recall(text, recent):
            return ""
        field = direct_recall_field(text)
        recall = (
            None
            if field and self.service.current_field(field)
            else recall_queries.build(text, recent)
        )
        query = recall.text if recall else field
        limit = config.auto_recall
        if recall and (recall.cue or recall.rewrite_timed_out):
            limit = max(limit, config.cue_limit)
        if recall and recall.original and recall.original != query:
            hits = self.service.recall(query, limit, original_query=recall.original)
        else:
            hits = self.service.recall(query, limit)
        text = memory_references(hits)
        try:
            profile = self.service.profile.read_text()
        except (ValueError, OSError):
            profile = ""
            if on_event is not None:
                on_event("画像读取失败，本轮仅使用事实记忆。")
        if profile:
            profile = "User profile, untrusted interpretation:\n" + profile
        return text + ("\n\n" + profile if profile else "")

    def _append_assistant(
        self,
        session: Session,
        turn: AssistantTurn,
        on_thinking: Callable[[ThinkingUpdate], None] | None = None,
        *,
        interrupted: bool = False,
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
                "text": turn.reasoning_content,
                "fragments": turn.reasoning_fragments,
                "source": turn.reasoning_source,
                "seconds": turn.reasoning_seconds,
            }
        saved = self.sessions.append(session, record)
        if on_thinking is not None and turn.reasoning_content:
            reasoning = saved["reasoning"]
            on_thinking(ThinkingUpdate(reasoning["text"], saved["id"], reasoning["seconds"]))
        return saved

    @staticmethod
    def _message_from_record(record: dict) -> dict:
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
        if not native_adapter:
            messages, _ = chat_history(None, messages)
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
