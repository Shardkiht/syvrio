"""Shared trusted-host rules and JSON errors for both HTTP listeners."""

import os

from fastapi.responses import JSONResponse

from .models import ApiErrorResponse, ErrorDetail

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
