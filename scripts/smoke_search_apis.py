"""多厂商 Search/Fetch 受控冒烟脚本：默认不联网、不计费。

不带 --live：只做本地检查（Key 是否可读、厂商按当前配置是否会注册），
不发任何网络请求。
带 --live：对指定厂商发起最多 --max-requests（默认 1）次真实请求；
不自动测试全部厂商，不自动重试。

用法：
  uv run python scripts/smoke_search_apis.py
  uv run python scripts/smoke_search_apis.py --provider tavily --operation search
  uv run python scripts/smoke_search_apis.py --provider volcengine --operation fetch --live

Key 从环境变量或系统凭据读取，输出只含状态与数量，不回显 Key。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from syvrio.infra.config import load_config, read_api_key  # noqa: E402
from syvrio.infra.search.base import (  # noqa: E402
    FetchQuery,
    ProviderError,
    SearchQuery,
    assert_public_fetch_url,
)
from syvrio.infra.search.fetch_router import (  # noqa: E402
    _EXTERNAL_FETCH_PROVIDERS,
    _OPTIONAL_FETCH_KEY_PROVIDERS,
    build_fetch_registry,
)
from syvrio.infra.search.router import (  # noqa: E402
    _EXTERNAL_PROVIDERS,
    _OPTIONAL_KEY_PROVIDERS,
    build_registry,
)

# 冒烟用中性查询与公开页面，不含任何个人数据。
SMOKE_QUERY = "OpenAI"
SMOKE_URL = "https://example.com"

# 各厂商尚未经真实响应核实的字段，随报告输出，提醒冒烟时重点确认。
UNVERIFIED_FIELDS = {
    "baidu_search": "鉴权头用 Authorization 还是 X-Appbuilder-Authorization",
    "tavily": "无",
    "exa": "无（Search 固定 type=auto；Contents text 默认关闭需显式开启）",
    "you": "Contents 响应字段形状（markdown/title）与 Search 日期字段",
    "volcengine": "Fetch 的 Result.Title/Content 字段名与 Custom NeedContent 行为",
    "open_websearch": "无",
    "anysearch": "匿名 zone/language 行为与免费调用限制",
    "parallel_mcp": "匿名速率和厂商额度",
    "searxng": "实例是否启用 JSON 输出与 publishedDate 字段形状",
}


def _search_key_env(name: str) -> str | None:
    entry = _EXTERNAL_PROVIDERS.get(name)
    if entry is not None:
        return entry[1]
    return _OPTIONAL_KEY_PROVIDERS.get(name)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="多厂商搜索/抓取受控冒烟")
    parser.add_argument("--provider", help="只检查/调用指定厂商；缺省列出全部")
    parser.add_argument("--operation", choices=("search", "fetch"), default="search")
    parser.add_argument(
        "--live",
        action="store_true",
        help="允许真实联网调用；不带此参数绝不发请求",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=1,
        help="本次 --live 最多发起的真实请求次数（默认 1）",
    )
    return parser


def _dry_report(config) -> list[dict]:
    """本地检查：Key 可读性与注册预判，零网络请求。"""
    rows = []
    for operation, listed in (
        ("search", config.search.providers),
        ("fetch", config.fetch.providers),
    ):
        optional = (
            _OPTIONAL_KEY_PROVIDERS if operation == "search" else _OPTIONAL_FETCH_KEY_PROVIDERS
        )
        for name in listed:
            if operation == "search":
                env_name = _search_key_env(name)
            else:
                env_name = (
                    optional.get(name) or _EXTERNAL_FETCH_PROVIDERS.get(name, (None, None))[1]
                )
            key_present = None if env_name is None else bool(read_api_key(env_name))
            # 匿名 Provider 无 Key 也注册；官方厂商必须有 Key。
            would_register = env_name is None or name in optional or key_present
            row = {
                "provider": name,
                "operation": operation,
                "in_config": True,
                "key_present": key_present,
                "would_register": would_register,
                "unverified_fields": UNVERIFIED_FIELDS.get(name, "无"),
            }
            rows.append(row)
    return rows


def _live_search(config, provider: str, max_requests: int) -> dict:
    registry = build_registry(config.search)
    instance = registry.get(provider)
    if instance is None:
        return {
            "provider": provider,
            "operation": "search",
            "ok": False,
            "error_kind": "not_registered",
            "note": "厂商未注册：检查 search.providers 与对应 API Key",
        }
    total = 0
    started = time.perf_counter()
    try:
        results = []
        for _ in range(max(1, max_requests)):
            # 上限内重试空结果，成功即停；失败直接上报，不自动重试。
            results = instance.search(SearchQuery(query=SMOKE_QUERY, limit=5))
            total += 1
            if results:
                break
    except ProviderError as exc:
        return {
            "provider": provider,
            "operation": "search",
            "ok": False,
            "requests": total,
            "error_kind": exc.kind.value,
            "detail": exc.detail,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "unverified_fields": UNVERIFIED_FIELDS.get(provider, "无"),
        }
    return {
        "provider": provider,
        "operation": "search",
        "ok": True,
        "requests": total,
        "result_count": len(results),
        "first_url": results[0].url if results else "",
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "unverified_fields": UNVERIFIED_FIELDS.get(provider, "无"),
    }


def _live_fetch(config, provider: str) -> dict:
    registry = build_fetch_registry(config.fetch, config.search)
    instance = registry.get(provider)
    if instance is None:
        return {
            "provider": provider,
            "operation": "fetch",
            "ok": False,
            "error_kind": "not_registered",
            "note": "厂商未注册：检查 fetch.providers 与对应 API Key",
        }
    started = time.perf_counter()
    try:
        assert_public_fetch_url(SMOKE_URL)
        result = instance.fetch(FetchQuery(SMOKE_URL, 4000))
        if result.final_url and result.final_url != SMOKE_URL:
            assert_public_fetch_url(result.final_url)
    except ProviderError as exc:
        return {
            "provider": provider,
            "operation": "fetch",
            "ok": False,
            "requests": 1,
            "error_kind": exc.kind.value,
            "detail": exc.detail,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "unverified_fields": UNVERIFIED_FIELDS.get(provider, "无"),
        }
    except ValueError:
        return {
            "provider": provider,
            "operation": "fetch",
            "ok": False,
            "error_kind": "unsafe_url",
        }
    return {
        "provider": provider,
        "operation": "fetch",
        "ok": True,
        "requests": 1,
        "content_chars": len(result.content),
        "truncated": result.truncated,
        "final_url": result.final_url,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "unverified_fields": UNVERIFIED_FIELDS.get(provider, "无"),
    }


def main(argv: list[str] | None = None, config=None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = config or load_config()
    if not args.live:
        rows = _dry_report(config)
        if args.provider:
            rows = [
                row
                for row in rows
                if row["provider"] == args.provider and row["operation"] == args.operation
            ]
        print(json.dumps({"mode": "dry", "rows": rows}, ensure_ascii=False, indent=2))
        return 0

    if not args.provider:
        print(
            json.dumps(
                {"mode": "live", "error": "--live 必须配合 --provider 指定单一厂商"},
                ensure_ascii=False,
            )
        )
        return 2
    if args.max_requests < 1 or args.max_requests > 10:
        print(json.dumps({"mode": "live", "error": "--max-requests 取值 1-10"}, ensure_ascii=False))
        return 2
    if args.operation == "search":
        report = _live_search(config, args.provider, args.max_requests)
    else:
        report = _live_fetch(config, args.provider)
    print(json.dumps({"mode": "live", "reports": [report]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
