"""AnySearch REST 搜索适配器：匿名可用，配置 Key 后改用 Bearer。"""

from __future__ import annotations

from typing import Any, Callable

import httpx

from .base import (
    FetchQuery,
    FetchResult,
    ProviderError,
    ProviderErrorKind,
    SearchQuery,
    SearchResult,
)
from .fetch_content import extract_text, extracted_result
from .http_client import JsonApiClient

NAME = "anysearch"
DEFAULT_BASE_URL = "https://api.anysearch.com/v1"
MAX_SNIPPET_CHARS = 600


class AnySearchProvider:
    name = NAME
    capabilities = {"search"}

    def __init__(
        self,
        api_key: str = "",
        timeout: float = 10.0,
        base_url: str = DEFAULT_BASE_URL,
        zone: str = "cn",
        language: str = "zh-CN",
        client_factory: Callable[[], httpx.Client] | None = None,
    ):
        self._client = JsonApiClient(NAME, base_url, timeout, client_factory)
        self._api_key = api_key.strip()
        self._zone = zone.strip() or "cn"
        self._language = language.strip() or "zh-CN"

    def search(self, query: SearchQuery) -> list[SearchResult]:
        body: dict[str, Any] = {
            "query": query.query,
            "max_results": max(1, min(query.limit, 20)),
            "zone": self._zone,
            "language": self._language,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        response = self._client.post_json(
            "/search",
            body,
            headers,
            timeout=self._client.effective_timeout(query.timeout),
        )
        payload = self._client.parse_json(response)
        code = payload.get("code")
        if code is not None and str(code) != "0":
            raise ProviderError(
                NAME,
                ProviderErrorKind.BAD_RESPONSE,
                f"业务错误 code={str(code)[:40]}",
                code=str(code)[:40],
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "响应缺少 data 对象")
        results = data.get("results")
        if results is None:
            return []
        if not isinstance(results, list):
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "data.results 字段类型异常")
        mapped = [_map_result(item) for item in results]
        return [item for item in mapped if item is not None]


class AnySearchFetchProvider:
    name = NAME
    capabilities = {"fetch"}

    def __init__(
        self,
        api_key: str = "",
        timeout: float = 45.0,
        base_url: str = DEFAULT_BASE_URL,
        client_factory: Callable[[], httpx.Client] | None = None,
    ):
        self._client = JsonApiClient(NAME, base_url, timeout, client_factory)
        self._api_key = api_key.strip()

    def fetch(self, query: FetchQuery) -> FetchResult:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            response = self._client.post_json("/extract", {"url": query.url}, headers)
        except ProviderError as exc:
            # A 402 can carry generated credentials in the response message.
            # Do not propagate the server's text or error-code fields.
            parts = exc.detail.split(maxsplit=2)
            detail = "抓取 API 请求失败"
            if len(parts) >= 2 and parts[0] == "HTTP":
                code = parts[1]
                if code.isascii() and code.isdecimal() and len(code) == 3:
                    detail = f"HTTP {code}"
            pause = "official_quota_exhausted" if exc.kind == ProviderErrorKind.QUOTA else ""
            raise ProviderError(NAME, exc.kind, detail, pause_reason=pause) from None
        payload = self._client.parse_json(response)
        data = payload.get("data")
        if payload.get("code") != 0 or not isinstance(data, dict):
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "Extract 响应结构异常")
        return extracted_result(
            NAME,
            query,
            url=data.get("final_url") or data.get("url"),
            title=data.get("title"),
            content=extract_text(data.get("content")),
        )


def _map_result(item: Any) -> SearchResult | None:
    if not isinstance(item, dict):
        return None
    url = str(item.get("url") or "").strip()
    if not url:
        return None
    snippet = str(item.get("snippet") or item.get("content") or "").strip()
    if len(snippet) > MAX_SNIPPET_CHARS:
        snippet = snippet[:MAX_SNIPPET_CHARS].rstrip() + "…"
    return SearchResult(
        title=str(item.get("title") or "").strip() or url,
        url=url,
        snippet=snippet,
        published=None,
        provider=NAME,
    )
