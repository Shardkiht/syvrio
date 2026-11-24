"""两个 HTTP 监听入口共用的主机信任规则和 JSON 错误响应。"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .models import ApiErrorResponse, ErrorDetail

STATE_MODIFYING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _hostname(value: str) -> str:
    """提取 Host 或 Origin 中不含端口的主机名，以便跨代理比较。"""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else value
    if ":" in value:
        return value.rsplit(":", 1)[0]
    return value


def _allowed_hosts(host: str, extra: list[str] | None = None) -> frozenset[str]:
    values = {"localhost", "127.0.0.1", "::1", host.strip().lower()}
    values.update(item.strip().lower() for item in (extra or []) if item.strip())
    return frozenset(values)


def error_response(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=ApiErrorResponse(error=ErrorDetail(code=code, message=message)).model_dump(),
    )


def guard_hosts(app: FastAPI, host: str, extra: list[str] | None = None) -> None:
    """两个监听入口的所有路由使用同一套 Host 和 Origin 策略。"""
    trusted = _allowed_hosts(host, extra)

    @app.middleware("http")
    async def check_request(request: Request, call_next):
        if _hostname(request.headers.get("host", "")) not in trusted:
            return error_response("untrusted_host", "This Host is not allowed.", 403)
        origin = request.headers.get("origin")
        if (
            request.method in STATE_MODIFYING
            and origin
            and _hostname(urlsplit(origin).netloc) not in trusted
        ):
            return error_response("untrusted_origin", "Cross-site write rejected.", 403)
        return await call_next(request)
