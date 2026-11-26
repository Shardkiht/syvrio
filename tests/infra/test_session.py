from __future__ import annotations

from datetime import UTC, datetime

import pytest

from syvrio.infra.session import SessionStore, new_message_id
from syvrio.infra.storage.database import transaction

pytestmark = pytest.mark.agent


def make_store(tmp_path):
    return SessionStore(tmp_path), SessionStore(tmp_path).create()


def test_create_append_and_reload(tmp_path):
    store, session = make_store(tmp_path)
    store.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "你好"},
    )
    store.append(
        session,
        {"type": "assistant", "id": new_message_id(), "content": "你好，有什么可以帮你？"},
    )

    loaded = store.load(session.id)
    assert loaded.id == session.id
    assert [message["role"] for message in loaded.messages] == ["user"]
    assert loaded.messages[0]["content"] == "你好"
    assert loaded.title() == "未命名会话"


def test_navigation_snapshots_are_detached_and_follow_archive_moves(tmp_path):
    store, session = make_store(tmp_path)
    store.append(session, {"type": "message", "role": "user", "id": "m1", "content": "Saved"})
    snapshot = store.load_snapshot(session.id)
    snapshot.messages[0]["content"] = "Unsaved change"
    assert store.load_snapshot(session.id).user_texts() == ["Saved"]

    store.set_archived(session.id, True)
    with pytest.raises(KeyError):
        store.load_snapshot(session.id)
    assert store.load(session.id, archived=True).user_texts() == ["Saved"]
    store.set_archived(session.id, False)
    assert store.load_snapshot(session.id).user_texts() == ["Saved"]


def test_title_job_survives_restart_and_uses_first_valid_exchange(service, monkeypatch):
    from threading import Event

    from syvrio.memory.background import BackgroundMemory, titles
    from syvrio.memory.background.jobs import job_key

    backend = BackgroundMemory(service)
    store = backend.sessions
    session, untouched = store.create(), store.create()
    for item in (
        {"type": "message", "role": "user", "id": "m1", "content": "interrupted question"},
        {"type": "assistant", "id": "a1", "content": "partial answer", "interrupted": True},
        {"type": "message", "role": "user", "id": "m2", "content": "actual question"},
        {
            "type": "assistant",
            "id": "a2",
            "content": "tool preamble",
            "tool_calls": [{"name": "recall"}],
        },
        {"type": "tool", "id": "t1", "content": "tool result"},
        {"type": "assistant", "content": "actual answer"},  # 旧记录可能没有 ID。
        {"type": "message", "role": "user", "id": "m3", "content": "later topic"},
        {"type": "assistant", "id": "a4", "content": "later answer"},
    ):
        store.append(session, item)
        store.append(untouched, item)
    # 列出会话或重启服务都不会调度历史对话。
    assert len(store.list()) == 2 and not backend.pending_titles([session.id, untouched.id])
    backend.request_title(session.id)
    restarted = BackgroundMemory(service)
    assert restarted.pending_titles([session.id, untouched.id]) == {session.id}
    seen = []

    def generate(service, question, answer):
        seen.append((question, answer))
        return "Generated title"

    monkeypatch.setattr(titles, "generate", generate)
    path = job_key(service.paths, "title", session.id)
    assert restarted._step_job("title", path, Event())
    assert seen == [("actual question", "actual answer")] and not restarted.pending_titles(
        [session.id, untouched.id]
    )
    saved = store.load(session.id)
    assert saved.title() == "Generated title"
    assert store.load(untouched.id).title() == "未命名会话"
    backend.request_title(session.id)
    assert not backend.pending_titles([session.id, untouched.id])
    store.append(saved, {"type": "undo", "message_id": "m2"})
    assert saved.title() == "未命名会话"


def test_generated_title_only_reads_title_evidence(service, monkeypatch):
    from threading import Event

    from syvrio.memory.background import BackgroundMemory, titles
    from syvrio.memory.background.jobs import job_key

    service.initialize_files()
    bg = BackgroundMemory(service)
    store = bg.sessions
    session = store.create()
    for event in (
        {"type": "message", "id": "m1", "role": "user", "content": "question"},
        {"type": "tool", "id": "t1", "content": "x" * 256_000},
        {"type": "assistant", "id": "a1", "content": "answer"},
    ):
        store.append(session, event)

    def unexpected_load(*args, **kwargs):
        pytest.fail("Title generation loaded full session history.")

    monkeypatch.setattr(store, "load", unexpected_load)
    monkeypatch.setattr(titles, "generate", lambda *_: "New title")
    bg.request_title(session.id)
    assert bg._step_job("title", job_key(service.paths, "title", session.id), Event())
    assert not store.untitled_exchange(session.id)


