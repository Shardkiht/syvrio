"""将浏览器请求转发到本地常驻服务的网页监听入口。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from ...backend.api.http import error_response, guard_hosts
from .assets import WebAssets
from .http import web_hosts

_REQUEST_HEADERS = {
    "accept",
    "content-type",
    "content-length",
    "if-none-match",
    "if-range",
    "range",
    "x-syvrio-view",
    "x-syvrio-edit",
}
_HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "proxy-connection", "upgrade"}
_API_DOCS = {"openapi.json", "docs", "docs/oauth2-redirect", "redoc"}


def create_gateway(
    base: str,
    host: str,
    domains: list[str] | None = None,
    *,
    locate: Callable[[], tuple[str, str] | None] | None = None,
    static_dir: Path | None = None,
    token: str | None = None,
) -> FastAPI:
    assets = WebAssets(static_dir)
    upstream = httpx.URL(base)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            trust_env=False, timeout=httpx.Timeout(None, connect=5)
        ) as client:
            app.state.client = client
            yield

    gateway = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)
    guard_hosts(gateway, host, web_hosts(domains))

    @gateway.api_route(
        "/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    )
    async def forward(request: Request, path: str):
        nonlocal upstream, token
        if path.startswith("api/resident"):
            return error_response("not_found", "Not found.", 404)
        if path != "api" and not path.startswith("api/") and path not in _API_DOCS:
            if request.method in {"GET", "HEAD"}:
                return assets.serve(path)
            return error_response("http_error", "Method Not Allowed", 405)
        route = request.scope["raw_path"]
        query = request.scope.get("query_string", b"")
        url = upstream.copy_with(raw_path=route + (b"?" + query if query else b""))
        headers = {
            key: value for key, value in request.headers.items() if key.lower() in _REQUEST_HEADERS
        }
        # 软件更新接口仅接受来自回环网关的授权，包括经 GitHub 验证的分享入口。
        # 不转发浏览器令牌。
        admin = path == "api/update" and host in {"localhost", "127.0.0.1", "::1"}
        if admin and token:
            headers["X-Syvrio-Token"] = token
        client = request.app.state.client
        outgoing = client.build_request(
            request.method, url, headers=headers, content=request.stream()
        )
        try:
            response = await client.send(outgoing, stream=True)
        except httpx.ConnectError:
            # 常驻服务重启后临时端口可能变更，只重放可安全重复的请求。
            endpoint = await asyncio.to_thread(locate) if locate else None
            if endpoint:
                upstream = httpx.URL(endpoint[0])
                token = endpoint[1]
                if admin:
                    headers["X-Syvrio-Token"] = token
            if not endpoint or request.method not in {"GET", "HEAD"}:
                return error_response(
                    "backend_unavailable", "The resident backend is unavailable.", 503
                )
            retry = client.build_request(
                request.method, upstream.copy_with(raw_path=url.raw_path), headers=headers
            )
            try:
                response = await client.send(retry, stream=True)
            except httpx.HTTPError:
                return error_response(
                    "backend_unavailable", "The resident backend is unavailable.", 503
                )
        except httpx.HTTPError:
            return error_response(
                "backend_unavailable", "The resident backend is unavailable.", 503
            )
        if path == "api/health" and request.method == "GET" and response.status_code == 200:
            try:
                await response.aread()
                health = response.json()
                health["static_ready"] = assets.ready
                return JSONResponse(health)
            except (httpx.HTTPError, ValueError, TypeError):
                return error_response(
                    "backend_unavailable", "The resident returned an invalid health response.", 503
                )
            finally:
                await response.aclose()
        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers={k: v for k, v in response.headers.items() if k not in _HOP_HEADERS},
            background=BackgroundTask(response.aclose),
        )

    return gateway
