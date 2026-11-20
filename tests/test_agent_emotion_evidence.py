import pytest

from syvrio.agent.evidence import RecallContext
from syvrio.agent.tools import build_tools
from syvrio.session import SessionStore

pytestmark = pytest.mark.agent


def start_chat(service, text):
    sessions = SessionStore(service.paths)
    session = sessions.create()
    sessions.append(session, {"type": "message", "role": "user", "id": "m-first", "content": text})
    return sessions, session, RecallContext(service)


def reviewed_write(tools, evidence, name, payload):
    evidence.begin_model_request()
    first = tools.execute(name, payload)
    assert first.ok and first.data["status"] == "review_required"
    evidence.begin_model_request()
    second = tools.execute(name, payload)
    assert second.ok, second.content
    return second.data


def read_page(sessions, session, tools, filename):
    result = tools.execute("read_file", {"path": filename})
    assert result.ok, result.content
    # The resident appends the delivered result before sending it to the model.
    sessions.append(
        session,
        {
            "type": "tool",
            "id": "file-result",
            "name": "read_file",
            "tool_call_id": "read",
            "ok": result.ok,
            "content": result.content,
        },
    )
    return result.data


def test_chat_keeps_quoted_emotions_and_ignores_generated_draft_and_assistant(service):
    sessions, session, evidence = start_chat(service, "当时有点紧张，现在想起来很开心。")
    sessions.append(session, {"type": "assistant", "content": "或许还无比激动。"})
    tools = build_tools(service, session, evidence=evidence)
    data = reviewed_write(
        tools,
        evidence,
        "memorize",
        {
            "text": "展览时无比激动。",
            "topic": "events",
            "context": {
                "location": "示例展厅",
                "emotion": {
                    "event": "有点紧张",
                    "recollection": "很开心",
                    "unspecified": "无比激动",
                },
            },
        },
    )
    context = data["memory"]["context"]
    assert context == {
        "location": "示例展厅",
        "emotion": {"event": "有点紧张", "recollection": "很开心"},
    }
    assert service.store.find(data["memory"]["id"]).context.model_dump(exclude_none=True) == context


def test_pending_accepts_only_emotions_from_actually_read_attachment_pages(
    service, tmp_path, monkeypatch
):
    sessions, session, evidence = start_chat(service, "请看附件。")
    attached = tmp_path / "fixture.txt"
    attached.write_text(
        "当时满怀期待。\n" + "合成材料。" * 1000 + "\n现在深感遗憾。", encoding="utf-8"
    )
    tools = build_tools(service, session, evidence=evidence, file_paths={"attachment-A": attached})
    page = read_page(sessions, session, tools, "attachment-A")
    assert page["next_offset"] and "满怀期待" in page["text"] and "深感遗憾" not in page["text"]
    monkeypatch.setattr(
        "syvrio.agent.file_tools.read_document", lambda *_: pytest.fail("reopened attachment")
    )
    data = reviewed_write(
        tools,
        evidence,
        "create_pending",
        {
            "text": "看展时满怀期待，回想时深感遗憾。",
            "topic": "events",
            "question": "是哪次展览？",
            "context": {"emotion": {"event": "满怀期待", "recollection": "深感遗憾"}},
        },
    )
    assert data["pending"]["context"]["emotion"] == {"event": "满怀期待"}
    assert service.pending()[0].context.emotion.recollection is None


def test_safely_inserted_message_starts_new_emotion_sources(service, tmp_path):
    sessions, session, evidence = start_chat(service, "之前我有点担心。")
    attached = tmp_path / "before-insertion.txt"
    attached.write_text("我经历这件事时欣喜。", encoding="utf-8")
    previous_tools = build_tools(service, session, evidence=evidence, file_paths={"old": attached})
    read_page(sessions, session, previous_tools, "old")
    sessions.append(
        session,
        {
            "type": "message",
            "role": "user",
            "id": "m-insert",
            "turn_root": "m-first",
            "content": "现在只觉得好奇。",
        },
    )
    evidence.invalidate()
    tools = build_tools(service, session, evidence=evidence)
    data = reviewed_write(
        tools,
        evidence,
        "memorize",
        {
            "text": "谈到一件事",
            "topic": "events",
            "context": {
                "emotion": {"event": "欣喜", "unspecified": "有点担心", "recollection": "好奇"}
            },
        },
    )
    assert data["memory"]["context"]["emotion"] == {"recollection": "好奇"}
    assert service.store.find(data["memory"]["id"]).source_message_id == "m-insert"
