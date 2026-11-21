"""Encode the application conversation stream as server-sent events for resident clients."""

from __future__ import annotations

from collections.abc import AsyncIterator

from ...application.turns import ConversationTurn
from .models import sse_frame


async def stream_frames(turn: ConversationTurn, after: int = 0) -> AsyncIterator[str]:
    async for event in turn.events(after):
        yield ": keep-alive\n\n" if event is None else sse_frame(event)
