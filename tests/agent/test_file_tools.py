from __future__ import annotations

import pytest

from syvrio.agent.file_tools import document_paths, file_handlers
from syvrio.agent.tools import build_tools
from syvrio.infra.session import SessionStore
from syvrio.memory.models import Candidate

pytestmark = pytest.mark.agent


def test_paths_require_explicit_absolute_path_and_preserve_spaces(tmp_path):
    path = tmp_path / "synthetic conversation.JSONL"
    assert document_paths(f'分析 "{path}"，只读不写') == [path.resolve()]
    assert document_paths("分析 relative.txt 和 https://example.invalid/file.pdf") == []


def test_file_reads_are_complete_by_default_and_paged_on_request(service, tmp_path):
    path = tmp_path / "synthetic.txt"
    text = "合成个人资料。" * 3000
    path.write_text(text, encoding="utf-8")
    handlers = file_handlers(service, [path])
    with pytest.raises(ValueError, match="本条用户消息"):
        handlers["read_file"]({"path": str(tmp_path / "other.txt")})
    full = handlers["read_file"]({"path": str(path)})
    assert full["text"] == text and full["total_chars"] == len(text) and full["next_offset"] is None
    assert handlers["read_file"]({"path": str(path), "offset": 7})["text"] == text[7:]
    for invalid in (0, -1, True, 2.5):
        with pytest.raises(ValueError, match="length"):
            handlers["read_file"]({"path": str(path), "length": invalid})
    pieces = []
    offset = 0
    while offset is not None:
        result = handlers["read_file"]({"path": str(path), "offset": offset, "length": 617})
        assert result["text"] == text[offset : offset + 617]
        pieces.append(result["text"])
        offset = result["next_offset"]
    assert "".join(pieces) == text


def test_file_read_redacts_before_tool_output(service, tmp_path):
    path = tmp_path / "synthetic.txt"
    path.write_text("密码是 SYNTHETIC-ONLY-12345", encoding="utf-8")
    result = file_handlers(service, [path])["read_file"]({"path": str(path)})
    assert "SYNTHETIC-ONLY-12345" not in str(result)


def test_import_is_preview_by_default_and_reports_remaining_chunks(service, tmp_path, monkeypatch):
    path = tmp_path / "synthetic.txt"
    path.write_text("合成笔记" * 2000, encoding="utf-8")
    from syvrio.agent.evidence import RecallContext

    calls = []
    candidate = Candidate(text="合成用户偏好茶饮", topic="preferences")

    def prepare(text):
        calls.append(text)
        return service.evidence_version(include_pending=True), [candidate]

    monkeypatch.setattr(service, "prepare_import", prepare)
    sessions = SessionStore(service.paths)
    session = sessions.create()
    sessions.append(
        session, {"type": "message", "role": "user", "content": "请导入附件", "id": "m-file"}
    )
    evidence = RecallContext(service)
    handler = file_handlers(service, [path], evidence=evidence, session=session)["import_file"]
    args = {"path": str(path)}
    result = handler(args)
    assert result["next_chunk"] == 2 and result["drafts"][0]["text"] == candidate.text
    assert service.store.entries() == []
    evidence.begin_model_request()
    review = handler({**args, "apply": True})
    assert review["status"] == "review_required" and service.store.entries() == []
    evidence.begin_model_request()
    result = handler({**args, "apply": True})
    assert result["mode"] == "applied" and len(calls) == 1
    assert service.store.entries()[0].source_message_id == "m-file"


def test_tool_registration_is_scoped_to_current_turn(service, tmp_path):
    session = SessionStore(service.config.vault).create()
    assert "read_file" not in build_tools(service, session)._tools
    tools = build_tools(service, session, file_paths=[tmp_path / "synthetic.txt"])
    assert tools._tools["import_file"].mutating
    assert not tools.execute("read_file", {"path": str(tmp_path / "not-granted.txt")}).ok
