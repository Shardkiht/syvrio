from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from syvrio.infra.session import SessionStore
from syvrio.infra.storage import database
from syvrio.infra.storage.paths import SyvrioPaths
from syvrio.memory.background import distillation

pytestmark = pytest.mark.agent


def test_pages_preserve_tool_groups_and_use_saved_preview(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    text = "示例\x00图" * 10000
    store.append_event_transactionally(
        session,
        {"type": "message", "role": "user", "id": "u", "content": "start"},
        {"type": "assistant", "id": "a", "tool_calls": [{"id": "call", "name": "read"}]},
        {"type": "tool", "id": "tool", "tool_call_id": "call", "content": text},
        {"type": "assistant", "id": "end", "content": "done"},
    )
    args = dict(archived=False, tool_preview=200)
    page = store.history_window(session.id, 2, before_seq=None, **args)
    assert [r["id"] for r in page.records] == ["a", "tool", "end"]
    assert page.records[1]["content"] == text[:200]
    assert page.records[1]["tool_content_length"] == len(text)
    longer = store.history_window(session.id, 2, before_seq=None, archived=False, tool_preview=600)
    assert longer.records[1]["content"] == text[:600]
    previous = store.history_window(session.id, 2, before_seq=page.earlier_cursor, **args)
    assert [r["id"] for r in previous.records] == ["u"] and previous.earlier_cursor is None
    with database.transaction(store.paths, write=False) as db:
        assert tuple(
            db.execute(
                "SELECT tool_preview,tool_length FROM session_events WHERE kind='tool'"
            ).fetchone()
        ) == (
            text[:300],
            len(text),
        )
    assert store.tool_content(session.id, "tool", archived=False) == text
    store.append(session, {"type": "undo", "message_id": "u"})
    assert store.tool_content(session.id, "tool", archived=False) is None
    assert not store.history_window(session.id, 2, before_seq=None, **args).records
    store.set_archived(session.id, True)
    with pytest.raises(KeyError):
        store.history_window(session.id, 2, before_seq=None, **args)
    with pytest.raises(ValueError):
        store.history_window(session.id, 1, archived=True, tool_preview=3, before_seq=2**63)


def test_external_tool_writes_refresh_preview_and_preserve_nul(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    with database.transaction(store.paths) as db:
        db.execute(
            """INSERT INTO session_events(session_id,kind,message_id,at,content)
               VALUES(?,'tool','tool','2026-01-01T00:00:00+00:00',?)""",
            (session.id, "外部写入" * 150),
        )
        assert tuple(
            db.execute(
                "SELECT tool_preview,tool_length FROM session_events WHERE message_id='tool'"
            ).fetchone()
        ) == ("外部写入" * 75, 600)
    for text in ("更新后的文本" * 400, "第一段\x00第二段🙂" * 400):
        with database.transaction(store.paths) as db:
            db.execute("UPDATE session_events SET content=? WHERE message_id='tool'", (text,))
            if "\x00" not in text:
                cached = db.execute(
                    "SELECT tool_preview,tool_length FROM session_events WHERE message_id='tool'"
                ).fetchone()
                assert tuple(cached) == (text[:300], len(text))
        page = store.history_window(
            session.id, 1, archived=False, before_seq=None, tool_preview=200
        )
        (result,) = page.records
        assert result["content"] == text[:200]
        assert result["tool_content_length"] == len(text)
        assert store.tool_content(session.id, "tool", archived=False) == text


def test_reasoning_free_reads_do_not_request_private_column(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    thought = {"text": "private reasoning"}
    store.append_event_transactionally(
        session,
        {"type": "message", "role": "user", "id": "u", "content": "question"},
        {"type": "assistant", "id": "a", "content": "public answer", "reasoning": thought},
    )

    def deny_private_column(action, table, column, database_name, trigger_name):
        if action == sqlite3.SQLITE_READ and (table, column) == ("session_events", "reasoning"):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with database.transaction(store.paths, write=False) as db:
        db.set_authorizer(deny_private_column)
        try:
            records = store.load(session.id, include_reasoning=False).records
            _, history, _ = store.context_history(
                session.id, include_reasoning=False, active_message_id="u"
            )
        finally:
            db.set_authorizer(None)
    for view in (records, history):
        assert view[-1]["content"] == "public answer" and "reasoning" not in view[-1]
    assert store.load(session.id).records[-1]["reasoning"] == thought


def test_distilled_progress_does_not_replay_history(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    session = store.create()
    for id_ in ("a", "b", "c"):
        store.append(session, {"type": "message", "role": "user", "id": id_, "content": id_})
    store.append(session, {"type": "summary", "distilled_ids": ["b"], "content": "summary"})
    old = store.list()[0]["tokens"]
    monkeypatch.setattr(store, "load", lambda *a, **kw: pytest.fail("replayed session history"))
    distillation.request(store, session.id, None)
    assert store.first_pending(session.id)["id"] == "a"
    store.advance_distilled(session, "a")
    assert store.first_pending(session.id)["id"] == "c"
    store.advance_distilled(session, "c")
    assert store.pending_sessions() == []
    assert store.first_pending(session.id) is None and store.list()[0]["tokens"] == old


def test_concurrent_first_open_initializes_the_current_schema(tmp_path):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    gate = Barrier(4)

    def create_session(_):
        gate.wait(10)
        store = SessionStore(paths)
        session = store.create()
        store.append(session, {"type": "message", "role": "user", "id": "u", "content": "hello"})
        return session.id

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(create_session, range(4)))
    assert len(set(ids)) == 4
    store = SessionStore(paths)
    assert all(store.first_pending(id_)["id"] == "u" for id_ in ids)
    with database.transaction(paths, write=False) as db:
        assert db.execute("SELECT count(*) FROM search_signatures WHERE ready=1").fetchone()[0] == 4
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_resident_connection_keeps_wal_between_full_durability_appends(tmp_path):
    from contextlib import closing

    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    store = SessionStore(paths)
    with closing(database.keep_wal_open(paths)):
        session = store.create()
        for key in ("u1", "u2"):
            store.append(session, {"type": "message", "role": "user", "id": key, "content": key})
            assert Path(str(database.database_path(paths)) + "-wal").stat().st_size > 0
        with database.transaction(paths, write=False) as db:
            assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert store.load(session.id).user_texts() == ["u1", "u2"]
