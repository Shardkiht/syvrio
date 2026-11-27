"""仅供本地 CLI 和 MCP 显式调用的操作，公开的网页 API 无权访问。"""

from __future__ import annotations

import re
from dataclasses import asdict, replace

from pydantic import TypeAdapter

from ..application.contracts import ImportDocument
from ..infra.config import WebShareConfig, write_config
from ..infra.search import SearchQuery, build_search_router
from ..infra.search.tool_request import run_tool_search
from ..memory.models import MemoryCorrection, MemoryDraft
from .client_contracts import UploadedImportDocument
from .hosts import normalize_domain


class ResidentOperations:
    def __init__(self, owner):
        self.owner = owner
        self.service = owner._service
        self.search_router = None

    def dispatch(self, name: str, data: dict):
        service = self.service
        if name == "correct":
            data = {**data, "draft": MemoryCorrection.model_validate(data.get("draft"))}
        elif name in {"memorize", "transition"}:
            data = {**data, "draft": MemoryDraft.model_validate(data.get("draft"))}
        operations = {
            "web_share": self.web_share,
            "memorize": service.memorize,
            "verify_attachments": self.verify_attachments,
            "mcp_capabilities": lambda: {
                "search": service.config.search.enabled,
                "max_results": service.config.search.max_results,
            },
            "recall": service.recall,
            "read_topic": service.read_topic,
            "deprecate": service.deprecate,
            "correct": service.correct,
            "memory_history": service.store.version_history,
            "read_memory_source": service.read_memory_source,
            "transition": service.transition,
            "topics": service.topics,
            "pending": service.pending,
            "approve": service.approve,
            "mcp_approve": lambda item_id: (
                service.approve(item_id).as_recall().model_dump(exclude_none=True)
            ),
            "reject": service.reject,
            "history": self.owner.git.history,
            "commit": self.owner.git.commit,
            "status": lambda: {**service.status(), "dirty": self.owner.git.is_dirty()},
            "inferences": service.list_inferences,
            "review_inference": service.review_inference,
            "profile": service.profile.read_text,
            "sessions": self.owner._sessions.list,
            "inference_review": self.owner._app_service.memory.load_inference_review,
            "dream": lambda action="run": self.owner._app_service.reflection.command(action),
            "search": self.search,
            "tool_search": self.tool_search,
        }
        if name not in operations:
            raise KeyError("Unknown backend operation.")
        return operations[name](**data)

    def web_share(self, domain: str, github_id: str | None, save: bool = False) -> dict:
        with self.owner.scheduling.settings_lock:
            cfg = self.service.config
            try:
                host = normalize_domain(domain or cfg.web_share.domain)
            except ValueError as exc:
                raise ValueError(
                    "请用 --domain 指定 ngrok 分配的完整主机名，例如 demo.ngrok-free.dev。"
                ) from exc
            account = cfg.web_share.github_user_id if github_id is None else github_id
            if not re.fullmatch(r"[1-9][0-9]{0,19}", account):
                raise ValueError("请用 --github-id 指定唯一允许登录的 GitHub 数字账号 ID。")
            share = WebShareConfig(domain=host, github_user_id=account)
            if save and cfg.web_share != share:
                pending = replace(cfg, web_share=share)
                write_config(self.service.config_path, pending)
                cfg.web_share, cfg._baseline = share, pending._baseline
            return asdict(share)

    def verify_attachments(self):
        return self.owner._sessions.verify_attachments()

    def import_documents(self, upload, apply: bool):
        def documents():
            upload.seek(0)
            for line in upload:
                data = UploadedImportDocument.model_validate_json(line)
                yield ImportDocument(data.name, data.content, data.read_error)

        # 在开始任何写入前验证上传的每条记录；遍历二次以免一次性载入整个目录。
        count = sum(1 for _ in documents())
        return self.owner._app_service.memory.import_documents(
            documents(), apply, total_files=count
        )

    def _router(self):
        if not self.service.config.search.enabled:
            raise ValueError("Search is disabled.")
        if self.search_router is None:
            self.search_router = build_search_router(self.service.config.search, self.service.paths)
        return self.search_router

    def search(self, query: dict):
        return self._router().search(TypeAdapter(SearchQuery).validate_python(query)).to_payload()

    def tool_search(self, **arguments):
        return run_tool_search(self._router(), self.service.config.search, arguments)
