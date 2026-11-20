from __future__ import annotations

import json

import pytest

from syvrio.infra.config import Config, LLMConfig, RetrievalConfig
from syvrio.memory.extractor import StaticExtractor
from syvrio.memory.models import (
    Candidate,
    ExtractionResult,
    MemoryContext,
    MemoryDraft,
    PendingItem,
)
from syvrio.memory.service import MemoryService
from syvrio.session import SessionStore

pytestmark = pytest.mark.memory


def make_service(tmp_path, candidates):
    config = Config(
        vault=tmp_path / "vault", llm=LLMConfig(), retrieval=RetrievalConfig(method="keyword")
    )
    extractor = StaticExtractor(ExtractionResult(memories=candidates))
    service = MemoryService(config, extractor=extractor)
    service.init_vault()
    return service


def draft(text, topic="misc", **context):
    return MemoryDraft(
        text=text, topic=topic, context=MemoryContext(**context) if context else None
    )


def message_source(service, content):
    sessions = SessionStore(service.paths)
    session = sessions.create()
    sessions.append(
        session, {"type": "message", "role": "user", "id": "m-test", "content": content}
    )
    return {"session_id": session.id, "message_id": "m-test"}


@pytest.mark.parametrize("question", [None, "是在哪个展馆？"])
def test_context_and_trusted_source_survive_import_and_approval(tmp_path, question):
    context = MemoryContext(event_time="2024-06", emotion={"event": "紧张", "recollection": "开心"})
    candidate = Candidate(text="参观展览", topic="events", context=context, question=question)
    svc = make_service(tmp_path, [candidate])
    original = "2024年6月参加展览时紧张，现在想起来开心。"
    source = message_source(svc, original)
    version, candidates = svc.prepare_import(original, extractor=svc.extractor)
    result = svc.apply_import(candidates, version, **source)
    if question:
        (queued,) = result.queued
        assert svc.queue.decode(svc.queue.pending_path.read_bytes()) == [queued]
        assert queued.context == context and not result.admitted
        entry = svc.approve(queued.id)
    else:
        (entry,) = result.admitted
        assert result.to_dict()["admitted"][0]["context"]["emotion"] == context.emotion.model_dump(
            exclude_none=True
        )
    assert svc.store.find(entry.id).context == context
    assert (
        entry.source_session_id == source["session_id"]
        and entry.source_message_id == source["message_id"]
    )
    assert svc.read_memory_source(entry.id)["message"]["content"] == original
    assert svc.memory_referencing_session(source["session_id"]) == entry.id


def test_import_dry_run_and_duplicate_retry(tmp_path):
    svc = make_service(
        tmp_path,
        [
            Candidate(
                text="有过紧张的感受",
                topic="events",
                context=MemoryContext(emotion={"event": "紧张"}),
            ),
            Candidate(text="也许在附近", topic="location", question="具体在哪里？"),
        ],
    )
    result = svc.import_text("外部材料", dry_run=True, extractor=svc.extractor)
    assert result.to_dict()["admitted"][0]["context"] == {"emotion": {"event": "紧张"}}
    assert [p.text for p in result.queued] == ["也许在附近"]
    assert not svc.pending() and not svc.current_facts() and not svc.store.changes_path.exists()
    saved = svc.import_text("外部材料", extractor=svc.extractor)
    again = svc.import_text("外部材料", extractor=svc.extractor)
    assert again.duplicates == ["有过紧张的感受"] and not again.admitted
    assert again.queued == saved.queued and len(svc.pending()) == 1
    assert len(svc.store.changes_path.read_bytes().splitlines()) == 1


def test_import_rejects_stale_evidence_across_call_and_retries_fresh(tmp_path):
    svc = make_service(tmp_path, [Candidate(text="导入的事实", topic="misc")])
    stale, candidates = svc.prepare_import("材料", extractor=svc.extractor)
    svc.note("读取后发生了另一次写入")
    with pytest.raises(ValueError, match="fresh evidence"):
        svc.apply_import(candidates, stale)
    assert [r.text for r in svc.current_facts()] == ["读取后发生了另一次写入"]
    assert svc.import_text("材料", extractor=svc.extractor).admitted[0].text == "导入的事实"


