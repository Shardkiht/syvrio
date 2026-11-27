"""Parallel Streamable HTTP MCP 搜索适配器：匿名可用，Key 仅作为 Bearer。"""

from __future__ import annotations

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import httpx

from ..base import (
    FetchQuery,
    FetchResult,
    ProviderError,
    ProviderErrorKind,
    SearchQuery,
    SearchResult,
    normalize_url,
    parse_published_date,
)
from ..fetch_content import extract_text, extracted_result

NAME = "parallel_mcp"
MCP_URL = "https://search.parallel.ai/mcp"
FETCH_MCP_URL = "https://mcp.parallel.ai/mcp"
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
        result = _request_tool(self._client_factory, headers, timeout, "web_search", arguments)
        items = _extract_results(result)
        if items is None:
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "无法解析 MCP 搜索响应")
        return _map_results(items, min(MAX_RESULTS, max(1, query.limit)))


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
        result = _request_tool(
            self._client_factory,
            headers,
            self._timeout,
            "web_fetch",
            {"urls": [query.url], "full_content": True},
        )
        items = _extract_results(result)
        if items is None:
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "无法解析 MCP 抓取响应")
        # 每次只请求一个网址。该网址的错误（包括目标站点的 401 或 429）
        # 不代表供应商账户故障，无需暂停供应商。
        if len(items) != 1 or not isinstance(items[0], dict):
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "MCP 响应没有单一网页结果")
        item = items[0]
        content = extract_text(item.get("full_content"))
        return extracted_result(
            NAME,
            query,
            url=item.get("final_url") or item.get("url"),
            title=item.get("title"),
            content=content or _excerpt(item.get("excerpts")),
            excerpts_only=not content,
        )


def _request_tool(
    factory: ClientFactory, headers: dict, timeout: float, tool: str, arguments: dict
):
    async def call():
        async with factory(dict(headers), timeout) as client:
            return await client.call_tool(tool, arguments, timeout=timeout, raise_on_error=False)

    try:
        result = _run_async(call)
    except Exception as exc:
        # 传输错误可能夹带凭据或请求正文，只向外传递经过分类的错误。
        raise _translate_error(exc) from None
    if getattr(result, "is_error", False):
        error = _reported_error(getattr(result, "structured_content", None))
        if error:
            raise error
        for block in getattr(result, "content", None) or ():
            text = getattr(block, "text", None)
            if isinstance(text, str):
                payload = _load_json_text(text)
                error = _reported_error(payload if payload is not None else text)
                if error:
                    raise error
        raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "MCP 工具返回错误")
    return result


def _default_client(headers: dict[str, str], timeout: float, url: str = MCP_URL):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(url, headers=headers or None)
    return Client(transport, timeout=timeout, init_timeout=timeout)


def _fetch_client(headers: dict[str, str], timeout: float):
    return _default_client(headers, timeout, FETCH_MCP_URL)


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
    for entry in value:
        if isinstance(entry, dict):
            entry = (
                entry.get("text")
                or entry.get("excerpt")
                or entry.get("content")
                or entry.get("snippet")
            )
        text = extract_text(entry)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _http_error(status: int) -> ProviderError:
    if status == 402:
        return ProviderError(
            NAME, ProviderErrorKind.QUOTA, "HTTP 402", pause_reason="official_quota_exhausted"
        )
    if status in (401, 403):
        kind = ProviderErrorKind.AUTH
    elif status == 429:
        kind = ProviderErrorKind.RATE_LIMIT
    elif 500 <= status < 600:
        kind = ProviderErrorKind.UNAVAILABLE
    else:
        kind = ProviderErrorKind.BAD_RESPONSE
    return ProviderError(NAME, kind, f"HTTP {status}")


def _reported_error(value: object) -> ProviderError | None:
    # 只读工具级错误信封。网页结果、按 URL 报错的条目和引文都由目标站点控制。
    if isinstance(value, dict):
        if any(key in value for key in ("results", "errors", "url", "final_url")):
            return None
        for field in ("status_code", "status", "code", "type", "error", "message", "detail"):
            if field in value and (error := _reported_error(value[field])):
                return error
        return None
    if isinstance(value, int) and 400 <= value <= 599:
        return _http_error(value)
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    status = re.match(r"^(?:error:\s*)?(?:http(?:/\d(?:\.\d)?)?\s+)?([45]\d{2})(?=$|[\s:.-])", text)
    if status:
        return _http_error(int(status[1]))
    if re.match(
        r"^(?:error:\s*)?(?:(?:your|the) (?:account|organization) has )?insufficient[ _-]+credits\b",
        text,
    ):
        return ProviderError(
            NAME,
            ProviderErrorKind.QUOTA,
            "Insufficient credits",
            pause_reason="official_quota_exhausted",
        )
    if text.startswith("unauthorized"):
        return ProviderError(NAME, ProviderErrorKind.AUTH, "MCP 认证失败")
    if text.startswith(("rate limit", "rate_limit")):
        return ProviderError(NAME, ProviderErrorKind.RATE_LIMIT, "MCP 请求被限流")
    return None


def _translate_error(exc: Exception) -> ProviderError:
    from fastmcp.exceptions import AuthorizationError, InsufficientScopeError

    if isinstance(exc, (AuthorizationError, InsufficientScopeError)):
        return ProviderError(NAME, ProviderErrorKind.AUTH, "MCP 认证失败")
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return ProviderError(NAME, ProviderErrorKind.TIMEOUT, "MCP 请求超时")
    status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(
        exc, "status_code", None
    )
    if isinstance(status, int):
        return _http_error(status)
    if isinstance(exc.__cause__, Exception) and exc.__cause__ is not exc:
        cause = _translate_error(exc.__cause__)
        if cause.kind != ProviderErrorKind.NETWORK:
            return cause
    if error := _reported_error(str(exc)):
        return error
    code = getattr(exc, "code", getattr(getattr(exc, "error", None), "code", None))
    if code == -32001 or "timed out" in str(exc).lower():
        return ProviderError(NAME, ProviderErrorKind.TIMEOUT, "MCP 请求超时")
    if code in (-32600, -32601, -32602, -32603, -32700):
        return ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "MCP RPC 响应错误")
    return ProviderError(NAME, ProviderErrorKind.NETWORK, "MCP 连接失败")
