"""Shared exception taxonomy for Syvrio.

Every error Syvrio raises deliberately on a user-reachable path inherits from
``SyvrioError`` (alongside its traditional base, so existing ``except
ValueError``/``except RuntimeError`` clauses keep working). Boundary layers
catch ``SyvrioError`` for translated, user-facing messages and fall back to
``except Exception`` only as defense in depth, recording the original error
before converting it into an exit or an error payload.
"""

from __future__ import annotations


class SyvrioError(Exception):
    """Base class for all deliberate, user-reachable Syvrio failures."""


class SessionProtectedError(SyvrioError):
    """The session is in use or holds original evidence for a saved memory."""


class SessionDeletionChanged(SyvrioError):
    """Related pending items changed since the user saw the confirmation."""
