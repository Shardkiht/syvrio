"""FastAPI entry: HTTP/SSE transport, static assets and request validation only.

All chat, memory, session and model behaviour stays in the Application layer.
This module deliberately contains no business logic.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ...infra.diagnostics import record_event
from .backend import WebBackend, WebError
from .models import (
    ApiErrorResponse,
    CancelRequest,
    CancelResponse,
    ChatRequest,
    CommandCatalogResponse,
    CommandRequest,
    CommandResponse,
    ActionResponse,
    CreateSessionResponse,
    DeleteSessionResponse,
    ErrorDetail,
    HealthResponse,
    HistoryResponse,
    ProviderActionRequest,
    ProviderListResponse,
    SessionListResponse,
    SwitchSessionResponse,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATE_MODIFYING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _hostname(value: str) -> str:
    """Host/Origin host without port, comparable across proxies."""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else value
    if ":" in value:
        return value.rsplit(":", 1)[0]
    return value


def _allowed_hosts(host: str, extra: list[str] | None = None) -> frozenset[str]:
    values = {"localhost", "127.0.0.1", "::1", host.strip().lower()}
    env = os.environ.get("SYVRIO_WEB_ALLOWED_HOSTS", "")
    values.update(item.strip().lower() for item in env.split(",") if item.strip())
    values.update(item.strip().lower() for item in (extra or []) if item.strip())
    return frozenset(values)


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=ApiErrorResponse(error=ErrorDetail(code=code, message=message)).model_dump(),
    )


def _static_root(override: Path | None) -> Path:
    if override is not None:
        return override
    env = os.environ.get("SYVRIO_WEB_STATIC")
    return Path(env) if env else Path(__file__).parent / "static"


def create_app(
    backend: WebBackend | None = None,
    *,
    autostart: bool = True,
    static_dir: Path | None = None,
    allowed_hosts: list[str] | None = None,
    host: str = DEFAULT_HOST,
) -> FastAPI:
    """Build the ASGI app. ``autostart=False`` when the caller already started it."""
    owner = backend or WebBackend()
    root = _static_root(static_dir)
    static_ready = (root / "index.html").is_file()
    trusted = _allowed_hosts(host, allowed_hosts)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if autostart:
            owner.startup()
        try:
            yield
        finally:
            owner.shutdown()

    app = FastAPI(
        title="Syvrio Web",
        version="0.1.0",
        description="Private single-user Web client for Syvrio.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        # X-Forwarded-* and similar proxy headers are intentionally ignored:
        # only Host and Origin are trusted, and both must be on the allowlist.
        host = _hostname(request.headers.get("host", ""))
        if host not in trusted:
            return _error("untrusted_host", "This Host is not allowed.", 403)
        if request.method in STATE_MODIFYING:
            origin = request.headers.get("origin")
            if origin:
                origin_host = _hostname(urlsplit(origin).netloc)
                if origin_host not in trusted:
                    return _error("untrusted_origin", "Cross-site write rejected.", 403)
        return await call_next(request)

    @app.exception_handler(WebError)
    async def _web_error(request: Request, exc: WebError):
        return _error(exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        return _error("invalid_request", "Invalid request payload.", 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        return _error("not_found" if exc.status_code == 404 else "http_error",
                      str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # Never leak a stack trace or chat content to the browser.
        record_event("web.unhandled", exc)
        return _error("internal_error", "Internal server error.", 500)

    @app.get("/api/health", response_model=HealthResponse, tags=["web"])
    async def health():
        return owner.health(static_ready=static_ready)

    @app.get("/api/sessions", response_model=SessionListResponse, tags=["web"])
    async def sessions(limit: int = Query(100, ge=1, le=500)):
        return await asyncio.to_thread(owner.list_sessions, limit)

    @app.post("/api/sessions", response_model=CreateSessionResponse,
              status_code=201, tags=["web"])
    async def create_session():
        return await asyncio.to_thread(owner.create_session)

    @app.post("/api/sessions/{session_id}/switch",
              response_model=SwitchSessionResponse, tags=["web"])
    async def switch_session(session_id: str):
        return await asyncio.to_thread(owner.switch_session, session_id)

    @app.delete("/api/sessions/{session_id}", response_model=DeleteSessionResponse,
                tags=["web"])
    async def delete_session(session_id: str, confirmed: bool = Query(False)):
        return await asyncio.to_thread(owner.delete_session, session_id, confirmed)

    @app.get("/api/sessions/{session_id}", response_model=HistoryResponse, tags=["web"])
    async def history(session_id: str, limit: int = Query(300, ge=1, le=300)):
        return await asyncio.to_thread(owner.history, session_id, limit)

    @app.get("/api/settings/providers", response_model=ProviderListResponse, tags=["web"])
    async def providers():
        return await asyncio.to_thread(owner.providers)

    @app.post("/api/settings/providers/{provider}", response_model=ActionResponse,
              tags=["web"])
    async def provider_action(provider: str, body: ProviderActionRequest):
        return await asyncio.to_thread(owner.provider_action, provider, body)

    @app.get("/api/commands", response_model=CommandCatalogResponse, tags=["web"])
    async def commands():
        return owner.command_catalog()

    @app.post("/api/commands", response_model=CommandResponse, tags=["web"])
    async def run_command(body: CommandRequest):
        return await asyncio.to_thread(
            owner.execute_command, body.line, body.session_id, body.confirmed
        )

    @app.post("/api/chat", tags=["web"])
    async def chat(body: ChatRequest):
        loop = asyncio.get_running_loop()
        turn = owner.start_turn(body.message, body.session_id, loop)
        return StreamingResponse(
            owner.stream(turn),
            media_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/chat/cancel", response_model=CancelResponse, tags=["web"])
    async def cancel(body: CancelRequest):
        cancelled = owner.cancel(body.turn_id)
        return CancelResponse(
            cancelled=cancelled,
            turn_id=body.turn_id,
            active_turn_id=owner.active_turn_id(),
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            return _error("not_found", "Not found.", 404)
        if not static_ready:
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "assets_missing",
                                   "message": "Web assets are not built. Run scripts/build_web.py."}},
            )
        resolved = root.resolve()
        candidate = (resolved / full_path).resolve()
        if candidate.is_file() and (candidate == resolved or resolved in candidate.parents):
            return FileResponse(candidate)
        return FileResponse(resolved / "index.html")

    return app


def run_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    config_path: Path | None = None,
    vault_path: Path | None = None,
    backend: WebBackend | None = None,
    domains: list[str] | None = None,
) -> None:
    """Run one uvicorn worker. A second client on the same Vault is rejected."""
    import uvicorn

    owner = backend or WebBackend(config_path, vault_path)
    try:
        owner.startup()
    except WebError as exc:
        raise RuntimeError(exc.message) from exc
    app = create_app(owner, autostart=False, host=host, allowed_hosts=domains)
    uvicorn.run(
        app,
        host=host,
        port=port,
        workers=1,
        access_log=False,  # ordinary logs must never contain chat content
        log_level="info",
    )
