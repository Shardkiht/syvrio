"""定义记忆工具的参数，由 MemoryService 校验并执行所有写入。"""

from __future__ import annotations

import json
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from ..infra.config import DEFAULTS
from ..infra.session.model import Session
from ..memory.extractor import grounded_context
from ..memory.models import MemoryContext, MemoryCorrection, MemoryDraft, TimeRange
from ..memory.service import MemoryService, pending_view
from .evidence import ProposalDecision, ProposalError, RecallContext, emotion_sources
from .registry import ToolOutcome, ToolRegistry, ToolSpec


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class ResolveProposals(Arguments):
    decisions: list[ProposalDecision] = Field(min_length=1)


class Recall(Arguments):
    query: str = Field(min_length=1, description="保留自然问句和相关同义词，例如姓名、名字。")
    limit: int = Field(default=DEFAULTS["retrieval"]["default_limit"], ge=1)
    time_range: TimeRange | None = None
    location: str | None = None
    emotion: str | None = None


class Topic(Arguments):
    topic: str = Field(min_length=1)


class Source(Arguments):
    memory_id: str = Field(min_length=1)
    commit_id: int | None = Field(
        default=None, ge=1, description="需要核对历史来源时提供该版本的提交号。"
    )


class ChatSearch(Arguments):
    query: str = Field(min_length=1, max_length=160)
    limit: int = Field(default=10, ge=1, le=50)


class Target(Arguments):
    entry_id: str = Field(min_length=1)


class Correction(MemoryCorrection):
    entry_id: str = Field(min_length=1)


class Transition(MemoryDraft):
    entry_id: str = Field(min_length=1)
    previous_valid_to: str | None = None


class Pending(MemoryDraft):
    question: str = Field(min_length=1)
    related_ids: list[str] = Field(default_factory=list)


def _schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(node):
        if isinstance(node, list):
            return [inline(element) for element in node]
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(definitions[node["$ref"].rsplit("/", 1)[-1]])
            return {key: inline(value) for key, value in node.items() if key != "title"}
        return node

    return inline(schema)


