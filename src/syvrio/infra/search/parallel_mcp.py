"""Parallel Streamable HTTP MCP 搜索适配器：匿名可用，Key 仅作为 Bearer。"""

from __future__ import annotations

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import httpx

from .base import (
    FetchQuery,
    FetchResult,
    ProviderError,
    ProviderErrorKind,
    SearchQuery,
    SearchResult,
    normalize_url,
    parse_published_date,
)
from .fetch_content import extract_text, extracted_result

NAME = "parallel_mcp"
MCP_URL = "https://search.parallel.ai/mcp"
FETCH_MCP_URL = "https://mcp.parallel.ai/mcp"
MAX_SNIPPET_CHARS = 600
MAX_TITLE_CHARS = 300
MAX_RESULTS = 20

# 注入点接收 (headers, timeout)，返回说明符协议的异步上下文管理器。
ClientFactory = Callable[[dict[str, str], float], Any]


class ParallelMcpProvider:
    name = NAME
    capabilities = {"search"}

    def __init__(
        self,
        api_key: str = "",
        timeout: float = 15.0,
        client_factory: ClientFactory | None = None,
    ):
        self._api_key = api_key.strip()
        self._timeout = max(0.05, timeout)
        self._client_factory = client_factory or _default_client

    def search(self, query: SearchQuery) -> list[SearchResult]:
        timeout = max(0.05, min(self._timeout, query.timeout or self._timeout))
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        objective = query.query
        if query.context_terms:
            objective += " — context: " + ", ".join(query.context_terms[:5])
        arguments = {
            "objective": objective,
            "search_queries": [query.query],
        }
        try:
            result = _run_async(lambda: self._call(arguments, headers, timeout))
        except ProviderError:
            raise
        except Exception as exc:
            raise _translate_error(exc) from exc
        if getattr(result, "is_error", False):
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "MCP 工具返回错误")
        items = _extract_results(result)
        if items is None:
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "无法解析 MCP 搜索响应")
        return _map_results(items, min(MAX_RESULTS, max(1, query.limit)))

    async def _call(
        self, arguments: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> Any:
        return await _call_tool(self._client_factory, headers, timeout, "web_search", arguments)


class ParallelMcpFetchProvider:
    name = NAME
    capabilities = {"fetch"}

    def __init__(
        self,
        api_key: str = "",
        timeout: float = 45.0,
        client_factory: ClientFactory | None = None,
    ):
        self._api_key = api_key.strip()
        self._timeout = max(0.05, timeout)
        self._client_factory = client_factory or _fetch_client

    def fetch(self, query: FetchQuery) -> FetchResult:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            result = _run_async(
                lambda: _call_tool(
                    self._client_factory,
                    headers,
                    self._timeout,
                    "web_fetch",
                    {"urls": [query.url], "full_content": True},
                    raise_on_error=False,
                )
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise _fetch_error(exc) from None
        if getattr(result, "is_error", False):
            # Tool errors may be sent in an HTTP 200 response. Classify known
            # upstream errors without including arbitrary tool text in diagnostics.
            for item in getattr(result, "content", None) or ():
                text = getattr(item, "text", "")
                if not isinstance(text, str):
                    continue
                error = _fetch_error(RuntimeError(text))
                if error.kind in (
                    ProviderErrorKind.AUTH,
                    ProviderErrorKind.QUOTA,
                    ProviderErrorKind.RATE_LIMIT,
                    ProviderErrorKind.UNAVAILABLE,
                ):
                    raise error
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "MCP web_fetch 工具返回错误")
        items = _extract_results(result)
        if items is None:
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "无法解析 MCP 抓取响应")
        # Only one URL was requested. Per-URL errors (including target-site
        # 401/429) are not account errors and must not pause this provider.
        if len(items) != 1 or not isinstance(items[0], dict):
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "MCP 响应没有单一网页结果")
        item = items[0]
        content = extract_text(item.get("full_content"))
        excerpts = item.get("excerpts")
        if not content and isinstance(excerpts, list):
            content = "\n\n".join(filter(None, map(extract_text, excerpts)))
        return extracted_result(
            NAME,
            query,
            url=item.get("final_url") or item.get("url"),
            title=item.get("title"),
            content=content,
        )


async def _call_tool(factory, headers, timeout, tool, arguments, **kwargs):
    async with factory(dict(headers), timeout) as client:
        return await client.call_tool(tool, arguments, timeout=timeout, **kwargs)


def _default_client(headers: dict[str, str], timeout: float, url: str = MCP_URL):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(url, headers=headers or None)
    return Client(transport, timeout=timeout, init_timeout=timeout)


def _fetch_client(headers: dict[str, str], timeout: float):
    return _default_client(headers, timeout, FETCH_MCP_URL)