def test_missing_title_credentials_leave_history_readable(service, monkeypatch):
    from threading import Event

    from syvrio.memory.background import BackgroundMemory, titles
    from syvrio.memory.background.jobs import job_key, read_job

    backend = BackgroundMemory(service)
    session = backend.sessions.create()
    for item in (
        {"type": "message", "role": "user", "id": "m1", "content": "question"},
        {"type": "assistant", "id": "a1", "content": "answer"},
    ):
        backend.sessions.append(session, item)
    monkeypatch.setattr(titles, "ask", lambda *_: pytest.fail("Unexpected title model call"))
    backend.request_title(session.id)
    path = job_key(service.paths, "title", session.id)
    assert backend._step_job("title", path, Event()) and not read_job(path)
    loaded = backend.sessions.load(session.id)
    assert loaded.title() == "未命名会话" and loaded.user_texts() == ["question"]


def test_session_redacts_credentials_in_nested_records(tmp_path):
    store, session = make_store(tmp_path)
    secret = "SYNTHETIC-ONLY-12345"
    record = store.append(
        session,
        {
            "type": "assistant",
            "content": f"密码是 {secret}",
            "tool_calls": [{"arguments": {"text": f"API key: {secret}"}}],
        },
    )
    store.rename(session, f"密码是 {secret}")
    assert secret not in str(record)
    assert secret not in str(session.records)
    assert secret not in str(store.load(session.id).records)
    assert "敏感信息已隐藏" in record["content"]


def test_rename_and_list(tmp_path):
    store, session = make_store(tmp_path)
    store.append(
        session,
        {
            "type": "message",
            "id": new_message_id(),
            "role": "user",
            "content": "一段很长的对话内容",
        },
    )
    store.rename(session, "口味偏好")

    rows = store.list()
    assert rows[0]["id"] == session.id
    assert rows[0]["title"] == "口味偏好"
    assert rows[0]["messages"] == 1


def test_session_listing_orders_by_last_message_before_limiting(tmp_path):
    store = SessionStore(tmp_path)
    older = store.create()
    newer = store.create()
    for session, stamp in ((older, "2025-01-01"), (newer, "2025-01-02")):
        store.append(
            session,
            {
                "type": "message",
                "role": "user",
                "content": "Synthetic turn",
                "at": stamp + "T00:00:00+00:00",
            },
        )
    store.append(
        older,
        {
            "type": "summary",
            "content": "Summary",
            "distilled_ids": [],
            "at": "2025-01-03T00:00:00+00:00",
        },
    )
    store.append(older, {"type": "title", "title": "Renamed", "at": "2025-01-04T00:00:00+00:00"})
    assert [row["id"] for row in store.list(limit=1)] == [newer.id]
    older_row = store.list()[1]
    assert older_row["title"] == "Renamed"
    assert older_row["updated_at"] == "2025-01-04T00:00:00+00:00"
    assert older_row["last_message_at"] == "2025-01-01T00:00:00+00:00"
    store.append(
        older,
        {
            "type": "message",
            "role": "user",
            "content": "Synthetic follow-up",
            "at": "2025-01-05T00:00:00+00:00",
        },
    )
    assert [row["id"] for row in store.list()] == [older.id, newer.id]


def test_summary_covers_records(tmp_path):
    store, session = make_store(tmp_path)
    store.append(session, {"type": "message", "id": "m1", "role": "user", "content": "第一句"})
    store.append(session, {"type": "tool", "id": "t1", "name": "recall", "content": "结果"})
    store.append(session, {"type": "message", "id": "m2", "role": "user", "content": "第二句"})
    store.append(
        session,
        {
            "type": "summary",
            "id": "s1",
            "content": "摘要",
            "covered_records": 3,
            "distilled_ids": [],
        },
    )

    loaded = store.load(session.id)
    assert loaded.summary_text() == "摘要"
    assert loaded.covered_records() == 3
    assert [record["id"] for record in loaded.records_after_summary()] == ["m2"]
    assert loaded.user_texts_after_summary() == ["第二句"]


