from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 常见 TUN 代理的合成 DNS 段，仅用于域名解析结果，不放行直接 IP 目标。
#
# 顺序有意义：open-websearch 2.2.0 的 isAllowedFakeIp 是
#   parsed.match(ipaddr.parseCIDR(cidr)) 套在 some() 里，整段包在 try/catch。
# 当被校验地址是 IPv6、而列表里先出现 IPv4 段时，ipaddr 的 IPv6.match 会
# 抛 "cannot match ipv6 address with non-ipv6 one"，异常被 catch 吞掉并返回
# false，于是该 IPv6 地址永远得不到豁免。IPv6 段必须排在前面。
DEFAULT_FAKE_IP_CIDRS = "2001:2::/48,198.18.0.0/15"


def order_fake_ip_cidrs(value: str) -> str:
    """把 IPv6 段排到 IPv4 段之前，规避 open-websearch 的顺序敏感缺陷。

    用户自定义 FAKE_IP_CIDRS 时同样适用；空项与多余空白一并清掉。
    """
    parts = [item.strip() for item in str(value).split(",") if item.strip()]
    v6 = [item for item in parts if ":" in item]
    v4 = [item for item in parts if ":" not in item]
    return ",".join(v6 + v4)


def _fake_ip_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """与 open-websearch 同名的 FAKE_IP_CIDRS：代理的合成 DNS 结果不算私网目标。"""
    networks = []
    raw = os.environ.get("FAKE_IP_CIDRS", DEFAULT_FAKE_IP_CIDRS)
    for item in order_fake_ip_cidrs(raw).split(","):
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _is_blocked_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    allowed: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_global:
        return False
    return not any(address in network for network in allowed)


def assert_public_fetch_url(url: str, label: str = "抓取地址") -> None:
    """抓取入口只接受公网 http(s) 地址，作为上游 daemon 校验之外的纵深防御。"""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(f"{label}必须是 http 或 https 链接")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError(f"{label}缺少主机名")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError(f"{label}不能指向本机地址")
    allowed = _fake_ip_networks()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_address(literal, ()):
            raise ValueError(f"{label}不能指向私网或本机地址")
        return
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except ValueError:
        raise ValueError(f"{label}端口不合法") from None
    except OSError as exc:
        raise ValueError(f"{label}无法解析主机名：{host}") from exc
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        if _is_blocked_address(address, allowed):
            raise ValueError(f"{label}解析到非公网地址，已拒绝抓取")


def normalize_search_engines(
    value: object, allowed: Sequence[str], limit: int = 0
) -> tuple[str, ...]:
    """把单个或一组引擎名规范化；auto 和空值表示使用配置默认；limit=0 表示不限。"""
    if value is None:
        return ()
    if isinstance(value, str):
        # Some providers serialize an array-valued tool argument as a JSON string.
        if value.strip().startswith("["):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError(
                    "search_engine must be a valid JSON array or engine name"
                ) from None
            if not isinstance(value, list):
                raise ValueError("search_engine must be a list of strings")
            return normalize_search_engines(value, allowed, limit)
        values = [value]
    elif isinstance(value, (list, tuple)):
        if not all(isinstance(item, str) for item in value):
            raise ValueError("search_engine entries must be strings")
        values = []
        for item in value:
            # 双重序列化：有的 provider 会把数组元素本身也编码成 JSON 字符串
            # （如 ["[\"baidu\"]"]），解析还原而不是当成引擎名报错。
            stripped = item.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except ValueError:
                    raise ValueError(
                        "search_engine must be a valid JSON array or engine name"
                    ) from None
                if not isinstance(parsed, list) or not all(
                    isinstance(inner, str) for inner in parsed
                ):
                    raise ValueError("search_engine must be a list of strings")
                values.extend(parsed)
            else:
                values.append(item)
    else:
        raise ValueError("search_engine must be a string or list of strings")

    selected: list[str] = []
    allowed_set = set(allowed)
    for item in values:
        name = item.strip().lower()
        if not name or name == "auto":
            continue
        if name not in allowed_set:
            raise ValueError(f"unsupported search engine {name}")
        if name not in selected:
            selected.append(name)
    if limit > 0 and len(selected) > limit:
        raise ValueError(f"at most {limit} search engines")
    return tuple(selected)


def normalize_search_providers(value: object, limit: int = 8) -> tuple[str, ...]:
    """规范化 LLM 推荐的 Provider 列表：去重、保序、限制长度。

    兼容部分模型把数组序列化成 JSON 字符串的调用方式；空值返回 ()。
    """
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                raise ValueError("providers must be a valid JSON array or provider name") from None
            if not isinstance(parsed, list):
                raise ValueError("providers must be a list of strings")
            value = parsed
        else:
            value = [text]
    if not isinstance(value, (list, tuple)):
        raise ValueError("providers must be a list of strings")
    if not all(isinstance(item, str) for item in value):
        raise ValueError("providers entries must be strings")
    selected: list[str] = []
    for item in value:
        name = item.strip().lower()
        if name and name not in selected:
            selected.append(name)
    if len(selected) > limit:
        raise ValueError(f"at most {limit} providers")
    return tuple(selected)


