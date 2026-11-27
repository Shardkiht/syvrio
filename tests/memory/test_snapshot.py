from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from syvrio.infra.session.store import SessionStore
from syvrio.infra.storage import audit
from syvrio.infra.storage.database import transaction
from syvrio.memory.models import MemoryDraft
from syvrio.memory.snapshot import MemorySnapshot, RecallRecord
from syvrio.memory.store import MemoryStore
from syvrio.memory.turn_undo import TurnJournal

pytestmark = pytest.mark.memory


def assert_full(store, snapshot):
    fresh = MemoryStore(store.paths).snapshot()
    assert snapshot.revision == fresh.revision
    assert tuple(snapshot.records) == tuple(fresh.records)
    assert list(snapshot.by_id.items()) == list(fresh.by_id.items())
    assert list(snapshot.by_id) == [e.id for e in store.entries()]


def test_snapshot_refresh_consumes_ids_without_reading_audit(tmp_path):
    store = MemoryStore(tmp_path)
    before = store.snapshot()
    added = store.append("test/a", "synthetic")
    with transaction(store.paths, write=False) as db:
        version = store.revision()
        seq = audit.latest_sequence(db)
        ids = audit.changed_memory_ids(
            db,
            from_revision=before.revision,
            to_revision=version,
            after_seq=before.audit_seq,
            through_seq=seq,
            previous_ids=before.by_id.keys(),
        )
        assert ids == {added.id}

        def deny_audit_read(action, table, *_):
            if action == sqlite3.SQLITE_READ and table == "audit":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(deny_audit_read)
        refreshed = before.refresh(db, version, seq, ids)
    assert_full(store, refreshed)


