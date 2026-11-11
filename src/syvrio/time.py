"""UTC storage and parsing for absolute timestamps."""

from datetime import UTC, date, datetime, tzinfo


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Timestamp requires an explicit UTC offset")
    try:
        return value.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("Timestamp is outside the UTC datetime range") from exc


def _parse_aware(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Timestamp must be an ISO 8601 string")
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() is None:
        raise ValueError("Timestamp requires an explicit UTC offset")
    return moment


def parse_timestamp(value: str) -> datetime:
    return _as_utc(_parse_aware(value))


def timestamp_calendar_date(value: str) -> date:
    """Calendar date in the offset explicitly supplied with a source message."""
    return _parse_aware(value).date()


def local_timestamp(value: str) -> str:
    """An explicit local offset for prompts and relative calendar calculations."""
    return parse_timestamp(value).astimezone().isoformat()


def serialize_timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat()


def utc_timestamp() -> str:
    return serialize_timestamp(utc_now().replace(microsecond=0))


def format_local_timestamp(
    value: str | None, pattern: str = "%Y-%m-%d %H:%M", *, zone: tzinfo | None = None
) -> str:
    return parse_timestamp(value).astimezone(zone).strftime(pattern) if value else ""
