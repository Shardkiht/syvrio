"""升级交接期间拦截新的写请求。"""

from __future__ import annotations

from fastapi import FastAPI, Request

from ..updates.activity import ResidentActivity
from .http import STATE_MODIFYING, error_response


def guard_update_writes(app: FastAPI, activity: ResidentActivity) -> None:
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in STATE_MODIFYING or request.url.path in {
            "/api/resident/update_drain",
            "/api/resident/update_resume",
            "/api/resident/shutdown",
        }:
            return await call_next(request)
        with activity.work(background=False) as admitted:
            if not admitted:
                return error_response(
                    "update_busy", "The service is restarting for an update.", 409
                )
            return await call_next(request)