@pytest.mark.parametrize(
    ("older_at", "newer_at"),
    [
        ("2025-01-01T09:00:00+08:00", "2025-01-01T02:00:00+00:00"),
        ("2025-01-02T01:00:00+08:00", "2025-01-01T19:00:00Z"),
        ("2025-01-01T01:00:00-05:00", "2025-01-01T02:00:00-05:00"),
        ("", "2025-01-01T00:00:00Z"),
    ],
)
def test_session_listing_sorts_timestamps_before_limiting(tmp_path, older_at, newer_at):
    store = SessionStore(tmp_path)
    older, newer = store.create(), store.create()
    for session, stamp in ((older, older_at), (newer, newer_at)):
        if stamp:
            store.append(
                session, {"type": "message", "role": "user", "content": "Turn", "at": stamp}
            )
    if not older_at:
        with transaction(store.paths) as db:
            db.execute("UPDATE sessions SET last_message_at='' WHERE id=?", (older.id,))
    assert [row["id"] for row in store.list()] == [newer.id, older.id]
    assert store.list(limit=1)[0]["id"] == newer.id


@pytest.mark.parametrize("invalid", ["2025-07-01T12:00:00", "not-a-timestamp"])
def test_session_rejects_invalid_timestamp_on_append_and_load(tmp_path, invalid):
    store, session = make_store(tmp_path)
    assert session.started_at.endswith("+00:00")
    record = {"type": "message", "role": "user", "content": "Turn", "at": invalid}
    before = store.load(session.id).records
    with pytest.raises(ValueError):
        store.append(session, record)
    assert store.load(session.id).records == before


def test_session_listing_keeps_archive_order_for_equal_times(tmp_path):
    store = SessionStore(tmp_path)
    first, second = store.create(), store.create()
    for session, stamp in (
        (first, "2025-01-01T09:00:00+08:00"),
        (second, "2025-01-01T01:00:00Z"),
    ):
        store.append(session, {"type": "message", "role": "user", "content": "Turn", "at": stamp})
    archive_order = sorted([first.id, second.id])
    assert [row["id"] for row in store.list()] == archive_order
    assert store.list(limit=1)[0]["id"] == archive_order[0]


def test_session_listing_reorders_after_undo(tmp_path):
    store = SessionStore(tmp_path)
    edited, other = store.create(), store.create()
    for session, message_id, stamp in (
        (edited, "first", "2025-01-01T09:00:00+08:00"),
        (other, "other", "2025-01-01T02:00:00Z"),
        (edited, "latest", "2025-01-01T11:00:00+08:00"),
    ):
        store.append(
            session,
            {"type": "message", "id": message_id, "role": "user", "content": "Turn", "at": stamp},
        )
    assert store.list(limit=1)[0]["id"] == edited.id

    store.append(edited, {"type": "undo", "message_id": "latest"})
    rows = store.list()
    assert [row["id"] for row in rows] == [other.id, edited.id]
    assert rows[1]["last_message_at"] == "2025-01-01T01:00:00+00:00"

    store.append(edited, {"type": "undo", "message_id": "first"})
    row = next(row for row in store.list() if row["id"] == edited.id)
    assert row["last_message_at"] == edited.started_at


def test_undo_persists_and_allows_reedited_message(tmp_path):
    store, session = make_store(tmp_path)
    for message_id, text in (("m1", "第一问"), ("m2", "第二问")):
        store.append(
            session, {"type": "message", "id": message_id, "role": "user", "content": text}
        )
        store.append(
            session,
            {"type": "assistant", "id": f"a{message_id[1:]}", "content": f"{text}的回答"},
        )
    raw_records = list(session.records)

    store.append(session, {"type": "undo", "id": "u1", "message_id": "m2"})
    loaded = store.load(session.id)

    assert loaded.user_texts() == ["第一问"]
    assert loaded.last_user_message_id() == "m1"
    assert [record["id"] for record in loaded.records_after_summary()] == ["m1", "a1"]
    assert loaded.user_texts_after_distilled() == ["第一问"]
    assert loaded.records[: len(raw_records)] == raw_records

    store.append(loaded, {"type": "message", "id": "m3", "role": "user", "content": "编辑后"})
    store.append(loaded, {"type": "assistant", "id": "a3", "content": "新回答"})
    assert loaded.user_texts() == ["第一问", "编辑后"]

    store.append(loaded, {"type": "undo", "id": "u2", "message_id": "m3"})
    assert store.load(session.id).user_texts() == ["第一问"]


