"""AnySearch REST 搜索适配器：匿名可用，配置 Key 后改用 Bearer。"""

from __future__ import annotations

from typing import Any, Callable

import httpx

from ..base import (
    FetchQuery,
    FetchResult,
    ProviderError,
    ProviderErrorKind,
    SearchQuery,
    SearchResult,
)
from ..fetch_content import extract_text, extracted_result
from ..http_client import JsonApiClient

NAME = "anysearch"
DEFAULT_BASE_URL = "https://api.anysearch.com/v1"


def _request(
    client: JsonApiClient,
    path: str,
    body: dict,
    headers: dict[str, str],
    timeout: float | None = None,
) -> dict:
    try:
        response = client.post_json(path, body, headers, timeout)
    except ProviderError as exc:
        # 匿名额度耗尽时响应可能含自动生成的账号和密钥，只转发状态码。
        parts = exc.detail.split(maxsplit=2)
        detail = "AnySearch 请求失败"
        if len(parts) >= 2 and parts[0] == "HTTP" and parts[1].isascii() and parts[1].isdigit():
            detail = f"HTTP {parts[1]}"
        pause = "official_quota_exhausted" if exc.kind == ProviderErrorKind.QUOTA else ""
        raise ProviderError(NAME, exc.kind, detail, pause_reason=pause) from None
    return client.parse_json(response)


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
        payload = _request(
            self._client,
            "/search",
            body,
            headers,
            timeout=self._client.effective_timeout(query.timeout),
        )
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
        payload = _request(self._client, "/extract", {"url": query.url}, headers)
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
    return SearchResult(
        title=str(item.get("title") or "").strip() or url,
        url=url,
        snippet=snippet,
        published=None,
        provider=NAME,
    )
