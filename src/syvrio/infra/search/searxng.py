"""SearXNG 自托管搜索适配器：整个实例视为一个 Provider，无 API Key。

只访问配置里的实例地址，不解析用户查询中的 URL，也不跟随重定向；
SearXNG 内部的百度/Bing 等引擎由实例自行聚合，Syvrio 不再逐家适配。
"""
from __future__ import annotations

import json
import re
from threading import local
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from .base import (
    ProviderError,
    ProviderErrorKind,
    SearchFailure,
    SearchQuery,
    SearchResult,
    normalize_url,
    parse_published_date,
)

NAME = "searxng"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TITLE_CHARS = 300
MAX_SNIPPET_CHARS = 600
MAX_RESULTS = 20

_LANGUAGE = re.compile(r"^(?:all|[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)$")
_FRESHNESS = {
    "day": "day",
    "today": "day",
    "24h": "day",
    "1d": "day",
    "d": "day",
    "week": "week",
    "7d": "week",
    "month": "month",
    "30d": "month",
    "year": "year",
    "365d": "year",
}


def normalize_base_url(value: str) -> str:
    """校验并规范化 SearXNG 实例地址；允许本机/私网地址（自托管场景）。"""
    text = str(value or "").strip()
    if not text:
        raise ValueError("SearXNG 实例地址不能为空")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise ValueError("SearXNG 实例地址端口不合法") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("SearXNG 实例地址必须是 http 或 https")
    if not parts.hostname:
        raise ValueError("SearXNG 实例地址缺少主机名")
    if parts.username or parts.password:
        raise ValueError("SearXNG 实例地址不能包含用户名或密码")
    if parts.query or parts.fragment:
        raise ValueError("SearXNG 实例地址不能包含查询参数或片段")
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, "", ""))


class SearxngProvider:
    name = NAME
    capabilities = {"search"}

    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        client_factory: Callable[[], httpx.Client] | None = None,
    ):
        self.base_url = normalize_base_url(base_url)
        self.timeout = max(0.05, float(timeout))
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=self.timeout))
        self._search_state = local()

    @property
    def last_partial_failures(self) -> list[SearchFailure]:
        return getattr(self._search_state, "partial_failures", [])

    def search(self, query: SearchQuery) -> list[SearchResult]:
        self._search_state.partial_failures = []
        timeout = self.timeout
        if query.timeout is not None:
            timeout = max(0.05, min(self.timeout, query.timeout))
        params: dict[str, str] = {"q": query.query, "format": "json"}
        language = _map_language(query.lang)
        if language:
            params["language"] = language
        time_range = _map_freshness(query.freshness)
        if time_range:
            params["time_range"] = time_range

        try:
            with self._client_factory() as client:
                # 不跟随重定向：实例可信，但避免被上游 3xx 带到任意地址。
                with client.stream(
                    "GET", f"{self.base_url}/search", params=params, timeout=timeout
                ) as response:
                    if response.status_code != 200:
                        raise _status_error(response.status_code)
                    raw = _read_limited(response)
        except httpx.TimeoutException as exc:
            raise ProviderError(NAME, ProviderErrorKind.TIMEOUT, str(exc)) from exc
        except httpx.RequestError as exc:
            raise ProviderError(NAME, ProviderErrorKind.NETWORK, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(NAME, ProviderErrorKind.NETWORK, str(exc)) from exc

        payload = _parse_json(raw)
        results = payload.get("results")
        if results is None:
            results = []
        if not isinstance(results, list):
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "results 字段类型异常")
        partial = _partial_failures(payload.get("unresponsive_engines"))
        mapped = _map_results(results, max(1, min(query.limit, MAX_RESULTS)))
        if not mapped and partial:
            # 零结果且内部引擎全部报错：这是上游失败，不是正常的空结果。
            raise ProviderError(
                NAME,
                _aggregate_kind(partial),
                "内部引擎均无响应：" + "; ".join(
                    f"{item.provider}: {item.detail}" for item in partial[:5]
                ),
            )
        self._search_state.partial_failures = partial
        return mapped


def _read_limited(response: httpx.Response) -> bytes:
    data = bytearray()
    for chunk in response.iter_bytes():
        if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
            raise ProviderError(
                NAME,
                ProviderErrorKind.BAD_RESPONSE,
                f"响应超过 {MAX_RESPONSE_BYTES // (1024 * 1024)} MiB 限制",
            )
        data.extend(chunk)
    return bytes(data)


