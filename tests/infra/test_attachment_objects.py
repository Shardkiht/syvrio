from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from io import BytesIO
from uuid import uuid4

from syvrio.infra.session.attachment_objects import original_path, upgrade_originals
from syvrio.infra.session.attachments import AttachmentBatch


def test_concurrent_bindings_share_bytes_and_release_only_the_last_reference(tmp_path):
    root = tmp_path / "attachments"

    def attach(index):
        first = (f"file-{index}.txt", BytesIO(b"common bytes"))
        alias = ("renamed.md", BytesIO(b"common bytes"))
        batch = AttachmentBatch.stage(root, [first, alias])
        assert len(batch.files) == 1 and batch.files[0].name == first[0]
        batch.bind(f"s-{index % 2}", f"m-{index}")
        return batch

    with ThreadPoolExecutor(max_workers=6) as pool:
        batches = list(pool.map(attach, range(6)))
        original = original_path(root, batches[0].files[0].sha256)
        assert len({next(iter(batch.documents().values())) for batch in batches}) == 1
        assert list(root.rglob("original")) == [original]
        list(pool.map(lambda batch: batch.discard(), batches[:-1]))
    assert original.read_bytes() == b"common bytes"
    batches[-1].discard()
    assert not original.exists()


def test_historical_originals_coalesce_and_keep_their_message_ids(tmp_path):
    root = tmp_path / "attachments"
    batch = AttachmentBatch.stage(root, [("sample.txt", BytesIO(b"old upload"))])
    item = batch.files[0]
    batch.bind("new", "m-new")
    old = asdict(item) | {"id": uuid4().hex, "name": "original.md"}
    folder = root / "older" / "m-old"
    folder.mkdir(parents=True)
    legacy = folder / (old["id"] + ".md")
    legacy.write_bytes(b"old upload")
    for _ in range(2):
        upgrade_originals(root)
        saved = AttachmentBatch.restore(root, "older", "m-old", [old])
        assert saved.files[0].id == old["id"]
        assert saved.documents()["1. original.md"] == batch.documents()["1. sample.txt"]
    assert not legacy.exists()
    batch.discard()
    assert saved.documents()["1. original.md"].read_bytes() == b"old upload"
    saved.discard()
    assert not list(root.rglob("original"))


def test_same_name_and_length_with_different_bytes_are_distinct(tmp_path):
    batch = AttachmentBatch.stage(
        tmp_path / "attachments", [("notes.txt", BytesIO(b"one")), ("notes.txt", BytesIO(b"two"))]
    )
    assert len({item.sha256 for item in batch.files}) == 2
    batch.discard()
