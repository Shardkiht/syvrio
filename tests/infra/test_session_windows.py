from __future__ import annotations

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
    previous = store.history_window(session.id, 2, before_seq=page.earlier_cursor, **args)
    assert [r["id"] for r in previous.records] == ["u"] and previous.earlier_cursor is None
    with database.transaction(store.paths, write=False) as db:
        assert tuple(db.execute("SELECT preview,length FROM tool_previews").fetchone()) == (
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
