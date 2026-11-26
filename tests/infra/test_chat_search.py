from __future__ import annotations

import re
import sqlite3
from contextlib import closing

from syvrio.infra.session.query import SessionQuery
from syvrio.infra.storage.database import database_path, transaction


def _seed(db, key, stamp, title, messages):
    db.execute("INSERT INTO sessions(id,started_at,title) VALUES(?,?,?)", (key, stamp, title))
    db.executemany(
        """INSERT INTO session_events(session_id,kind,message_id,role,at,content)
           VALUES(?,'message',?,'user',?,?)""",
        [(key, f"m{i}", stamp, body) for i, body in enumerate(messages)],
    )


def test_search_ranking_and_unicode_match_original_regex(service):
    query = SessionQuery(service.paths)
    groups = [
        ("b", "2", "I need data", ["工作。🦊！", "K ıſ İß ﬅΣ", "数中文 検索 한국", "A\0B"]),
        ("a", "2", "K", ["old same date", "工作。!"]),
        ("c", "1", "first", ["工作。 earliest", "ςſ"]),
    ]
    with transaction(service.paths) as db:
        for key, stamp, title, messages in groups:
            _seed(db, key, stamp, title, messages)
    for keyword in (
        "工",
        "工作。",
        "中",
        "检",
        "검",
        "索",
        "한",
        "s",
        "I",
        "k",
        "iS",
        "iß",
        "ﬆσ",
        "ς",
        "🦊",
        "！",
        "\0",
        "a\0b",
        "missing",
    ):
        pat = re.compile(re.escape(keyword), re.IGNORECASE)
        expected = []
        for key, _, title, messages in groups:
            if pat.search(title):
                expected.append((key, ""))
            expected.extend(
                (key, f"m{i}") for i in reversed(range(len(messages))) if pat.search(messages[i])
            )
        for limit in (1, 3, 20):
            found = query.search_chats(keyword, limit)["results"]
            assert [(r["session_id"], r["message_id"]) for r in found] == expected[:limit]


def test_raw_sql_edits_and_uncommitted_messages_are_immediately_searchable(service):
    query = SessionQuery(service.paths)
    with transaction(service.paths) as db:
        _seed(db, "s1", "1", "", ["Old data"])
        _seed(db, "s2", "2", "", ["New data"])
        assert [r["session_id"] for r in query.search_chats("data")["results"]] == ["s2", "s1"]
    with closing(sqlite3.connect(database_path(service.paths))) as db, db:
        db.execute("UPDATE session_events SET content='新数据' WHERE session_id='s1'")
        db.execute("UPDATE sessions SET started_at='3' WHERE id='s1'")
    assert not query.search_chats("Old")["results"]
    assert query.search_chats("据")["results"][0]["session_id"] == "s1"
    with transaction(service.paths) as db:
        db.execute("UPDATE session_events SET content='新数据 B' WHERE session_id='s2'")
    assert [r["session_id"] for r in query.search_chats("据")["results"]] == ["s1", "s2"]
    with transaction(service.paths) as db:
        db.execute("UPDATE sessions SET archived=1 WHERE id='s1'")
        db.execute("UPDATE session_events SET active=0 WHERE session_id='s2'")
    assert not query.search_chats("据")["results"]
    assert not query.search_chats("数据", exclude_session="s1")["results"]


def test_search_validates_only_signature_candidates_and_stops_after_limit(service):
    query = SessionQuery(service.paths)
    with transaction(service.paths) as db:
        _seed(db, "data", "1", "log", ["daily tasks"] * 500 + ["一份猫粮"] * 50)
        for i in range(100):
            _seed(db, f"empty{i}", "2", "log", [])
    with transaction(service.paths, write=False) as db:
        calls = []
        sql = []
        db.set_trace_callback(sql.append)
        db.set_progress_handler(lambda: calls.append(0) or 0, 100)
        hits = query.search_chats("猫", 1)["results"]
        db.set_trace_callback(None)
        db.set_progress_handler(None, 0)
    assert hits[0]["message_id"] == "m549"
    assert sum("FROM search_signatures g" in statement for statement in sql) == 1
    assert len(calls) < 75


def test_long_message_signatures_include_matches_at_the_end(service):
    query = SessionQuery(service.paths)
    with transaction(service.paths) as db:
        _seed(db, "long", "1", "test", ["a" * 40000 + "RareZ", "复" * 40000 + "İ猫🦊"])
    assert query.search_chats("rarez", 1)["results"][0]["message_id"] == "m0"
    assert query.search_chats("i猫🦊", 1)["results"][0]["message_id"] == "m1"
    assert not query.search_chats("猫i", 1)["results"]


def test_only_session_writes_maintain_their_own_search_signatures(service):
    from syvrio.infra.search.usage import UsageLimiter
    from syvrio.infra.session import SessionStore

    with transaction(service.paths) as db:
        _seed(db, "external", "1", "", ["external message"])
    UsageLimiter({}, service.paths).record("synthetic")
    store = SessionStore(service.paths)
    session = store.create()
    store.append(session, {"type": "message", "role": "user", "content": "ordinary message"})
    with transaction(service.paths, write=False) as db:
        assert dict(db.execute("SELECT session_id,ready FROM search_signatures")) == {
            "external": 0,
            session.id: 1,
        }
    assert store.search_chats("external")["results"][0]["session_id"] == "external"
