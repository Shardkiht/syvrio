"""Resolve and serve the browser bundle and its client-side routes."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.responses import FileResponse, Response

from ...backend.api.http import error_response


class WebAssets:
    def __init__(self, directory: Path | None = None) -> None:
        configured = os.environ.get("SYVRIO_WEB_STATIC")
        self.root = (
            directory or (Path(configured) if configured else Path(__file__).parent / "static")
        ).resolve()

    @property
    def ready(self) -> bool:
        return (self.root / "index.html").is_file()

    def serve(self, full_path: str) -> Response:
        if full_path == "api" or full_path.startswith("api/"):
            return error_response("not_found", "Not found.", 404)
        if not self.ready:
            return error_response(
                "assets_missing", "Web assets are not built. Run scripts/build_web.py.", 503
            )
        candidate = (self.root / full_path).resolve()
        if self.root in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(self.root / "index.html")
