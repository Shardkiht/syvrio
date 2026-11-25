from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from io import BytesIO

from syvrio.infra.session.attachment_objects import cleanup, original_path, release, reserve
from syvrio.infra.session.attachments import AttachmentBatch
from syvrio.infra.session.store import SessionStore
from syvrio.infra.storage.database import transaction


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
        token = batch.folder.name
        reserve(service.paths, token, batch.files)
        with transaction(service.paths):
            batch.publish(service.paths)
            store.append(
                session,
                dict(
                    type="message",
                    role="user",
                    content="synthetic",
                    attachments=[asdict(batch.files[0])],
                ),
            )
            release(service.paths, token)
        batch.finish()
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
