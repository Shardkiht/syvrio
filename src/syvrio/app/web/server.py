"""FastAPI entry: HTTP/SSE transport, static assets and request validation only.

All chat, memory, session and model behaviour stays in the Application layer.
This module deliberately contains no business logic.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException, MultiPartParser

from ...application.commands import parse_command
from ...infra.attachments import MAX_BATCH_BYTES, MAX_FILES, AttachmentBatch
from ...infra.diagnostics import record_event
from ...infra.documents import DocumentError
from .backend import WebBackend, WebError
from .http import STATE_MODIFYING, _allowed_hosts, _error, _hostname
from .models import (
    ActionResponse,
    CancelRequest,
    CancelResponse,
    ChatRequest,
    CommandCatalogResponse,
    CommandRequest,
    CommandResponse,
    ContextUsageResponse,
    CreateSessionResponse,
    DeleteSessionResponse,
    HealthResponse,
    HistoryResponse,
    InsertRequest,
    MessageDraft,
    ModelSearchResponse,
    ProviderActionRequest,
    ProviderListResponse,
    SessionDeletionPreview,
    SessionListResponse,
    SwitchSessionResponse,
)
from .views import ClientViews

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


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
    resident_token: str | None = None,
) -> FastAPI:
    """Build the ASGI app. ``autostart=False`` when the caller already started it."""
    owner = backend or WebBackend()
    views = ClientViews(owner)

    def view_key(request: Request) -> str:
        return request.headers.get("x-syvrio-view", "")

    def channel(request: Request) -> WebBackend:
        return views.channel(view_key(request))
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
            views.shutdown()

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
        return _error(
            "not_found" if exc.status_code == 404 else "http_error",
            str(exc.detail),
            exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # Never leak a stack trace or chat content to the browser.
        record_event("web.unhandled", exc)
        return _error("internal_error", "Internal server error.", 500)

    if resident_token:
        from ...backend.mcp_writes import MCPWrites
        from ...backend.operations import ResidentOperations

        operations = ResidentOperations(owner)
        mcp_writes = MCPWrites(owner._service)

        def authenticate(request: Request) -> None:
            supplied = request.headers.get("x-syvrio-token", "")
            if not secrets.compare_digest(supplied.encode(), resident_token.encode()):
                raise WebError("forbidden", "Local credentials required.", 403)

        @app.get("/api/resident", include_in_schema=False)
        def resident_info(request: Request):
            authenticate(request)
            return {"protocol": 1}

        @app.post("/api/resident/{operation}", include_in_schema=False)
        def local_operation(request: Request, operation: str, body: dict):
            authenticate(request)
            if operation == "shutdown":
                request.app.state.stop_resident()
                return {"stopping": True}
            try:
                if operation == "mcp_write":
                    return mcp_writes.apply(view_key(request), **body)
                return operations.dispatch(operation, body)
            except KeyError as exc:
                raise WebError("not_found", str(exc), 404) from exc
            except (TypeError, ValueError) as exc:
                raise WebError("invalid_operation", str(exc), 422) from exc

    @app.get("/api/health", response_model=HealthResponse, tags=["web"])
    async def health(request: Request):
        return channel(request).health(static_ready=static_ready)

    @app.get("/api/context-usage", response_model=ContextUsageResponse, tags=["web"])
    async def context_usage(request: Request, session_id: str | None = None):
        return await asyncio.to_thread(channel(request).context_usage, session_id)

    @app.get("/api/sessions", response_model=SessionListResponse, tags=["web"])
    async def sessions(request: Request, limit: int = Query(100, ge=1, le=500)):
        return await asyncio.to_thread(channel(request).list_sessions, limit)

    @app.post("/api/sessions", response_model=CreateSessionResponse, status_code=201, tags=["web"])
    async def create_session(request: Request):
        return await asyncio.to_thread(views.new, view_key(request))

    @app.post("/api/view/disconnect", status_code=204, tags=["web"])
    async def disconnect(request: Request):
        await asyncio.to_thread(views.disconnect, view_key(request))

    @app.post(
        "/api/sessions/{session_id}/switch", response_model=SwitchSessionResponse, tags=["web"]
    )
    async def switch_session(request: Request, session_id: str):
        return await asyncio.to_thread(views.switch, view_key(request), session_id)

    @app.put("/api/sessions/{session_id}/title", status_code=204, tags=["sessions"])
    def rename_session(request: Request, session_id: str, body: dict):
        title = body.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise WebError("invalid_title", "Enter a session title of up to 500 characters.", 422)
        views.rename(view_key(request), session_id, title.strip())

    @app.get("/api/sessions/{session_id}/deletion-preview", response_model=SessionDeletionPreview, tags=["web"])
    async def deletion_preview(request: Request, session_id: str):
        return await asyncio.to_thread(channel(request).deletion_preview, session_id)

    @app.delete("/api/sessions/{session_id}", response_model=DeleteSessionResponse, tags=["web"])
    async def delete_session(
        request: Request, session_id: str, confirmed: bool = Query(False),
        pending_revision: str = Query("", max_length=64),
    ):
        return await asyncio.to_thread(views.delete, view_key(request), session_id, confirmed, pending_revision)

    @app.get("/api/sessions/{session_id}", response_model=HistoryResponse, tags=["web"])
    async def history(request: Request, session_id: str, limit: int = Query(300, ge=1, le=300)):
        return await asyncio.to_thread(channel(request).history, session_id, limit)

    @app.get("/api/settings/models", response_model=ModelSearchResponse, tags=["web"])
    async def search_models(q: str = Query("", max_length=512), refresh: bool = False):
        return await asyncio.to_thread(owner.search_models, q, refresh)

    @app.get("/api/settings/providers", response_model=ProviderListResponse, tags=["web"])
    async def providers():
        return await asyncio.to_thread(owner.providers)

    @app.get("/api/sessions/{session_id}/messages/{message_id}/draft", response_model=MessageDraft, tags=["web"])
    def edit_draft(request: Request, session_id: str, message_id: str):
        return channel(request).edit_draft(session_id, message_id)

    @app.get("/api/sessions/{session_id}/messages/{message_id}/attachments/{attachment_id}", tags=["web"])
    def download_attachment(session_id: str, message_id: str, attachment_id: str):
        owner._validate_session_id(session_id)
        try:
            session = owner._sessions.load(session_id, include_reasoning=False)
        except KeyError as exc:
            raise WebError("session_not_found", "Session not found.", 404) from exc
        for record in session.active_records():
            if record.get("id") != message_id or record.get("role") != "user":
                continue
            for meta in record.get("attachments", []):
                if meta.get("id") == attachment_id:
                    try:
                        batch = AttachmentBatch.restore(owner._service.paths.attachments_dir, session_id, message_id, [meta])
                    except (DocumentError, ValueError, TypeError, OSError) as exc:
                        raise WebError("attachment_unavailable", "Restore the matching attachment archive.", 404) from exc
                    item = batch.files[0]
                    return FileResponse(batch.folder / item.filename, media_type="application/octet-stream",
                                        filename=item.name, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        raise WebError("attachment_not_found", "Attachment not found in this message.", 404)

    @app.post("/api/settings/providers/{provider}", response_model=ActionResponse, tags=["web"])
    async def provider_action(provider: str, body: ProviderActionRequest):
        return await asyncio.to_thread(owner.provider_action, provider, body)

    @app.get("/api/commands", response_model=CommandCatalogResponse, tags=["web"])
    async def commands():
        return owner.command_catalog()

    @app.post("/api/commands", response_model=CommandResponse, tags=["web"])
    async def run_command(request: Request, body: CommandRequest):
        line = body.line
        if body.pending_text is not None and body.inference_text is not None:
            raise WebError("invalid_request", "Supply only one edited text field.", 422)
        text = body.pending_text if body.pending_text is not None else body.inference_text
        if text is not None:
            command, action = (
                ("pending", "edit") if body.pending_text is not None else ("inferences", "correct")
            )
            name, argument = parse_command(line)
            parts = argument.split()
            if name != command or len(parts) != 2 or parts[0].lower() != action:
                raise WebError(
                    "invalid_request", f"Edited text requires /{command} {action} <id>.", 422
                )
            if not text.strip():
                raise WebError("invalid_request", "Edited text cannot be blank.", 422)
            line = f"/{command} {action} {parts[1]} {text}"
        if body.expected_message_id is not None and (parse_command(line)[0] != "undo" or not body.session_id):
            raise WebError("invalid_request", "A message target requires /undo and a session.", 422)
        if parse_command(line)[0] == "new":
            return await asyncio.to_thread(views.new_command, view_key(request), body.session_id)
        return await asyncio.to_thread(
            channel(request).execute_command, line, body.session_id, body.confirmed, body.expected_message_id
        )

    @app.post("/api/chat", tags=["web"])
    async def chat(request: Request, body: ChatRequest):
        return await stream_reply(request, body)

    @app.post("/api/chat/attachments", tags=["web"])
    async def chat_with_files(request: Request):
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            raise WebError("invalid_upload", "Send files as multipart form data.", 415)

        async def limited_body():
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BATCH_BYTES + 512 * 1024:
                    raise WebError("upload_too_large", "Attachment batch exceeds 40 MiB.", 413)
                yield chunk

        try:
            form = await MultiPartParser(
                request.headers, limited_body(), max_files=MAX_FILES, max_fields=2,
            ).parse()
        except MultiPartException as exc:
            raise WebError("invalid_upload", "Invalid multipart upload.", 422) from exc
        try:
            message = form.get("message", "")
            session_id = form.get("session_id") or None
            uploads = form.getlist("files")
            if (not isinstance(message, str) or len(message) > 100_000
                    or session_id is not None and not isinstance(session_id, str)
                    or not all(isinstance(item, UploadFile) for item in uploads)):
                raise WebError("invalid_upload", "Invalid attachment or message.", 422)
            body = ChatRequest(message=message.strip() or "Analyze the attached files.", session_id=session_id)
            try:
                batch = await asyncio.to_thread(
                    AttachmentBatch.stage, owner._service.paths.attachments_dir,
                    [(item.filename or "", item.file) for item in uploads],
                )
            except DocumentError as exc:
                raise WebError("invalid_upload", str(exc), 422) from exc
        finally:
            await form.close()
        try:
            return await stream_reply(request, body, batch)
        except Exception:
            batch.discard()
            raise

    async def stream_reply(request: Request, body: ChatRequest, attachments: AttachmentBatch | None = None):
        loop = asyncio.get_running_loop()
        selected, turn = await asyncio.to_thread(
            views.start_turn, view_key(request), body.message, body.session_id, loop, attachments
        )
        if request.query_params.get("receipt") == "1":
            return JSONResponse(turn.receipt(), status_code=202)
        return StreamingResponse(
            selected.stream(turn),
            media_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/chat/queue", tags=["chat"])
    def queue_state(request: Request):
        return channel(request).queue_state()

    @app.delete("/api/chat/queue/{turn_id}", status_code=204, tags=["chat"])
    def remove_queued(request: Request, turn_id: int):
        channel(request).remove_queued(turn_id)

    @app.get("/api/chat/turns/{turn_id}", tags=["chat"])
    async def replay(request: Request, turn_id: int, after: int = Query(0, ge=0)):
        selected = channel(request)
        turn = selected._turns.get(turn_id, selected._conversation.current_id)
        if turn is None:
            raise WebError("turn_not_found", "This turn is unavailable. Reload session history.", 404)
        return StreamingResponse(selected.stream(turn, after), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    @app.post("/api/chat/insert", status_code=204, tags=["web"])
    async def insert(request: Request, body: InsertRequest):
        await asyncio.to_thread(channel(request).insert, body.turn_id, body.session_id, body.message)

    @app.post("/api/chat/cancel", response_model=CancelResponse, tags=["web"])
    async def cancel(request: Request, body: CancelRequest):
        selected = channel(request)
        cancelled = selected.cancel(body.turn_id)
        return CancelResponse(
            cancelled=cancelled,
            turn_id=body.turn_id,
            active_turn_id=selected.active_turn_id(),
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            return _error("not_found", "Not found.", 404)
        if not static_ready:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "assets_missing",
                        "message": "Web assets are not built. Run scripts/build_web.py.",
                    }
                },
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
    """Serve browser assets and requests without owning the Vault or background tasks."""
    import uvicorn

    from ...backend.endpoint import client_paths, connect, discover
    from .gateway import create_gateway

    _, paths = client_paths(config_path, vault_path)
    base, _ = connect(paths)
    uvicorn.run(create_gateway(base, host, domains, locate=lambda: discover(paths)), host=host, port=port, workers=1, access_log=False)


def serve_resident(config_path: Path, vault_path: Path) -> None:
    import uvicorn

    from ...backend.endpoint import client_paths, publish
    from ...backend.ownership import BackendLease

    _, paths = client_paths(config_path, vault_path)
    lease = BackendLease(paths)
    lease.acquire(timeout=0)
    try:
        owner = WebBackend(config_path, vault_path)
        owner.startup(ownership=lease)
    except BaseException:
        lease.release()
        raise
    token = secrets.token_urlsafe(32)
    try:
        with socket.socket() as sock:
            sock.bind((DEFAULT_HOST, 0))
            sock.listen()
            port = sock.getsockname()[1]
            app = create_app(owner, autostart=False, resident_token=token)
            server = uvicorn.Server(uvicorn.Config(app, host=DEFAULT_HOST, port=port, access_log=False))
            app.state.stop_resident = lambda: setattr(server, "should_exit", True)
            descriptor = publish(paths, port, token)
            try:
                server.run(sockets=[sock])
            finally:
                # A successor can publish after the lifespan released ownership.
                import json
                try:
                    if json.loads(descriptor.read_bytes()).get("token") == token:
                        descriptor.unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
    finally:
        owner.shutdown()
