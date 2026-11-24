"""仅供本地 CLI 和 MCP 显式调用的操作，公开的网页 API 无权访问。"""

from __future__ import annotations

from pydantic import TypeAdapter

from ..infra.search import SearchQuery, build_search_router
from ..memory.models import MemoryDraft


class ResidentOperations:
    def __init__(self, owner):
        self.owner = owner
        self.service = owner._service
        self.search_router = None

    def dispatch(self, name: str, data: dict):
        service = self.service
        if name in {"memorize", "correct", "transition"}:
            data = {**data, "draft": MemoryDraft.model_validate(data.get("draft"))}
        operations = {
            "memorize": service.memorize,
            "import_text": service.import_text,
            "recall": service.recall,
            "read_topic": service.read_topic,
            "deprecate": service.deprecate,
            "correct": service.correct,
            "transition": service.transition,
            "topics": service.topics,
            "pending": service.pending,
            "approve": service.approve,
            "reject": service.reject,
            "history": service.history,
            "commit": service.commit,
            "status": service.status,
            "inferences": service.list_inferences,
            "review_inference": service.review_inference,
            "profile": service.profile.read_text,
            "sessions": self.owner._sessions.list,
            "inference_review": self.owner._app_service.memory.load_inference_review,
            "dream": lambda action="run": self.owner._app_service.reflection.command(action),
            "search": self.search,
        }
        if name not in operations:
            raise KeyError("Unknown backend operation.")
        return operations[name](**data)

    def search(self, query: dict):
        if not self.service.config.search.enabled:
            raise ValueError("Search is disabled.")
        if self.search_router is None:
            self.search_router = build_search_router(self.service.config.search, self.service.paths)
        return self.search_router.search(
            TypeAdapter(SearchQuery).validate_python(query)
        ).to_payload()