class ProviderErrorKind(str, Enum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    BAD_RESPONSE = "bad_response"
    UNAVAILABLE = "unavailable"
    # 鉴权失效/权限不足（401/403）与配额/余额限制（402、额度用尽）；
    # 不做快速重试，也不得把这类失败缓存成空结果。
    AUTH = "auth"
    QUOTA = "quota"
    # 本地单次搜索的请求次数预算耗尽：与厂商账户配额（quota）是两回事。
    BUDGET_EXHAUSTED = "budget_exhausted"


class ProviderError(Exception):
    def __init__(
        self,
        provider: str,
        kind: ProviderErrorKind | str,
        detail: str = "",
        *,
        code: str = "",
        pause_reason: str = "",
        resume_at: float | None = None,
    ):
        self.code = code
        self.pause_reason = pause_reason
        self.resume_at = resume_at
        self.provider = provider
        self.kind = kind if isinstance(kind, ProviderErrorKind) else ProviderErrorKind(kind)
        self.detail = detail
        super().__init__(f"{provider}: {self.kind.value}: {detail}".rstrip(": "))

    def to_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "kind": self.kind.value, "detail": self.detail}


@dataclass(slots=True)
class SearchQuery:
    query: str
    limit: int = 5
    engine: str = "auto"
    lang: str | None = None
    freshness: str | None = None
    # 指定上游搜索引擎（bing/baidu/sogou...）；空表示用 provider 的默认配置。
    engines: tuple[str, ...] = ()
    # 会话上下文里的补充关键词（人物全名、项目、产品），用于短名称消歧；
    # 只参与派生查询变体和相关性判断，不改变原始 query 的语义。
    context_terms: tuple[str, ...] = ()
    # 单次请求超时上限（秒）；由 router 按剩余预算下发，None 表示用 provider 默认。
    timeout: float | None = None
    # LLM 建议优先尝试的 SearchProvider；只是排序提示，失败或质量不足仍回退。
    # 空值 / "auto" 表示使用配置顺序。保留用于旧调用兼容。
    preferred_provider: str = ""
    # LLM 推荐的有序 Provider 列表；空表示使用配置顺序。
    providers: tuple[str, ...] = ()
    # auto：逐家尝试并允许回退；compare：尽量完成至少两家交叉验证；
    # strict：只调用 providers 指定的来源，不自动扩展。
    strategy: str = "auto"


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    published: str | None = None
    provider: str = ""
    # 结果实际来自哪个上游搜索引擎（bing/baidu/...）；provider 字段始终是 Syvrio 的
    # SearchProvider 名，二者不能混用。
    engine: str = ""


@dataclass(slots=True)
class FetchQuery:
    url: str
    max_chars: int = 4000


@dataclass(slots=True)
class FetchResult:
    url: str
    final_url: str
    title: str
    content: str
    truncated: bool = False
    site_name: str = ""


def truncate_fetch_content(content: str, max_chars: int) -> tuple[str, bool]:
    """正文按 max_chars 截断；返回（截断后文本, 是否发生截断）。"""
    if max_chars <= 0 or len(content) <= max_chars:
        return content, False
    return content[:max_chars], True


def normalize_url(url: str) -> str:
    """保守 URL 规范化：只合并 www.、utm_* 与普通锚点差异。

    m. 等站点变体与路径参数可能指向不同内容，不做激进合并；SPA 路由型
    片段（#/...、#!/...）也保留，避免把不同页面误合成一条。
    """
    url = url.strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.rstrip("/")
    if not parts.scheme or not parts.netloc:
        return url.rstrip("/")
    host = parts.netloc.lower()
    if host.startswith("www.") and "@" not in host:
        host = host[4:]
    try:
        query = urlencode(
            [
                (key, value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
                if not key.lower().startswith("utm_")
            ],
            doseq=True,
        )
    except ValueError:
        return url.rstrip("/")
    fragment = parts.fragment
    if fragment.startswith(("/", "!")):
        kept_fragment = fragment
    else:
        kept_fragment = ""
    return urlunsplit(
        (
            parts.scheme.lower(),
            host,
            parts.path.rstrip("/"),
            query,
            kept_fragment,
        )
    )


_DATE_PREFIX = re.compile(r"^\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T\s]|$)")


def parse_published_date(value: object) -> str | None:
    """只保留可信的发布日期，统一为 YYYY-MM-DD。

    非法日期、早于 1990 年或明显晚于今天（允许 1 天时区误差）都返回 None，
    避免伪造日期参与时效排序。
    """
    if not isinstance(value, str):
        return None
    match = _DATE_PREFIX.match(value)
    if not match:
        return None
    try:
        day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    if day.year < 1990 or day > date.today() + timedelta(days=1):
        return None
    return day.isoformat()


@dataclass(slots=True)
class SearchFailure:
    provider: str
    kind: str
    detail: str = ""

    @classmethod
    def from_error(cls, error: ProviderError) -> "SearchFailure":
        return cls(provider=error.provider, kind=error.kind.value, detail=error.detail)

    def to_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "kind": self.kind, "detail": self.detail}


@dataclass(slots=True)
class SearchOutcome:
    results: list[SearchResult] = field(default_factory=list)
    provider: str = ""
    failed: list[SearchFailure] = field(default_factory=list)
    hint: str = ""
    cached: bool = False
    # 实际尝试过的 Provider 顺序与策略，便于 TUI/调试区分回退与交叉验证。
    attempted: list[str] = field(default_factory=list)
    strategy: str = "auto"
    # compare 的验证状态：complete / incomplete / unknown；其他策略为空。
    verification: str = ""

    def to_payload(self) -> dict:
        payload: dict = {
            "results": [
                {
                    "title": result.title,
                    "url": result.url,
                    "snippet": result.snippet,
                    "published": result.published,
                    "provider": result.provider,
                    "engine": result.engine,
                }
                for result in self.results
            ],
            "provider": self.provider,
            "attempted": list(self.attempted),
            "strategy": self.strategy,
            "verification": self.verification,
            "failed": [failure.to_dict() for failure in self.failed],
        }
        if self.hint:
            payload["hint"] = self.hint
        return payload


class SearchProvider(Protocol):
    name: str
    capabilities: set[str]

    def search(self, query: SearchQuery) -> list[SearchResult]: ...