def test_invalid_session_id_is_rejected(tmp_path):
    store = SessionStore(tmp_path)
    try:
        store.load("../../etc/passwd")
    except KeyError as exc:
        assert "非法会话" in str(exc)
    else:
        raise AssertionError("应当拒绝非法会话 ID")


def test_delete_session_file(tmp_path):
    store, session = make_store(tmp_path)
    store.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "待删除"},
    )

    assert store.delete(session.id) is True
    assert not store.exists(session.id)
    assert store.list() == []
    assert store.delete(session.id) is False


def test_create_retries_id_collision_without_overwrite(tmp_path, monkeypatch):
    from syvrio.infra.session import store as session_module

    monkeypatch.setattr(session_module, "utc_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    tokens = iter(["aaaa", "aaaa", "ffff"])
    monkeypatch.setattr(session_module.secrets, "token_hex", lambda _: next(tokens))
    store = SessionStore(tmp_path)

    first = store.create()
    second = store.create()

    assert first.id == "s-20260101-000000-aaaa"
    assert second.id == "s-20260101-000000-ffff"
    assert store.load(first.id).records[0]["id"] == first.id


def test_pending_sessions_return_ids_in_most_recent_order(tmp_path):
    store = SessionStore(tmp_path)
    sessions = []
    for index, count in enumerate((5, 1, 2, 1)):
        session = store.create()
        sessions.append(session)
        for number in range(count):
            store.append(
                session,
                {
                    "type": "message",
                    "id": f"m{index}-{number}",
                    "role": "user",
                    "content": f"合成陈述 {index}-{number}",
                    "at": f"2026-01-0{index + 1}T12:00:00+00:00",
                },
            )

    assert store.pending_sessions() == [s.id for s in reversed(sessions)]


def test_distillation_tracks_ids_separately_from_new_summary_boundaries(tmp_path):
    store, session = make_store(tmp_path)
    store.append(session, {"type": "message", "id": "m1", "role": "user", "content": "旧版已沉淀"})
    store.append(
        session,
        {"type": "summary", "content": "此前摘要", "covered_records": 2, "distilled_ids": ["m1"]},
    )
    store.append(
        session, {"type": "message", "id": "m2", "role": "user", "content": "首次抽取失败"}
    )
    store.append(
        session,
        {
            "type": "summary",
            "content": "失败后仍压缩",
            "covered_records": 4,
            "distilled_ids": [],
        },
    )
    store.append(
        session, {"type": "message", "id": "m3", "role": "user", "content": "后续抽取成功"}
    )
    store.append(
        session,
        {
            "type": "summary",
            "content": "后续摘要",
            "covered_records": 6,
            "distilled_ids": ["m3"],
        },
    )
    store.append(session, {"type": "message", "id": "m4", "role": "user", "content": "新消息"})

    loaded = store.load(session.id)
    assert loaded.user_texts_after_summary() == ["新消息"]
    assert loaded.user_texts_after_distilled() == ["首次抽取失败", "新消息"]
    store.append(loaded, {"type": "distilled", "up_to": "m4"})
    assert store.load(session.id).user_texts_after_distilled() == []


@pytest.mark.parametrize("succeeded", [False, True])
def test_undo_preserves_distillation_receipts_but_excludes_undone_messages(tmp_path, succeeded):
    store, session = make_store(tmp_path)
    store.append(session, {"type": "message", "id": "m1", "role": "user", "content": "保留陈述"})
    store.append(session, {"type": "message", "id": "m2", "role": "user", "content": "撤销陈述"})
    store.append(
        session,
        {
            "type": "summary",
            "content": "撤销后失效的摘要",
            "covered_records": 2,
            "distilled_ids": ["m1"] if succeeded else [],
        },
    )
    store.append(session, {"type": "undo", "message_id": "m2"})

    loaded = store.load(session.id)
    assert loaded.summary_text() == ""
    assert loaded.user_texts() == ["保留陈述"]
    assert loaded.user_texts_after_distilled() == ([] if succeeded else ["保留陈述"])


def test_list_reuses_cached_summary_until_file_changes(tmp_path, monkeypatch):
    store, session = make_store(tmp_path)
    store.append(
        session,
        {
            "type": "message",
            "id": new_message_id(),
            "role": "user",
            "content": "第一条",
        },
    )
    monkeypatch.setattr(
        store, "load", lambda *a, **kw: pytest.fail("read history to render sidebar")
    )
    first = store.list()
    second = store.list()
    assert first == second

    store.append(
        session,
        {
            "type": "message",
            "id": new_message_id(),
            "role": "user",
            "content": "第二条",
        },
    )
    third = store.list()
    assert third[0]["messages"] == 2


def test_pending_sessions_skip_completed_sessions_without_loading_history(tmp_path, monkeypatch):
    store, session = make_store(tmp_path)
    message_id = new_message_id()
    store.append(
        session,
        {
            "type": "message",
            "id": message_id,
            "role": "user",
            "content": "已经沉淀",
        },
    )
    store.append(session, {"type": "distilled", "up_to": message_id})
    monkeypatch.setattr(
        store, "load", lambda *a, **kw: pytest.fail("loaded completed conversation")
    )
    assert store.pending_sessions() == []


def test_distilled_high_water_mark_does_not_move_backwards(tmp_path):
    store, session = make_store(tmp_path)
    for number in range(3):
        store.append(
            session,
            {
                "type": "message",
                "id": f"m{number}",
                "role": "user",
                "content": f"陈述{number}",
            },
        )
    store.append(session, {"type": "distilled", "up_to": "m1"})
    store.append(session, {"type": "distilled", "up_to": "m0"})
    assert store.load(session.id).user_texts_after_distilled() == ["陈述2"]


@pytest.mark.parametrize("identifier", [{}, {"id": "../invalid"}])
def test_invalid_reasoning_reference_keeps_assistant_body(tmp_path, identifier):
    store, session = make_store(tmp_path)
    store.append(
        session,
        {"type": "assistant", "content": "Synthetic reply", "reasoning_local": True, **identifier},
    )
    before = store.load(session.id).records
    assistant = store.load(session.id).records[-1]
    assert assistant["content"] == "Synthetic reply"
    assert "reasoning" not in assistant
    assert store.load(session.id).records == before


def test_missing_private_reasoning_preserves_both_messages(tmp_path):
    store, session = make_store(tmp_path)
    for identifier in ("first", "second"):
        store.append(
            session,
            {
                "type": "assistant",
                "id": identifier,
                "content": "visible",
                "reasoning": {"text": "local reasoning"},
            },
        )
    with transaction(store.paths) as db:
        db.execute(
            "DELETE FROM session_reasoning WHERE event_id=(SELECT seq FROM session_events WHERE message_id='first')"
        )
    first, second = store.load(session.id).records[-2:]
    assert first["content"] == second["content"] == "visible"
    assert "reasoning" not in first
    assert second["reasoning"]["text"] == "local reasoning"


@pytest.mark.parametrize("reader_kind", ["snapshot", "query"])
def test_readers_wait_only_for_the_session_published_by_transaction(tmp_path, reader_kind):
    from concurrent.futures import ThreadPoolExecutor

    from syvrio.infra.session.query import SessionQuery
    from syvrio.infra.storage.database import transaction

    store, edited = make_store(tmp_path)
    other = store.create()
    store.append(edited, {"type": "message", "role": "user", "id": "old", "content": "before"})
    query = SessionQuery(store.paths)
    read = store.load_snapshot if reader_kind == "snapshot" else query.load
    with ThreadPoolExecutor(2) as pool:
        with pytest.raises(RuntimeError, match="rollback"):
            with transaction(store.paths):
                store.append_event_transactionally(edited, {"type": "undo", "message_id": "old"})
                waiting = pool.submit(read, edited.id)
                assert pool.submit(read, other.id).result(3).id == other.id
                assert waiting.result(3).user_texts() == ["before"]
                raise RuntimeError("rollback")
        assert waiting.result(3).user_texts() == ["before"]
