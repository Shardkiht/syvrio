import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from syvrio.agent.session import SessionStore, new_message_id
from syvrio.time import (
    format_local_timestamp,
    parse_timestamp,
    serialize_timestamp,
    timestamp_calendar_date,
    utc_now,
)


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
    # 不同时区的同一时刻必须归一到同一 UTC 瞬间。
    assert parse_timestamp(stamp) == datetime(2026, 11, 11, 1, tzinfo=UTC)
    assert serialize_timestamp(parse_timestamp(stamp)) == "2026-11-11T01:00:00+00:00"


@pytest.mark.parametrize("stamp", ["2026-11-11", "2026-11-11T09:00:00", "invalid", ""])
def test_timestamp_rejects_missing_offset_and_invalid_text(stamp):
    # 缺失、非法和无 offset 的时间一律拒绝，不允许静默猜测时区。
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


def test_format_local_timestamp_empty_is_blank():
    assert format_local_timestamp(None) == ""
    assert format_local_timestamp("") == ""


def test_calendar_date_keeps_supplied_offset_date():
    # 日历日期按来源时间自带偏移取日期，不得换算成 UTC 日期。
    assert timestamp_calendar_date("2026-11-11T01:00:00+08:00").isoformat() == "2026-11-11"
    assert parse_timestamp("2026-11-11T01:00:00+08:00").astimezone(UTC).date().isoformat() == (
        "2026-11-10"
    )


def test_session_append_normalizes_record_time_to_utc(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    record = store.append(
        session,
        {
            "type": "message", "id": new_message_id(), "role": "user",
            "content": "跨时区", "at": "2026-11-11T09:00:00+08:00",
        },
    )
    assert record["at"] == "2026-11-11T01:00:00+00:00"
    loaded = store.load(session.id)
    assert loaded.messages[0]["at"] == "2026-11-11T01:00:00+00:00"


def test_session_list_orders_by_instant_across_timezones(tmp_path):
    # 字符串顺序与真实时刻相反时，会话排序必须按 UTC 瞬间而不是字典序。
    store = SessionStore(tmp_path)
    early = store.create()
    late = store.create()
    store.append(
        early,
        {
            "type": "message", "id": new_message_id(), "role": "user",
            "content": "早", "at": "2026-11-11T01:00:00+08:00",
        },
    )
    store.append(
        late,
        {
            "type": "message", "id": new_message_id(), "role": "user",
            "content": "晚", "at": "2026-11-11T23:00:00-05:00",
        },
    )
    listed = store.list()
    assert [row["id"] for row in listed] == [late.id, early.id]


def test_session_load_rejects_naive_timestamps(tmp_path):
    # 历史 naive 数据必须经迁移工具转换；加载路径不得猜测时区。
    store = SessionStore(tmp_path)
    session = store.create()
    with session.path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"type": "message", "id": "m1", "role": "user", "content": "旧", "at": "2026-11-11T09:00:00"}
            )
            + "\n"
        )
    with pytest.raises(ValueError, match="offset"):
        store.load(session.id)
