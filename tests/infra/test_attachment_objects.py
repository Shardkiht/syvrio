import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from io import BytesIO
from pathlib import Path

import pytest

from syvrio.infra.session.attachment_objects import cleanup, original_path
from syvrio.infra.session.attachments import AttachmentBatch
from syvrio.infra.session.store import SessionStore
from syvrio.infra.storage.database import transaction
from syvrio.infra.storage.documents import DocumentError


def test_concurrent_bindings_share_bytes_and_release_only_last_reference(service, monkeypatch):
    root = service.paths.attachments_dir
    store = SessionStore(service.paths)

    def attach(i):
        session = store.create()
        batch = AttachmentBatch.stage(
            root,
            [(f"file-{i}.txt", BytesIO(b"common bytes")), ("renamed.md", BytesIO(b"common bytes"))],
        )
        assert len(batch.files) == 1
        with batch.sending(service.paths):
            store.append(
                session,
                dict(
                    type="message",
                    role="user",
                    content="synthetic",
                    attachments=[asdict(batch.files[0])],
                ),
            )
        return session, batch

    with ThreadPoolExecutor(max_workers=6) as pool:
        bindings = list(pool.map(attach, range(6)))
    first = bindings[0][1]
    original = original_path(root, first.files[0].sha256)
    assert len({next(iter(b.documents().values())) for _, b in bindings}) == 1
    for s, _ in bindings[:-1]:
        store.set_archived(s.id, True)
        store.delete(s.id, archived=True)
        assert original.read_bytes() == b"common bytes"
    last = bindings[-1][0]
    store.set_archived(last.id, True)
    real_unlink = type(original).unlink

    def fail(path, *a, **kw):
        if path == original:
            raise OSError("simulated cleanup failure")
        return real_unlink(path, *a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(type(original), "unlink", fail)
        store.delete(last.id, archived=True)
    assert original.exists() and not store.exists(last.id, archived=True)
    cleanup(service.paths)
    assert not original.exists()


def test_same_name_and_length_with_different_bytes_are_distinct(tmp_path):
    batch = AttachmentBatch.stage(
        tmp_path / "attachments", [("notes.txt", BytesIO(b"one")), ("notes.txt", BytesIO(b"two"))]
    )
    assert len({item.sha256 for item in batch.files}) == 2
    batch.discard()


def test_failed_send_does_not_remove_another_senders_original(service):
    store = SessionStore(service.paths)
    session = store.create()

    def stage(name):
        return AttachmentBatch.stage(store.attachments_dir, [(name, BytesIO(b"shared data"))])

    previous = store.create()
    old = stage("old.md")
    with old.sending(service.paths):
        store.append(
            previous,
            {
                "type": "message",
                "role": "user",
                "content": "old",
                "attachments": [asdict(old.files[0])],
            },
        )
    store.set_archived(previous.id, True)
    first, second = stage("first.txt"), stage("second.md")
    (item,) = second.files
    path = original_path(store.attachments_dir, item.sha256)
    with second.sending(service.paths):
        with pytest.raises(RuntimeError), first.sending(service.paths):
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(store.delete, previous.id, archived=True).result(timeout=10)
            assert path.read_bytes() == b"shared data"
            raise RuntimeError("interrupted before message commit")
        cleanup(service.paths)
        assert path.read_bytes() == b"shared data"
        store.append(
            session,
            {"type": "message", "role": "user", "content": "test", "attachments": [asdict(item)]},
        )
    with transaction(service.paths, write=False) as db:
        assert list(map(tuple, db.execute("SELECT name,size,sha256 FROM attachments"))) == [
            (item.name, item.size, item.sha256)
        ]
        assert not db.execute("SELECT 1 FROM file_cleanup").fetchone()
    assert path.is_file()


def test_resend_after_last_reference_deleted_is_rejected(service, monkeypatch):
    store = SessionStore(service.paths)
    session = store.create()
    batch = AttachmentBatch.stage(store.attachments_dir, [("notes.txt", BytesIO(b"original"))])
    with batch.sending(service.paths):
        store.append(
            session,
            {
                "type": "message",
                "role": "user",
                "content": "old",
                "attachments": [asdict(batch.files[0])],
            },
        )

    def unexpected_read(*args, **kwargs):
        pytest.fail("an immutable original should not be read again just to bind a message")

    with monkeypatch.context() as patch:
        patch.setattr("hashlib.file_digest", unexpected_read)
        with batch.sending(service.paths):
            store.append(
                session,
                {
                    "type": "message",
                    "role": "user",
                    "content": "again",
                    "attachments": [asdict(batch.files[0])],
                },
            )
    store.set_archived(session.id, True)
    assert store.delete(session.id, archived=True)
    with pytest.raises(DocumentError, match="missing or changed"), batch.sending(service.paths):
        pytest.fail("missing original was accepted for a second send")


def _crash_after_publish(vault, committed):
    import os

    store = SessionStore(Path(vault))
    batch = AttachmentBatch.stage(store.attachments_dir, [("sample.txt", BytesIO(b"pending data"))])
    with batch.sending(store.paths):
        if committed:
            store.append(
                store.create(),
                {
                    "type": "message",
                    "role": "user",
                    "content": "done",
                    "attachments": [asdict(batch.files[0])],
                },
            )
        os._exit(23)


@pytest.mark.parametrize("committed", [False, True])
def test_restart_only_collects_uncommitted_originals(tmp_path, committed):
    store = SessionStore(tmp_path)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests.infra.test_attachment_objects import _crash_after_publish; import sys; _crash_after_publish(sys.argv[1], bool(int(sys.argv[2])))",
            str(tmp_path),
            str(int(committed)),
        ],
        timeout=45,
    )
    assert child.returncode == 23
    with transaction(store.paths, write=False) as db:
        (digest,) = db.execute(
            "SELECT object_id FROM file_cleanup WHERE kind='attachment'"
        ).fetchone()
        assert bool(db.execute("SELECT 1 FROM attachments").fetchone()) == committed
    path = original_path(store.attachments_dir, digest)
    assert path.read_bytes() == b"pending data"
    store.recover_deletions()
    assert path.exists() == committed
    assert not list((store.attachments_dir / ".incoming").iterdir())
    assert store.verify_attachments() == (int(committed), [])
    with transaction(store.paths, write=False) as db:
        assert not db.execute("SELECT 1 FROM file_cleanup").fetchone()
