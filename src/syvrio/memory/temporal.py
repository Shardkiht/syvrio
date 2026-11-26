from __future__ import annotations

import calendar
from datetime import date, timedelta

from ..infra.time import timestamp_calendar_date
from .models import MemoryContext


def date_bounds(value: str) -> tuple[date, date]:
    """按原始精度解释日期范围，不把未知月日伪造成事实。"""
    MemoryContext(event_time=value)
    parts = [int(part) for part in value.split("-")]
    year = parts[0]
    month = parts[1] if len(parts) > 1 else 1
    start = date(year, month, parts[2] if len(parts) > 2 else 1)
    end_month = parts[1] if len(parts) > 1 else 12
    end = date(
        year, end_month, parts[2] if len(parts) > 2 else calendar.monthrange(year, end_month)[1]
    )
    return start, end


def resolve_relative(value: str, anchor: date | str | None) -> str | None:
    """根据可信本地日历日期或明确带偏移的原始消息时间，解析相对日、月、年。"""
    if not anchor:
        return None
    day = anchor if type(anchor) is date else timestamp_calendar_date(anchor)
    text = value.strip()
    if text in {"今天", "昨天", "前天", "明天", "后天", "现在"}:
        offset = {"前天": -2, "昨天": -1, "今天": 0, "现在": 0, "明天": 1, "后天": 2}[text]
        try:
            return (day + timedelta(days=offset)).isoformat()
        except OverflowError:
            return None
    if text in {"去年", "今年", "明年"}:
        year = day.year + {"去年": -1, "今年": 0, "明年": 1}[text]
        return f"{year:04d}" if 1 <= year <= 9999 else None
    if text in {"上个月", "这个月", "本月", "下个月"}:
        offset = {"上个月": -1, "这个月": 0, "本月": 0, "下个月": 1}[text]
        year, month = divmod(day.year * 12 + day.month - 1 + offset, 12)
        return f"{year:04d}-{month + 1:02d}" if 1 <= year <= 9999 else None
    return None


def matches_context(
    context: MemoryContext | None,
    start: str | None,
    end: str | None,
    location: str | None,
    emotion: str | None,
) -> bool:
    if context is None:
        return False
    if location and (
        not context.location or location.strip().casefold() not in context.location.casefold()
    ):
        return False
    if emotion:
        term = emotion.strip().casefold()
        if not any(term in text.casefold() for text in context.emotion_texts):
            return False
    if start or end:
        first = date_bounds(start)[0] if start else date.min
        last = date_bounds(end)[1] if end else date.max
        periods = []
        if context.event_time:
            periods.append(date_bounds(context.event_time))
        if context.valid_from or context.valid_to:
            periods.append(
                (
                    date_bounds(context.valid_from)[0] if context.valid_from else date.min,
                    date_bounds(context.valid_to)[1] if context.valid_to else date.max,
                )
            )
        return any(low <= last and high >= first for low, high in periods)
    return True
