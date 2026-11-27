from __future__ import annotations

from io import BytesIO

import pytest

from syvrio.app.tui.screens.attachments import AttachmentScreen
from syvrio.backend.api.models import HealthResponse
from syvrio.infra.session.attachment_policy import ATTACHMENT_POLICY
from syvrio.infra.session.attachments import AttachmentBatch
from syvrio.infra.storage.documents import DocumentError

pytestmark = pytest.mark.ui


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (["wrong.bin"], "format"),
        (["empty.txt"], "Empty"),
        ([f"f{i}.txt" for i in range(9)], "at most 8"),
    ],
)
def test_picker_and_upload_reject_same_selection(tmp_path, names, message):
    files = [tmp_path / name for name in names]
    for path in files:
        path.write_bytes(b"" if path.name == "empty.txt" else path.name.encode())
    policy = HealthResponse(version="test", provider="test", model="test").attachment_policy
    screen = AttachmentScreen(policy, ())
    with pytest.raises(DocumentError, match=message):
        screen._select_files(files)
    with pytest.raises(DocumentError, match=message):
        AttachmentBatch.stage(
            tmp_path / "uploads", [(p.name, BytesIO(p.read_bytes())) for p in files]
        )


def test_picker_uses_advertised_limits_after_content_deduplication(tmp_path):
    policy = ATTACHMENT_POLICY.model_copy(
        update={
            "extensions": (".custom",),
            "max_file_bytes": 6,
            "max_files": 2,
            "max_batch_bytes": 7,
        }
    )
    first, copy, second, large = [tmp_path / (name + ".custom") for name in ("a", "b", "c", "d")]
    first.write_bytes(b"abc")
    copy.write_bytes(b"abc")
    second.write_bytes(b"defg")
    large.write_bytes(b"1234567")
    screen = AttachmentScreen(policy, (first,))
    assert screen._select_files([copy, second]) == (first, second)
    with pytest.raises(DocumentError, match="each"):
        screen._select_files([large])
    large.write_bytes(b"12345")
    with pytest.raises(DocumentError, match="per message"):
        screen._select_files([large])
