"""将应用层对话流编码为服务端推送事件，供常驻服务客户端消费。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ...application.turns import ConversationTurn
from .models import sse_frame


async def stream_frames(turn: ConversationTurn, after: int = 0) -> AsyncIterator[str]:
    async for event in turn.events(after):
        yield ": keep-alive\n\n" if event is None else sse_frame(event)
