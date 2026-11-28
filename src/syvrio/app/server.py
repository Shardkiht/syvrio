from __future__ import annotations

from fastmcp import FastMCP

from ..backend.client import ResidentClient
from ..backend.client_contracts import MemoryContext, MemoryCorrection, MemoryDraft


def build_server(client: ResidentClient | None = None) -> FastMCP:
    svc = client or ResidentClient()
    capabilities = svc.call("mcp_capabilities")
    mcp = FastMCP(
        "syvrio",
        instructions=(
            "个人记忆库。确定的用户事实可调用 memorize，并提供结构化主题与可选上下文。"
            "回答用户问题前，如果涉及用户个人信息、偏好、经历、计划，先调用 recall 检索。"
            "仅用户明确要求完整主题，或 recall 不足以回答且需要完整主题上下文时调用 read_topic。"
            "需要最新公开信息时调用 search。"
        ),
    )

    def model_write(action, arguments, token):
        return svc.call("mcp_write", action=action, arguments=arguments, review_token=token)

    @mcp.tool
    def memorize(
        text: str, topic: str, context: MemoryContext | None = None, review_token: str | None = None
    ) -> dict:
        """保存一条明确的用户事实，保留来源中给出的时间、地点和情绪。"""
        draft = MemoryDraft(text=text, topic=topic, context=context)
        return model_write("memorize", {"draft": draft.model_dump(exclude_none=True)}, review_token)

    @mcp.tool
    def note(text: str, topic: str = "uncategorized", review_token: str | None = None) -> dict:
        """保存用户给出的原文。"""
        return model_write("memorize", {"draft": {"text": text, "topic": topic}}, review_token)

    @mcp.tool
    def recall(
        query: str,
        limit: int | None = None,
        time_range: dict[str, str] | None = None,
        location: str | None = None,
        emotion: str | None = None,
    ) -> list[dict]:
        """检索本地记忆。query 用自然问句并附常见同义词或字段名，如“我叫什么名字 姓名 名字”。"""
        return svc.call(
            "recall",
            query=query,
            limit=limit,
            time_range=time_range,
            location=location,
            emotion=emotion,
        )

    @mcp.tool
    def read_topic(topic: str) -> dict:
        """读取主题的结构化条目及其状态。仅用户明确要求完整主题，或 recall 不足以回答且需要完整主题上下文时使用；topic 只接受主题名或标题，不接受路径。"""
        try:
            return svc.read_topic(topic)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool
    def transition(
        entry_id: str,
        draft: MemoryDraft,
        previous_valid_to: str | None = None,
        review_token: str | None = None,
    ) -> dict:
        """记录原事实有效期或状态的变化，并将新事实与原事实相关联。"""
        return model_write(
            "transition",
            {
                "entry_id": entry_id,
                "draft": draft.model_dump(exclude_none=True),
                "previous_valid_to": previous_valid_to,
            },
            review_token,
        )

    @mcp.tool
    def correct(entry_id: str, draft: MemoryCorrection, review_token: str | None = None) -> dict:
        """更正同一条记忆，保留更正前的版本及来源。"""
        return model_write(
            "correct",
            {"entry_id": entry_id, "draft": draft.model_dump(exclude_unset=True)},
            review_token,
        )

    @mcp.tool
    def deprecate(entry_id: str, review_token: str | None = None) -> dict:
        """弃用一条正式记忆，保留其历史和来源。"""
        return model_write("deprecate", {"entry_id": entry_id}, review_token)

    @mcp.tool
    def profile() -> str:
        """查看最近一次 Dream 生成的画像。"""
        return svc.call("profile")

    @mcp.tool
    def create_pending(
        draft: MemoryDraft, question: str, related_ids: list[str], review_token: str | None = None
    ) -> dict:
        """保存待澄清的候选，列出需确认的问题及关联事实。"""
        return model_write(
            "create_pending",
            {
                "draft": draft.model_dump(exclude_none=True),
                "question": question,
                "related_ids": related_ids,
            },
            review_token,
        )

    @mcp.tool
    def review_pending() -> list[dict]:
        """列出等待人工确认的记忆候选。"""
        return [item.model_dump(exclude_none=True) for item in svc.pending()]

    @mcp.tool
    def approve(item_id: str) -> dict:
        """把一条待确认候选写入记忆库。"""
        try:
            return svc.call("mcp_approve", item_id=item_id)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool
    def reject(item_id: str) -> dict:
        """拒绝一条待确认候选，保留处理记录。"""
        try:
            return svc.reject(item_id).model_dump(exclude_none=True)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool
    def list_topics() -> list[dict]:
        """列出记忆主题及条目数。"""
        return svc.topics()

    @mcp.tool
    def status() -> dict:
        """查看记忆库状态：主题数、条目数、待确认数、未提交变更。"""
        return svc.status()

    if capabilities["search"]:

        @mcp.tool(description=capabilities["search_description"])
        def search(
            query: str,
            limit: int = capabilities["max_results"],
            search_engine: str | list[str] = "auto",
            context: list[str] | None = None,
            providers: list[str] | None = None,
            strategy: str = "auto",
        ) -> dict:
            return svc.call(
                "tool_search",
                query=query,
                limit=limit,
                search_engine=search_engine,
                context=context,
                providers=providers,
                strategy=strategy,
            )

    return mcp
