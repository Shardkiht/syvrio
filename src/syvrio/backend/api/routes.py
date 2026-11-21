"""Shared HTTP and SSE routes of the resident backend."""

from __future__ import annotations

import asyncio
import re
import secrets
from uuid import UUID

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.types import Lifespan

from ...application.commands import parse_command
from ...errors import ConversationError
from ...infra.attachments import MAX_BATCH_BYTES, MAX_FILES, AttachmentBatch
from ...infra.diagnostics import record_event
from ...infra.documents import DocumentError
from .channel import HISTORY_ITEM_LIMIT, APIChannel, APIError
from .http import error_response, guard_hosts
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
    HealthResponse,
    HistoryResponse,
    InsertRequest,
    MessageDraft,
    ModelSearchResponse,
    ProviderActionRequest,
    ProviderListResponse,
    SessionDeletionPreview,
    SessionListResponse,
    SwitchSessionHistoryResponse,
    SwitchSessionResponse,
)
from .views import ClientViews


def create_api(
    owner: APIChannel,
    *,
    lifespan: Lifespan[FastAPI] | None = None,
    static_ready: bool = False,
    allowed_hosts: list[str] | None = None,
    host: str = "127.0.0.1",
    resident_token: str | None = None,
) -> FastAPI:
    """Expose the shared protocol for an owner whose lifetime is set by the caller."""
    views = ClientViews(owner)

    def view_key(request: Request) -> str:
        key = request.headers.get("x-syvrio-view", "")
        if key:
            try:
                UUID(key)
            except ValueError as exc:
                raise APIError("invalid_view", "Invalid client view.", 422) from exc
        return key

    def channel(request: Request) -> APIChannel:
        return owner.directory.channel(view_key(request))

    app = FastAPI(
        title="Syvrio API",
        version="0.1.0",
        description="Shared API for clients of the local Syvrio backend.",
        lifespan=lifespan,
    )

    guard_hosts(app, host, allowed_hosts)

    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError):
        return error_response(exc.code, exc.message, exc.status)

    @app.exception_handler(ConversationError)
    async def _conversation_error(request: Request, exc: ConversationError):
        return await _api_error(request, APIError.from_conversation(exc))

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        return error_response("invalid_request", "Invalid request payload.", 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        return error_response(
            "not_found" if exc.status_code == 404 else "http_error",
            str(exc.detail),
            exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # Never leak a stack trace or chat content to the browser.
        record_event("web.unhandled", exc)
        return error_response("internal_error", "Internal server error.", 500)

    if resident_token:
        from ..mcp_writes import MCPWrites
        from ..operations import ResidentOperations

        operations = ResidentOperations(owner)
        mcp_writes = MCPWrites(owner._service)

        def authenticate(request: Request) -> None:
            supplied = request.headers.get("x-syvrio-token", "")
            if not secrets.compare_digest(supplied.encode(), resident_token.encode()):
                raise APIError("forbidden", "Local credentials required.", 403)

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
                raise APIError("not_found", str(exc), 404) from exc
            except (TypeError, ValueError) as exc:
                raise APIError("invalid_operation", str(exc), 422) from exc

    @app.get("/api/health", response_model=HealthResponse, tags=["web"])
    async def health(request: Request):
        return channel(request).health(static_ready=static_ready)

    @app.get("/api/context-usage", response_model=ContextUsageResponse, tags=["web"])
    async def context_usage(request: Request, session_id: str | None = None):
        return await asyncio.to_thread(channel(request).context_usage, session_id)

    @app.get("/api/sessions", response_model=SessionListResponse, tags=["web"])
    async def sessions(
        request: Request,
        limit: int = Query(100, ge=1, le=500),
        archived: bool = False,
        offset: int = Query(0, ge=0),
    ):
        return await asyncio.to_thread(
            channel(request).list_sessions, limit, archived=archived, offset=offset
        )

    @app.post("/api/sessions", response_model=CreateSessionResponse, status_code=201, tags=["web"])
    async def create_session(request: Request):
        return await asyncio.to_thread(views.create_session, view_key(request))

    @app.post("/api/view/disconnect", status_code=204, tags=["web"])
    async def disconnect(request: Request):
        await asyncio.to_thread(owner.directory.disconnect, view_key(request))

    @app.post(
        "/api/sessions/{session_id}/switch",
        response_model=SwitchSessionHistoryResponse | SwitchSessionResponse,
        tags=["web"],
    )
    async def switch_session(
        request: Request,
        session_id: str,
        include_history: bool = False,
        known_version: str | None = Query(None, max_length=128),
    ):
        return await asyncio.to_thread(
            views.switch, view_key(request), session_id, include_history, known_version
        )

    @app.put("/api/sessions/{session_id}/title", status_code=204, tags=["sessions"])
    def rename_session(request: Request, session_id: str, body: dict):
        title = body.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise APIError("invalid_title", "Enter a session title of up to 500 characters.", 422)
        owner._validate_session_id(session_id)
        owner.directory.rename(view_key(request), session_id, title.strip())

    @app.post("/api/sessions/{session_id}/archive", tags=["sessions"])
    async def archive_session(request: Request, session_id: str):
        view_key(request)
        return await asyncio.to_thread(views.archive, session_id, True)

    @app.post("/api/sessions/{session_id}/restore", tags=["sessions"])
    async def restore_session(request: Request, session_id: str):
        view_key(request)
        return await asyncio.to_thread(views.archive, session_id, False)

    @app.get(
        "/api/sessions/{session_id}/deletion-preview",
        response_model=SessionDeletionPreview,
        tags=["web"],
    )
    async def deletion_preview(request: Request, session_id: str):
        return await asyncio.to_thread(owner.deletion_preview, session_id)

    @app.delete("/api/sessions/{session_id}", tags=["web"])
    async def delete_session(
        request: Request,
        session_id: str,
        confirmed: bool = Query(False),
        permanent: bool = Query(False),
        pending_revision: str = Query("", max_length=64),
    ):
        view_key(request)
        if permanent:
            return await asyncio.to_thread(
                owner.delete_session, session_id, confirmed, pending_revision
            )
        return await asyncio.to_thread(views.archive, session_id, True)

    @app.get("/api/sessions/{session_id}", response_model=HistoryResponse, tags=["web"])
    async def history(
        request: Request,
        response: Response,
        session_id: str,
        limit: int = Query(HISTORY_ITEM_LIMIT, ge=1, le=HISTORY_ITEM_LIMIT),
        archived: bool = False,
        include_version: bool = False,
    ):
        if include_version and not archived and limit == HISTORY_ITEM_LIMIT:
            version, body = await asyncio.to_thread(
                views.versioned_history, view_key(request), session_id
            )
            response.headers["X-Syvrio-History-Version"] = version
            return body
        return await asyncio.to_thread(
            channel(request).history, session_id, limit, archived=archived
        )

    @app.get("/api/settings/models", response_model=ModelSearchResponse, tags=["web"])
    async def search_models(q: str = Query("", max_length=512), refresh: bool = False):
        return await asyncio.to_thread(owner.search_models, q, refresh)

    @app.get("/api/settings/providers", response_model=ProviderListResponse, tags=["web"])
    async def providers():
        return await asyncio.to_thread(owner.providers)

    @app.get(
        "/api/sessions/{session_id}/messages/{message_id}/draft",
        response_model=MessageDraft,
        tags=["web"],
    )
    def edit_draft(request: Request, session_id: str, message_id: str):
        return channel(request).edit_draft(session_id, message_id)

    @app.get(
        "/api/sessions/{session_id}/messages/{message_id}/attachments/{attachment_id}", tags=["web"]
    )
    def download_attachment(
        session_id: str, message_id: str, attachment_id: str, archived: bool = False
    ):
        owner._validate_session_id(session_id)
        try:
            with owner._service.write_lock:
                session = owner._sessions.load(
                    session_id, include_reasoning=False, archived=archived
                )
        except KeyError as exc:
            raise APIError("session_not_found", "Session not found.", 404) from exc
        for record in session.active_records():
            if record.get("id") != message_id or record.get("role") != "user":
                continue
            for meta in record.get("attachments", []):
                if meta.get("id") == attachment_id:
                    try:
                        batch = AttachmentBatch.restore(
                            owner._service.paths.attachments_dir, session_id, message_id, [meta]
                        )
                    except (DocumentError, ValueError, TypeError, OSError) as exc:
                        raise APIError(
                            "attachment_unavailable",
                            "Restore the matching attachment archive.",
                            404,
                        ) from exc
                    item = batch.files[0]
                    return FileResponse(
                        batch.folder / item.filename,
                        media_type="application/octet-stream",
                        filename=item.name,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                    )
        raise APIError("attachment_not_found", "Attachment not found in this message.", 404)

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
            raise APIError("invalid_request", "Supply only one edited text field.", 422)
        text = body.pending_text if body.pending_text is not None else body.inference_text
        if text is not None:
            command, action = (
                ("pending", "edit") if body.pending_text is not None else ("inferences", "correct")
            )
            name, argument = parse_command(line)
            parts = argument.split()
            if name != command or len(parts) != 2 or parts[0].lower() != action:
                raise APIError(
                    "invalid_request", f"Edited text requires /{command} {action} <id>.", 422
                )
            if not text.strip():
                raise APIError("invalid_request", "Edited text cannot be blank.", 422)
            line = f"/{command} {action} {parts[1]} {text}"
        if body.expected_message_id is not None and (
            parse_command(line)[0] != "undo" or not body.session_id
        ):
            raise APIError("invalid_request", "A message target requires /undo and a session.", 422)
        if parse_command(line)[0] == "new":
            return await asyncio.to_thread(views.new_command, view_key(request), body.session_id)
        return await asyncio.to_thread(
            channel(request).execute_command,
            line,
            body.session_id,
            body.confirmed,
            body.expected_message_id,
        )

    @app.post("/api/chat", tags=["web"])
    async def chat(
        request: Request,
        body: ChatRequest,
        replace: str | None = Query(None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$"),
    ):
        return await stream_reply(request, body, replace=replace)

    @app.post("/api/chat/attachments", tags=["web"])
    async def chat_with_files(
        request: Request,
        replace: str | None = Query(None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$"),
    ):
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            raise APIError("invalid_upload", "Send files as multipart form data.", 415)

        async def limited_body():
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BATCH_BYTES + 512 * 1024:
                    raise APIError("upload_too_large", "Attachment batch exceeds 40 MiB.", 413)
                yield chunk

        try:
            form = await MultiPartParser(
                request.headers,
                limited_body(),
                max_files=MAX_FILES,
                max_fields=3,
            ).parse()
        except MultiPartException as exc:
            raise APIError("invalid_upload", "Invalid multipart upload.", 422) from exc
        try:
            message = form.get("message", "")
            session_id = form.get("session_id") or None
            uploads = form.getlist("files")
            if (
                not isinstance(message, str)
                or len(message) > 100_000
                or session_id is not None
                and not isinstance(session_id, str)
                or not all(isinstance(item, UploadFile) for item in uploads)
            ):
                raise APIError("invalid_upload", "Invalid attachment or message.", 422)
            submission_id = form.get("submission_id")
            if submission_id is not None and (
                not isinstance(submission_id, str)
                or not re.fullmatch(r"[a-f0-9]{32}", submission_id)
            ):
                raise APIError("invalid_request", "Invalid submission identifier.", 422)
            body = ChatRequest(
                message=message.strip() or "Analyze the attached files.",
                session_id=session_id,
                submission_id=submission_id,
            )
            try:
                batch = await asyncio.to_thread(
                    AttachmentBatch.stage,
                    owner._service.paths.attachments_dir,
                    [(item.filename or "", item.file) for item in uploads],
                )
            except DocumentError as exc:
                raise APIError("invalid_upload", str(exc), 422) from exc
        finally:
            await form.close()
        try:
            return await stream_reply(request, body, batch, replace=replace)
        except Exception:
            # A failed journal append may already have discarded its bound batch.
            if batch.folder.exists():
                batch.discard()
            raise

    async def stream_reply(
        request: Request,
        body: ChatRequest,
        attachments: AttachmentBatch | None = None,
        *,
        replace: str | None = None,
    ):
        if body.submission_id is not None:
            if replace is not None or request.query_params.get("receipt") != "1":
                raise APIError(
                    "invalid_request", "A submission ID requires a regular delivery receipt.", 422
                )
            receipt = await asyncio.to_thread(
                owner.directory.submit_once,
                view_key(request),
                body.submission_id,
                body.message,
                body.session_id,
                attachments,
            )
            return JSONResponse(receipt, status_code=202)
        if replace is None:
            selected, turn = await asyncio.to_thread(
                owner.directory.submit,
                view_key(request),
                body.message,
                body.session_id,
                attachments,
            )
        else:
            selected = channel(request)
            edit_id = request.headers.get("x-syvrio-edit")
            if edit_id is not None and not re.fullmatch(r"[a-f0-9]{32}", edit_id):
                raise APIError("invalid_request", "Invalid edit receipt identifier.", 422)
            # A reload owns a fresh navigation view, but can claim its original
            # receipt with the saved edit token, session, target and payload.
            receipt_owner = f"edit:{edit_id}" if edit_id else view_key(request)
            turn = await asyncio.to_thread(
                selected.scheduling.replace,
                receipt_owner,
                replace,
                body.message,
                body.session_id,
                attachments,
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
        return owner.directory.queue_state(view_key(request))

    @app.delete("/api/chat/queue/{turn_id}", status_code=204, tags=["chat"])
    def remove_queued(request: Request, turn_id: int):
        channel(request).scheduling.remove_queued(turn_id)

    @app.get("/api/chat/turns/{turn_id}", tags=["chat"])
    async def replay(request: Request, turn_id: int, after: int = Query(0, ge=0)):
        selected = channel(request)
        turn = selected.scheduling.registry.get(turn_id, selected._conversation.current_id)
        if turn is None:
            raise APIError(
                "turn_not_found", "This turn is unavailable. Reload session history.", 404
            )
        return StreamingResponse(
            selected.stream(turn, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/chat/insert", status_code=204, tags=["web"])
    async def insert(request: Request, body: InsertRequest):
        await asyncio.to_thread(
            channel(request).scheduling.insert, body.turn_id, body.session_id, body.message
        )

    @app.post("/api/chat/cancel", response_model=CancelResponse, tags=["web"])
    async def cancel(request: Request, body: CancelRequest):
        selected = channel(request)
        cancelled = selected.cancel(body.turn_id)
        return CancelResponse(
            cancelled=cancelled,
            turn_id=body.turn_id,
            active_turn_id=selected.active_turn_id(),
        )

    return app