def test_sparse_edits_share_records_and_preserve_order(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    with transaction(store.paths):
        for i in range(540):
            store.append("test/a", str(i), entry_id=f"fact-{i}")
    before = store.snapshot()
    store.save_revision("fact-1", MemoryDraft(text="moved", topic="test/z"))
    store.set_status("fact-2", "deprecated")
    added = store.append("test/a", "new")
    converted = []
    convert = RecallRecord.from_row

    def count(row):
        converted.append(row[0])
        return convert(row)

    with monkeypatch.context() as m:
        m.setattr(RecallRecord, "from_row", staticmethod(count))
        after = store.snapshot()
    assert set(converted) == {"fact-1", added.id} and len(converted) == 2
    assert after.records[-1].text == "moved"
    assert after.records[-2].id == added.id
    assert after.records[2:12:2] == tuple(after.records)[2:12:2]
    assert after.records.pages[1] is before.records.pages[1]
    assert before.records[1].text == "1" and "fact-2" in before.by_id
    assert before.by_id["fact-300"] is after.by_id["fact-300"]
    assert_full(store, after)


@pytest.mark.parametrize("operation", ["add", "correct", "deprecate", "repeated"])
def test_raw_undo_refresh(tmp_path, monkeypatch, operation):
    store = MemoryStore(tmp_path)
    session = SessionStore(store.paths).create()
    original = store.append("test/a", "original")
    journal = TurnJournal(store.paths, session.id, "synthetic")
    store.snapshot()
    with journal.capture():
        if operation == "add":
            store.append("test/a", "temporary")
        elif operation == "deprecate":
            store.set_status(original.id, "deprecated")
        else:
            store.save_revision(original.id, MemoryDraft(text="first edit", topic="test/b"))
            if operation == "repeated":
                store.save_revision(original.id, MemoryDraft(text="second edit", topic="test/b"))
    edited = store.snapshot()
    assert_full(store, edited)
    assert journal.restore() > 0
    with monkeypatch.context() as m:
        m.setattr(MemorySnapshot, "read", lambda *args: pytest.fail("unexpected full rebuild"))
        restored = store.snapshot()
    assert [(r.id, r.text) for r in restored.records] == [(original.id, "original")]
    assert_full(store, restored)


def test_coalesced_undo_cannot_mask_missing_topic_audit(tmp_path):
    store = MemoryStore(tmp_path)
    session = SessionStore(store.paths).create()
    key = store.append("test/a", "original").id
    hidden = store.append("test/b", "other").id
    journal = TurnJournal(store.paths, session.id, "synthetic")
    with journal.capture():
        for text in ("one", "two"):
            store.save_revision(key, MemoryDraft(text=text, topic="test/a"))
    store.snapshot()
    with transaction(store.paths) as db:
        journal.restore()
        db.execute("UPDATE topics SET archived=1 WHERE name='test/b'")
    updated = store.snapshot()
    assert hidden not in updated.by_id
    assert_full(store, updated)


@pytest.mark.parametrize(
    "owner, method", [(audit, "changed_memory_ids"), (MemorySnapshot, "refresh")]
)
def test_concurrent_write_between_version_and_refresh(tmp_path, monkeypatch, owner, method):
    store = MemoryStore(tmp_path)
    first = store.append("test/a", "one")
    cold = store.snapshot()
    second = store.append("test/a", "two")
    entered, resume = Event(), Event()
    original = getattr(owner, method)

    def paused(*args, **kwargs):
        entered.set()
        assert resume.wait(10)
        return original(*args, **kwargs)

    with ThreadPoolExecutor(1) as pool, monkeypatch.context() as m:
        m.setattr(owner, method, paused)
        future = pool.submit(store.snapshot)
        try:
            assert entered.wait(10)
            store.save_revision(first.id, MemoryDraft(text="changed", topic="test/b"))
            store.set_status(second.id, "deprecated")
        finally:
            resume.set()
        fixed = future.result(timeout=10)
    assert fixed.revision == cold.revision + 1
    assert [r.text for r in fixed.records] == ["one", "two"]
    assert_full(store, store.snapshot())
    assert [r.text for r in fixed.by_id.values()] == ["one", "two"]


def test_private_rollback_and_older_pinned_reader(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("test/a", "one")
    original = store.snapshot()
    with pytest.raises(RuntimeError), transaction(store.paths):
        store.append("test/a", "rolled back")
        private = store.snapshot()
        assert private.revision is None and len(private.records) == 2
        raise RuntimeError
    assert store.snapshot() is original

    def advance():
        store.append("test/a", "committed")
        return store.snapshot()

    with transaction(store.paths, write=False), ThreadPoolExecutor(1) as pool:
        assert store.revision() == original.revision
        newest = pool.submit(advance).result(timeout=10)
        assert_full(store, store.snapshot())
        assert store.snapshot().records == original.records
    assert store.snapshot() is newest
    assert [r.text for r in newest.records] == ["one", "committed"]


def test_unpaired_compensation_cannot_cover_an_unlogged_write(tmp_path):
    from syvrio.infra.storage.audit import revert_memory_change

    store = MemoryStore(tmp_path)
    store.append("test/a", "one")
    other = store.append("test/b", "two")
    store.snapshot()
    with transaction(store.paths) as db:
        original = dict(db.execute("SELECT * FROM audit ORDER BY seq LIMIT 1").fetchone())
        revert_memory_change(db, original)
        db.execute("UPDATE memories SET text='unlogged' WHERE id=?", (other.id,))
    assert_full(store, store.snapshot())


def test_refresh_across_page_splits_and_empty_pages(tmp_path):
    store = MemoryStore(tmp_path)
    assert not store.snapshot().records
    for batch in range(5):
        with transaction(store.paths):
            for n in range(batch * 110, (batch + 1) * 110):
                store.append("test/a", "fact", entry_id=f"row-{n}")
        snap = store.snapshot()
        assert len(snap.records) == (batch + 1) * 110
    assert_full(store, snap)
    # 清掉整个首页，保留在旧快照中的位置索引，同时核对新区间的首尾索引。
    for start in (0, 128):
        with transaction(store.paths):
            for n in range(start, start + 128):
                store.set_status(f"row-{n}", "deprecated")
        trimmed = store.snapshot()
        assert_full(store, trimmed)
    assert trimmed.records[0].id == "row-256"
    assert snap.records[0].id == "row-0"