def _parse_json(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderError(
            NAME,
            ProviderErrorKind.BAD_RESPONSE,
            "响应不是合法 JSON（请确认 SearXNG 已在 settings.yml 的 search.formats 中启用 json）",
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "响应顶层不是 JSON 对象")
    return payload


def _status_error(status: int) -> ProviderError:
    if status == 401:
        return ProviderError(
            NAME, ProviderErrorKind.AUTH, "HTTP 401：实例需要认证，当前仅支持无认证访问"
        )
    if status == 403:
        return ProviderError(
            NAME,
            ProviderErrorKind.BAD_RESPONSE,
            "HTTP 403：请确认 SearXNG 的 settings.yml 已启用 search.formats 的 json",
        )
    if status == 402:
        return ProviderError(NAME, ProviderErrorKind.QUOTA, "HTTP 402")
    if status == 429:
        return ProviderError(NAME, ProviderErrorKind.RATE_LIMIT, "HTTP 429")
    if status >= 500:
        return ProviderError(NAME, ProviderErrorKind.UNAVAILABLE, f"HTTP {status}")
    if status == 404:
        return ProviderError(
            NAME, ProviderErrorKind.BAD_RESPONSE, "HTTP 404：请检查 searxng_base_url 与实例路径"
        )
    return ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, f"HTTP {status}")


def _map_language(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    return text if _LANGUAGE.fullmatch(text) else ""


def _map_freshness(value: str | None) -> str:
    if not value:
        return ""
    return _FRESHNESS.get(str(value).strip().lower(), "")


def _map_results(items: list[Any], limit: int) -> list[SearchResult]:
    mapped: list[SearchResult] = []
    seen: set[str] = set()
    for item in items:
        result = _map_result(item)
        if result is None:
            continue
        key = normalize_url(result.url) or f"{result.title}\x00{result.snippet}"
        if key in seen:
            continue
        seen.add(key)
        mapped.append(result)
        if len(mapped) >= limit:
            break
    return mapped


def _map_result(item: Any) -> SearchResult | None:
    if not isinstance(item, dict):
        return None
    # 答案卡片/建议词等特殊对象不能混进普通网页结果。
    kind = item.get("type")
    if kind not in (None, "", "result"):
        return None
    url = str(item.get("url") or "").strip()
    if not _is_http_url(url):
        return None
    title = str(item.get("title") or "").strip() or url
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS].rstrip() + "…"
    snippet = str(item.get("content") or "").strip()
    if len(snippet) > MAX_SNIPPET_CHARS:
        snippet = snippet[:MAX_SNIPPET_CHARS].rstrip() + "…"
    return SearchResult(
        title=title,
        url=url,
        snippet=snippet,
        published=parse_published_date(item.get("publishedDate")),
        provider=NAME,
        engine=_engine_text(item.get("engine")),
    )


def _is_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def _engine_text(value: Any) -> str:
    if isinstance(value, list):
        text = ",".join(str(item).strip() for item in value if str(item).strip())
    else:
        text = str(value or "").strip()
    return text[:80]


def _partial_failures(raw: Any) -> list[SearchFailure]:
    if not isinstance(raw, list):
        return []
    failures: list[SearchFailure] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            engine = str(item[0]).strip()[:60] or "unknown"
            detail = str(item[1]).strip()[:200]
        elif isinstance(item, str):
            engine = "unknown"
            detail = item.strip()[:200]
        else:
            continue
        lowered = detail.lower()
        if "429" in lowered or "rate" in lowered or "limit" in lowered:
            kind = ProviderErrorKind.RATE_LIMIT
        elif "timeout" in lowered or "timed out" in lowered:
            kind = ProviderErrorKind.TIMEOUT
        elif "403" in lowered:
            kind = ProviderErrorKind.BAD_RESPONSE
        else:
            kind = ProviderErrorKind.NETWORK
        failures.append(SearchFailure(engine, kind.value, detail or "no response"))
    return failures


def _aggregate_kind(failures: list[SearchFailure]) -> ProviderErrorKind:
    kinds = {item.kind for item in failures}
    if kinds == {ProviderErrorKind.TIMEOUT.value}:
        return ProviderErrorKind.TIMEOUT
    if ProviderErrorKind.RATE_LIMIT.value in kinds:
        return ProviderErrorKind.RATE_LIMIT
    return ProviderErrorKind.NETWORK
