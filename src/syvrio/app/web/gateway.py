"""Public Web listener that forwards browser requests to the local resident."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from .http import STATE_MODIFYING, _allowed_hosts, _error, _hostname

_REQUEST_HEADERS = {"accept", "content-type", "content-length", "if-none-match", "range", "x-syvrio-view"}
_HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "proxy-connection", "upgrade"}


def create_gateway(base: str, host: str, domains: list[str] | None = None, *,
                   locate: Callable[[], tuple[str, str] | None] | None = None):
    trusted = _allowed_hosts(host, domains)
    upstream = httpx.URL(base)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(None, connect=5)) as client:
            app.state.client = client
            yield

    gateway = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)

    @gateway.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def forward(request: Request, path: str):
        nonlocal upstream
        if _hostname(request.headers.get("host", "")) not in trusted:
            return _error("untrusted_host", "This Host is not allowed.", 403)
        origin = request.headers.get("origin")
        if request.method in STATE_MODIFYING and origin and _hostname(urlsplit(origin).netloc) not in trusted:
            return _error("untrusted_origin", "Cross-site write rejected.", 403)
        if path.startswith("api/resident"):
            return _error("not_found", "Not found.", 404)
        route = request.scope["raw_path"]
        query = request.scope.get("query_string", b"")
        url = upstream.copy_with(raw_path=route + (b"?" + query if query else b""))
        headers = {key: value for key, value in request.headers.items() if key.lower() in _REQUEST_HEADERS}
        client = request.app.state.client
        outgoing = client.build_request(request.method, url, headers=headers, content=request.stream())
        try:
            response = await client.send(outgoing, stream=True)
        except httpx.ConnectError:
            # A restarted resident can have a new ephemeral port. Only replay safe requests.
            endpoint = await asyncio.to_thread(locate) if locate else None
            if endpoint:
                upstream = httpx.URL(endpoint[0])
            if not endpoint or request.method not in {"GET", "HEAD"}:
                return _error("backend_unavailable", "The resident backend is unavailable.", 503)
            retry = client.build_request(request.method, upstream.copy_with(raw_path=url.raw_path), headers=headers)
            try:
                response = await client.send(retry, stream=True)
            except httpx.HTTPError:
                return _error("backend_unavailable", "The resident backend is unavailable.", 503)
        except httpx.HTTPError:
            return _error("backend_unavailable", "The resident backend is unavailable.", 503)
        return StreamingResponse(response.aiter_raw(), status_code=response.status_code,
                                 headers={k: v for k, v in response.headers.items() if k not in _HOP_HEADERS},
                                 background=BackgroundTask(response.aclose))

    return gateway