def _fetch_error(exc: Exception) -> ProviderError:
    error = _translate_error(exc)
    # FastMCP can wrap transport errors during negotiation. Inspect the cause
    # and the SDK's JSON-RPC error code without forwarding its message.
    if error.kind == ProviderErrorKind.NETWORK:
        cause = exc.__cause__
        if isinstance(cause, Exception):
            inner = _fetch_error(cause)
            if inner.kind != ProviderErrorKind.NETWORK:
                return inner
        code = getattr(exc, "code", getattr(getattr(exc, "error", None), "code", None))
        if code == -32001 or "timed out" in str(exc).lower():
            return ProviderError(NAME, ProviderErrorKind.TIMEOUT, "MCP 请求超时")
        if code in (-32600, -32601, -32602, -32603, -32700):
            return ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "MCP web_fetch RPC 响应错误")
    if error.kind == ProviderErrorKind.QUOTA:
        return ProviderError(
            NAME, error.kind, error.detail, pause_reason="official_quota_exhausted"
        )
    return error


def _run_async(coroutine_factory: Callable[[], Any]) -> Any:
    """同步 Provider 契约的桥接；若调用方已有事件循环，放到独立线程执行。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine_factory())
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(coroutine_factory())).result()


def _extract_results(result: Any) -> list[Any] | None:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        found = _results_from_payload(structured)
        if found is not None:
            return found
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        payload = _load_json_text(text)
        if payload is not None:
            found = _results_from_payload(payload)
            if found is not None:
                return found
    return None


def _results_from_payload(payload: Any) -> list[Any] | None:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if isinstance(results, list):
        return results
    for key in ("result", "data"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            results = nested.get("results")
            if isinstance(results, list):
                return results
    return None


def _load_json_text(text: str) -> Any | None:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            value = "\n".join(lines[1:-1]).strip()
            if value.lower().startswith("json"):
                value = value[4:].lstrip()
    try:
        return json.loads(value)
    except ValueError:
        return None


def _map_results(items: list[Any], limit: int) -> list[SearchResult]:
    mapped: list[SearchResult] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        title = str(item.get("title") or "").strip() or url
        if len(title) > MAX_TITLE_CHARS:
            title = title[:MAX_TITLE_CHARS].rstrip() + "…"
        mapped.append(
            SearchResult(
                title=title,
                url=url,
                snippet=_excerpt(item.get("excerpts")),
                published=parse_published_date(item.get("publish_date")),
                provider=NAME,
            )
        )
        if len(mapped) >= limit:
            break
    return mapped


def _excerpt(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for entry in value[:2]:
        if isinstance(entry, str):
            text = entry.strip()
        elif isinstance(entry, dict):
            text = str(
                entry.get("text")
                or entry.get("excerpt")
                or entry.get("content")
                or entry.get("snippet")
                or ""
            ).strip()
        else:
            text = ""
        if text:
            parts.append(text)
    result = " … ".join(parts)
    if len(result) > MAX_SNIPPET_CHARS:
        result = result[:MAX_SNIPPET_CHARS].rstrip() + "…"
    return result


def _translate_error(exc: Exception) -> ProviderError:
    from fastmcp.exceptions import AuthorizationError, InsufficientScopeError

    if isinstance(exc, (AuthorizationError, InsufficientScopeError)):
        return ProviderError(NAME, ProviderErrorKind.AUTH, "MCP 认证失败")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        return ProviderError(NAME, ProviderErrorKind.TIMEOUT, "MCP 请求超时")
    status = _status_code(exc)
    if status in (401, 403):
        return ProviderError(NAME, ProviderErrorKind.AUTH, f"HTTP {status}")
    if status == 402:
        return ProviderError(NAME, ProviderErrorKind.QUOTA, "HTTP 402")
    if status == 429:
        return ProviderError(NAME, ProviderErrorKind.RATE_LIMIT, "HTTP 429")
    if status is not None and status >= 500:
        return ProviderError(NAME, ProviderErrorKind.UNAVAILABLE, f"HTTP {status}")
    if status is not None:
        return ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, f"HTTP {status}")
    text = str(exc).lower()
    if "401" in text or "403" in text or "unauthorized" in text:
        return ProviderError(NAME, ProviderErrorKind.AUTH, "MCP 认证失败")
    if "429" in text or "rate limit" in text:
        return ProviderError(NAME, ProviderErrorKind.RATE_LIMIT, "MCP 请求被限流")
    return ProviderError(NAME, ProviderErrorKind.NETWORK, type(exc).__name__)


def _status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    match = re.search(r"\b(401|402|403|429|5\d\d)\b", str(exc))
    return int(match.group(1)) if match else None
