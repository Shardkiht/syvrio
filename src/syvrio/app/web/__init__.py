"""Syvrio Web adapter: FastAPI transport over the existing Application layer."""
from __future__ import annotations

from .server import DEFAULT_HOST, DEFAULT_PORT, create_app, run_server

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "create_app", "run_server"]
