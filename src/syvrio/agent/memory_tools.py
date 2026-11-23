"""Schemas for agent memory tools. All writes are validated and performed by MemoryService."""

from __future__ import annotations

import json
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from ..infra.config import DEFAULTS
from ..memory.extractor import grounded_context
from ..memory.models import MemoryContext, MemoryDraft, TimeRange
from ..memory.service import MemoryService, pending_view
from ..session.model import Session
from .evidence import RecallContext, emotion_sources
from .registry import ToolOutcome, ToolRegistry, ToolSpec


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


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


class ChatSearch(Arguments):
    query: str = Field(min_length=1, max_length=160)
    limit: int = Field(default=10, ge=1, le=50)


class Target(Arguments):
    entry_id: str = Field(min_length=1)


class Replacement(MemoryDraft):
    entry_id: str = Field(min_length=1)


class Transition(Replacement):
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
    session: Session,
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
        lambda r: service.session_query.search_chats(r.query, r.limit, exclude_session=session.id),
    )
    register("read_topic", "按主题读取全部有效事实。", Topic, lambda r: service.read_topic(r.topic))
    register(
        "read_memory_source",
        "按事实 ID 查阅一条原始用户消息，作为只读证据。",
        Source,
        lambda r: service.read_memory_source(r.memory_id),
    )
    register(
        "review_pending",
        "查看所有等待人工确认的候选和问题。",
        Arguments,
        lambda _: {"pending": [pending_view(item) for item in service.pending()]},
    )

    def mutate(name: str, request: BaseModel) -> dict:
        payload = request.model_dump(exclude_none=True)
        target = payload.get("entry_id")
        message_id = session.last_user_message_id()
        if not message_id:
            raise ValueError("A chat write requires an original user message.")
        source = {"session_id": session.id, "message_id": message_id}
        draft = None
        if "text" in payload:
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
        query = draft.text if draft else target
        if target:
            entry = service.store.find(target)
            if entry is None:
                raise ValueError("The target memory does not exist.")
            if name != "deprecate" and entry.status != "active":
                raise ValueError("The target memory is deprecated.")
            query = f"{entry.text} {draft.text}" if draft else entry.text
        boundary = payload.get("previous_valid_to")
        if boundary is not None:
            MemoryContext(valid_to=boundary)

        def write() -> dict:
            if name == "create_pending":
                item = service.create_pending(
                    draft, request.question, request.related_ids, **source
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

        return evidence.run(
            name,
            payload,
            query,
            write,
            target_id=target,
            include_pending=name == "create_pending",
            related_ids=tuple(payload.get("related_ids", [])),
        )

    mutations = [
        (
            "memorize",
            MemoryDraft,
            "新增用户明确陈述的事实。按已知情况填写 text、topic 和 context。",
        ),
        ("correct", Replacement, "旧事实原本错误，创建正确事实并将旧 ID 标为弃用。"),
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