def register_memory_tools(
    tools: ToolRegistry,
    service: MemoryService,
    get_session: Callable[[], Session],
    evidence: RecallContext,
    timeout: float,
) -> None:
    def register(
        name: str, description: str, model: type[BaseModel], action: Callable, *, write=False
    ):
        def execute(arguments: dict) -> ToolOutcome:
            request = model.model_validate(arguments)
            payload = action(request)
            review = payload.get("status") == "review_required"
            display = f"{name}: review recalled evidence" if review else name
            if name == "recall":
                display = f'Recalled "{request.query}": {len(payload["results"])} hits'
            elif name == "read_topic":
                display = f'Read topic "{payload["topic"]}": {len(payload["entries"])} entries'
            return ToolOutcome(True, json.dumps(payload, ensure_ascii=False), display, data=payload)

        schema = _schema(model)
        if model is Recall:
            schema["properties"]["limit"]["default"] = service.config.retrieval.default_limit
        tools.register(
            ToolSpec(name, description, schema, execute, timeout=timeout, mutating=write)
        )

    register(
        "recall",
        "检索相关正式事实，可使用自然问句和同义词。time_range 匹配事件日期或事实成立的时段。",
        Recall,
        lambda request: {
            "results": [
                entry.model_dump(exclude_none=True)
                for entry in service.recall(
                    **{
                        "limit": service.config.retrieval.default_limit,
                        **request.model_dump(exclude_none=True, exclude_unset=True),
                    }
                )
            ]
        },
    )
    register(
        "search_chats",
        "按一个关键词或连续原文片段搜索其他未归档会话的标题和对话正文。返回原文片段、角色和来源 ID；没有命中时可换词。",
        ChatSearch,
        lambda r: service.session_query.search_chats(
            r.query, r.limit, exclude_session=get_session().id
        ),
    )
    register("read_topic", "按主题读取全部有效事实。", Topic, lambda r: service.read_topic(r.topic))
    register(
        "read_memory_source",
        "按事实 ID 查阅一条原始用户消息，作为只读证据。",
        Source,
        lambda r: service.read_memory_source(r.memory_id, r.commit_id),
    )
    register(
        "review_pending",
        "查看所有等待人工确认的候选和问题。",
        Arguments,
        lambda _: {"pending": [pending_view(item) for item in service.pending()]},
    )

    def mutate(name: str, request: BaseModel) -> dict:
        payload = (
            request.model_dump(exclude_unset=True)
            if name == "correct"
            else request.model_dump(exclude_none=True)
        )
        target = payload.get("entry_id")
        session = get_session()
        message_id = session.last_user_message_id()
        if not message_id:
            raise ValueError("A chat write requires an original user message.")
        source = {"session_id": session.id, "message_id": message_id}
        draft = None
        if name == "correct":
            patch = {k: v for k, v in payload.items() if k != "entry_id"}
            updates = patch.get("context") or {}
            if isinstance(updates.get("emotion"), dict):
                sources = emotion_sources(session, message_id)
                quoted = {
                    k: v
                    for k, v in updates["emotion"].items()
                    if v is None or any(v in source for source in sources)
                }
                if quoted:
                    updates["emotion"] = quoted
                else:
                    updates.pop("emotion")
            draft = MemoryCorrection.model_validate(patch)
        elif "text" in payload:
            draft = MemoryDraft.model_validate(
                {key: value for key, value in payload.items() if key in MemoryDraft.model_fields}
            )
            service.validate_draft(draft)
            if draft.context and draft.context.emotion:
                grounded = grounded_context(draft.context, *emotion_sources(session, message_id))
                draft = draft.model_copy(update={"context": grounded})
                if grounded is None:
                    payload.pop("context", None)
                else:
                    payload["context"] = grounded.model_dump(exclude_none=True)

        def query() -> str:
            if isinstance(draft, MemoryCorrection):
                return service.preview_correction(target, draft).recall_query
            if target:
                entry = service.require_entry(target, include_deprecated=name == "deprecate")
                return f"{entry.text} {draft.text}" if draft and draft.text else entry.text
            return draft.text

        boundary = payload.get("previous_valid_to")
        if boundary is not None:
            MemoryContext(valid_to=boundary)

        new_fact = name in {"memorize", "create_pending"}

        def write(decision: ProposalDecision) -> dict:
            if decision.decision == "pending":
                if not new_fact:
                    raise ProposalError("Only a new fact can be queued. Submit a candidate draft.")
                question = decision.question or payload.get("question")
                if not question:
                    raise ProposalError(
                        "Include a question for the user with the pending decision."
                    )
                item = service.create_pending(
                    draft, question, payload.get("related_ids", []), **source
                )
                return {"pending": pending_view(item)}
            if name == "deprecate":
                entry = service.deprecate(target, **source)
            elif name == "transition":
                entry = service.transition(target, draft, previous_valid_to=boundary, **source)
            elif name == "correct":
                entry = service.correct(target, draft, **source)
            else:
                entry = service.memorize(draft, **source)
            return {"memory": entry.as_recall().model_dump(exclude_none=True)}

        return evidence.propose(
            name,
            payload,
            query,
            write,
            target_id=target,
            include_pending=new_fact,
            related_ids=tuple(payload.get("related_ids", [])),
        )

    mutations = [
        (
            "memorize",
            MemoryDraft,
            "新增用户明确陈述的事实。按已知情况填写 text、topic 和 context。",
        ),
        (
            "correct",
            Correction,
            "更正同一 ID 的错误事实，仅传要修改的字段；保留修正前的版本和来源。",
        ),
        (
            "transition",
            Transition,
            "情况后来变化，保留原事实。仅提供明确的旧结束时间或新开始时间。",
        ),
        ("deprecate", Target, "软弃用目标记忆。保留其原文和来源。"),
        (
            "create_pending",
            Pending,
            "关键歧义暂未厘清时创建候选。question 向用户提问，related_ids 仅引用事实。",
        ),
    ]
    for name, model, description in mutations:
        register(name, description, model, lambda req, op=name: mutate(op, req), write=True)

    register(
        "resolve_memory_proposals",
        "审阅检索结果后按 proposal_id 集中裁决。memory 执行正式操作，pending 为新增事实留待审问题，skip 跳过。可安全重试。",
        ResolveProposals,
        lambda request: evidence.resolve(request.decisions),
        write=True,
    )