def test_source_identity_is_idempotent_without_sharing_unrelated_messages(tmp_path):
    svc = make_service(tmp_path, [])
    text = "合成候选"
    one, two = message_source(svc, text), message_source(svc, text)
    record = draft(text)
    pending = svc.create_pending(record, "需要确认吗？", [], **one)
    assert svc.create_pending(record, "需要确认吗？", [], **one).id == pending.id
    other = svc.create_pending(record, "需要确认吗？", [], **two)
    assert other.id != pending.id and svc.queue.deletion_preview(two["session_id"])[0] == [other]
    with pytest.raises(ValueError):
        svc.create_pending(
            record, "需要确认吗？", [], session_id=one["session_id"], message_id="missing"
        )
    entry = svc.add(record, entry_id="mem-fixed", **one)
    assert svc.add(record, entry_id="mem-fixed", **one) == entry
    with pytest.raises(ValueError):
        svc.add(record, entry_id="mem-fixed", **two)
    assert svc.store.entries() == [entry]
    generated = svc.add(draft("另一条事实", "general/misc"), **one)
    reopened = MemoryService(svc.config)
    assert reopened.add(draft("另一条事实", "misc"), **one).id == generated.id
    independent = reopened.add(draft("另一条事实", "misc"), **two)
    assert independent.id != generated.id
    assert len(reopened.store.changes_path.read_bytes().splitlines()) == 3


def test_queue_question_and_payload_cannot_store_credentials(tmp_path):
    svc = make_service(tmp_path, [])
    secret = "api_key=synthetic-token-123"
    with pytest.raises(ValueError):
        svc.create_pending(draft("普通事实"), secret, [])
    with pytest.raises(ValueError):
        svc.create_pending(draft("普通事实", location=secret), "何时？", [])
    assert not svc.pending() and not svc.store.entries()


def test_init_creates_vault_git_and_readme(tmp_path):
    service = make_service(tmp_path, [])
    vault = tmp_path / "vault"
    assert (vault / "README.md").exists()
    assert (vault / ".git").is_dir()
    assert b"/attachments/" in (vault / ".gitignore").read_bytes().splitlines()
    assert service.history(limit=5)[0]["subject"] == "memory: 初始化记忆库"


def test_reopening_vault_preserves_user_ignore_rules_and_excludes_uploads(tmp_path):
    service = make_service(tmp_path, [])
    vault = service.config.vault
    path = vault / ".gitignore"
    prior = b"# private rules\r\n*.local\r\n!/attachments/"
    path.write_bytes(prior)
    upload = vault / "attachments" / "sample.txt"
    upload.parent.mkdir()
    upload.write_bytes(b"private document")
    (vault / "sample.txt").write_bytes(b"ordinary vault file")
    MemoryService(service.config, extractor=service.extractor)
    expected = prior + b"\r\n/attachments/\r\n"
    assert path.read_bytes() == expected
    service.repo.stage_all()
    staged = service.repo.staged_paths()
    assert "sample.txt" in staged and "attachments/sample.txt" not in staged
    MemoryService(service.config, extractor=service.extractor)
    assert path.read_bytes() == expected


@pytest.mark.parametrize("override", [None, "我喜欢深烘咖啡"])
def test_approval_retries_and_never_changes_related_fact(tmp_path, override):
    svc = make_service(tmp_path, [])
    old = svc.note("我喜欢喝茶", "preferences")
    item = svc.create_pending(
        draft("我喜欢咖啡", topic="preferences", event_time="2024"), "这是新增偏好吗？", [old.id]
    )
    order = svc.approve(item.id, text_override=override)
    assert order.text == (override or item.text) and order.id != old.id
    assert order.context == (None if override else item.context)
    assert svc.store.find(old.id).status == "active" and svc.store.find(old.id).text == old.text
    assert not svc.pending() and not svc.queue.resolved_path.exists()
    before = svc.store.changes_path.read_bytes(), svc.audit.path.read_bytes()
    assert svc.approve(item.id, text_override=override) == order
    with pytest.raises(ValueError):
        svc.approve(item.id, text_override="不同的重试正文")
    assert (svc.store.changes_path.read_bytes(), svc.audit.path.read_bytes()) == before
    assert [json.loads(line)["action"] for line in before[1].splitlines()] == ["add", "add"]


