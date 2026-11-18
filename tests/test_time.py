from datetime import UTC, datetime, timedelta, timezone

import pytest

from syvrio.time import format_local_timestamp, parse_timestamp, serialize_timestamp, utc_now


def test_utc_now_uses_explicit_utc():
    assert utc_now().tzinfo is UTC


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-11-11T09:00:00+08:00",
        "2026-11-11T01:00:00Z",
        "2026-11-10T20:00:00-05:00",
        "2026-11-11T06:45:00+05:45",
    ],
)
def test_timestamp_normalizes_offsets(stamp):
    assert parse_timestamp(stamp) == datetime(2026, 11, 11, 1, tzinfo=UTC)
    assert serialize_timestamp(parse_timestamp(stamp)) == "2026-11-11T01:00:00+00:00"


@pytest.mark.parametrize("stamp", ["2026-11-11", "2026-11-11T09:00:00", "invalid", ""])
def test_timestamp_rejects_missing_offset_and_invalid_text(stamp):
    with pytest.raises(ValueError):
        parse_timestamp(stamp)


def test_serialize_rejects_naive_and_preserves_microseconds():
    with pytest.raises(ValueError, match="explicit UTC offset"):
        serialize_timestamp(datetime(2026, 11, 11))
    value = datetime(2026, 11, 11, 9, microsecond=1, tzinfo=timezone(timedelta(hours=8)))
    assert serialize_timestamp(value) == "2026-11-11T01:00:00.000001+00:00"


@pytest.mark.parametrize(
    ("value", "offset", "expected"),
    [
        ("2026-11-11T00:00:00Z", 0, "2026-11-11 00:00:00"),
        ("2026-11-11T12:00:00Z", 0, "2026-11-11 12:00:00"),
        ("2026-11-11T01:05:03Z", 8, "2026-11-11 09:05:03"),
        ("2026-11-11T22:01:00Z", 8, "2026-11-12 06:01:00"),
        ("2026-11-11T02:01:00Z", -5, "2026-11-10 21:01:00"),
    ],
)
def test_local_display_is_zero_padded_24_hour(value, offset, expected):
    zone = timezone(timedelta(hours=offset))
    assert format_local_timestamp(value, "%Y-%m-%d %H:%M:%S", zone=zone) == expected
