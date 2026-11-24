"""会话工具和 MCP 使用相同的搜索参数与结果格式。"""

from __future__ import annotations

from ..config import SearchConfig
from .base import (
    SearchArgumentError,
    SearchQuery,
    normalize_search_engines,
    normalize_search_providers,
)
from .providers.open_websearch import SEARCH_ENGINES
from .router import SearchRouter


def run_tool_search(router: SearchRouter, settings: SearchConfig, arguments: dict) -> dict:
    text = str(arguments.get("query", "")).strip()

    def failure(field: str, message: str) -> dict:
        return {
            "results": [],
            "provider": "",
            "failed": [{"provider": field, "kind": "unavailable", "detail": message}],
        }

    unexpected = arguments.keys() - {
        "query",
        "limit",
        "search_engine",
        "providers",
        "strategy",
        "context",
    }
    if unexpected:
        return failure("search", f"不支持的搜索参数：{', '.join(sorted(unexpected))}")
    if not text:
        return {"results": [], "provider": "", "failed": [], "hint": "query 为空"}
    try:
        requested = int(arguments.get("limit", settings.max_results))
    except (TypeError, ValueError):
        requested = settings.max_results
    try:
        selected_engines = normalize_search_engines(
            arguments.get("search_engine"), SEARCH_ENGINES, settings.max_search_engines
        )
    except ValueError as exc:
        return failure("search_engine", str(exc))
    try:
        providers = normalize_search_providers(arguments.get("providers"))
    except ValueError as exc:
        return failure("providers", str(exc))
    strategy = str(arguments.get("strategy") or "auto").strip().lower()
    context = arguments.get("context") or ()
    if isinstance(context, str):
        context = [context]
    query = SearchQuery(
        query=text,
        limit=max(1, min(requested, 15)),
        engines=selected_engines,
        context_terms=tuple(str(item).strip() for item in context if str(item).strip())[:5],
        providers=providers,
        strategy=strategy,
    )
    try:
        return router.search(query).to_payload()
    except SearchArgumentError as exc:
        return failure(exc.field, str(exc))
    except Exception as exc:
        return {**failure("router", str(exc)), "hint": str(exc)}