def test_review_keeps_deprecated_related_fact_and_archive_contains_original_only(tmp_path):
    svc = make_service(tmp_path, [])
    old = svc.note("先前事实")
    item = svc.create_pending(draft("待确认", location="示例展厅"), "这里指哪里？", [old.id])
    svc.deprecate(old.id)
    before = svc.audit.path.read_bytes(), svc.store.changes_path.read_bytes()
    assert svc.reject(item.id) == item
    assert svc.queue.decode(svc.queue.resolved_path.read_bytes()) == [item]
    assert not svc.pending() and svc.store.find(old.id).status == "deprecated"
    assert (svc.audit.path.read_bytes(), svc.store.changes_path.read_bytes()) == before


@pytest.mark.parametrize("value", ["", "   ", "API key: demo-placeholder"])
def test_invalid_approval_keeps_original_and_all_files(tmp_path, value):
    svc = make_service(tmp_path, [])
    item = svc.create_pending(draft("候选"), "是这个意思吗？", [])
    before = svc.queue.pending_path.read_bytes()
    with pytest.raises(ValueError):
        svc.approve(item.id, text_override=value)
    assert svc.pending() == [item] and svc.queue.pending_path.read_bytes() == before
    assert not svc.current_facts() and not svc.store.changes_path.exists()


def test_commit_generates_fallback_summary_and_history(tmp_path):
    service = make_service(tmp_path, [])
    service.note("使用 手冲滤纸", topic="devices")

    result = service.commit(push=False)

    assert result["committed"]
    assert result["summary"].startswith("memory:")
    assert result["hash"]
    assert service.history(1)[0]["hash"] == result["hash"]

    again = service.commit(push=False)
    assert not again["committed"]


def test_push_without_remote_reports_error_but_keeps_commit(tmp_path):
    # 回归：push 失败不再抛异常掩盖已完成的本地提交。
    service = make_service(tmp_path, [])
    service.note("一条记录", topic="misc")
    service.commit(push=False)
    service.note("另一条记录", topic="misc")

    result = service.commit(push=True)

    assert result["committed"] and result["hash"]
    assert result["pushed"] is False
    assert "remote" in result["push_error"]


def test_recall_and_read_topic_return_structured_active_facts(tmp_path):
    service = make_service(tmp_path, [])
    wanted = service.note("喜欢清淡风味", topic="preferences")
    old = service.note("以前的偏好", topic="preferences")
    service.deprecate(old.id)
    (hit,) = service.recall("清淡")
    assert hit.id == wanted.id and hit.topic == "general/preferences"
    result = service.read_topic("general/preferences")
    assert result == {
        "topic": "general/preferences",
        "entries": [hit.model_dump(exclude_none=True)],
    }
    assert set(result["entries"][0]) == {"id", "status", "text", "topic"}
    topics = service.topics()
    assert len(topics) == 1 and topics[0]["entries"] == 1
    with pytest.raises((ValueError, KeyError)):
        service.read_topic("../preferences")
    with pytest.raises(KeyError):
        service.read_topic("no-such-topic")


def test_commit_push_failure_does_not_hide_commit(tmp_path):
    # auto_push 但无 remote：本地提交必须成功返回 hash，push 失败只记录在 push_error。
    config = Config(vault=tmp_path / "vault", llm=LLMConfig(), auto_push=True)
    service = MemoryService(config, extractor=StaticExtractor(ExtractionResult(memories=[])))
    service.init_vault()
    service.note("一条记录", topic="misc")

    result = service.commit()

    assert result["committed"] and result["hash"]
    assert result["pushed"] is False
    assert "remote" in result["push_error"]


def test_load_persona_falls_back_to_gbk(tmp_path):
    # 记事本 ANSI（GBK）保存的 SOUL.md 不应炸掉整轮对话。
    from syvrio.memory.service import load_persona

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "SOUL.md").write_bytes("你是阿澈，语气简洁。".encode("gbk"))

    assert "阿澈" in load_persona(vault)


def test_load_persona_missing_or_empty(tmp_path):
    from syvrio.memory.service import load_persona

    vault = tmp_path / "vault"
    vault.mkdir()
    assert load_persona(vault) == ""
    (vault / "SOUL.md").write_text("   \n", encoding="utf-8")
    assert load_persona(vault) == ""


def test_commit_pending_changes_auto_commits_without_llm(tmp_path):
    # 启动检查：有未提交变更时自动提交（不 push、不调 LLM），无变更时返回 None。
    service = make_service(tmp_path, [])
    assert service.commit_pending_changes() is None  # 干净库

    service.note("启动前未提交的一条", topic="misc")
    commit = service.commit_pending_changes()
    assert commit
    assert not service.repo.has_changes()
    assert any("自动提交" in item["subject"] for item in service.history(3))


@pytest.mark.parametrize(
    "text",
    [
        "api_key demo-123456",
        "password=!Demo1234",
        "验证码 458712请勿转发",
        "密码是 合成测试口令",
        "我的 API 密钥是 synthetic-token-1234",
    ],
)
def test_note_correct_and_import_reject_secrets(tmp_path, text):
    svc = make_service(tmp_path, [Candidate(text=text, topic="misc")])
    with pytest.raises(ValueError):
        svc.note(text)
    entry = svc.note("普通偏好", "preferences")
    with pytest.raises(ValueError):
        svc.correct(entry.id, draft(text))
    result = svc.import_text("合成材料", extractor=svc.extractor)
    assert result.blocked and text not in repr(result.blocked)
    assert not result.admitted and not result.queued and svc.current_facts() == [entry]


def test_normal_credential_description_can_be_imported(tmp_path):
    text = "这个软件的密码管理器好用"
    svc = make_service(tmp_path, [Candidate(text=text, topic="devices")])
    assert svc.import_text(text, extractor=svc.extractor).admitted[0].text == text


def test_approve_rejects_secret_from_manually_edited_candidate(tmp_path):
    svc = make_service(tmp_path, [])
    item = PendingItem(text="api_key=abcd1234", topic="misc", question="确认吗？")
    svc.queue.add(item)
    with pytest.raises(ValueError):
        svc.approve(item.id)
    assert svc.pending() == [item] and not svc.current_facts()


@pytest.mark.parametrize(
    ("operation", "stage"),
    [
        ("approve", "audit"),
        ("approve", "resolve"),
        ("approve", "context_job"),
        ("correct", "status"),
        ("correct", "audit"),
        ("transition", "status"),
        ("reject", "pending"),
        ("deprecate", "audit"),
    ],
)
def test_failed_multi_file_operations_restore_every_file_and_can_retry(
    tmp_path, monkeypatch, operation, stage
):
    from syvrio.infra import transaction
    from syvrio.memory import service as service_module

    svc = make_service(tmp_path, [])
    old = svc.note("最初事实", "old")
    pending = svc.create_pending(draft("待确认的事实", "new"), "具体是什么？", [old.id])

    def snapshot():
        roots = (
            svc.paths.memories_dir,
            svc.paths.vault_dir / "archived",
            svc.paths.vault_dir / "records",
            svc.paths.jobs_dir,
        )
        return {p: p.read_bytes() for root in roots for p in root.rglob("*") if p.is_file()}

    def execute():
        if operation == "approve":
            return svc.approve(pending.id, text_override="实际确认的事实")
        if operation == "correct":
            return svc.correct(old.id, draft("更正文", "new"))
        if operation == "transition":
            return svc.transition(old.id, draft("后续事实", "new"), previous_valid_to="2025")
        if operation == "reject":
            return svc.reject(pending.id)
        return svc.deprecate(old.id)

    def fail(*args, **kwargs):
        raise OSError("injected disk error")

    before = snapshot()
    with monkeypatch.context() as patch:
        if stage == "pending":
            writer = transaction.atomic_write

            def fail_pending(path, payload):
                return fail() if path == svc.queue.pending_path else writer(path, payload)

            patch.setattr(transaction, "atomic_write", fail_pending)
        else:
            obj, name = {
                "status": (svc.store, "update"),
                "audit": (svc.audit, "write"),
                "resolve": (svc.queue, "resolve"),
                "context_job": (service_module, "enqueue_context_fill"),
            }[stage]
            patch.setattr(obj, name, fail)
        with pytest.raises(OSError, match="injected"):
            execute()
    assert snapshot() == before
    assert svc.store.find(old.id).status == "active" and svc.pending() == [pending]
    execute()
    assert snapshot() != before
    if operation in {"approve", "reject"}:
        assert not svc.pending() and svc.store.find(old.id).status == "active"
    elif operation == "transition":
        assert (
            svc.store.find(old.id).status == "active"
            and svc.store.find(old.id).context.valid_to == "2025"
        )
    else:
        assert svc.store.find(old.id).status == "deprecated" and svc.pending() == [pending]
