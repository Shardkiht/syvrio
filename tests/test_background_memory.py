from __future__ import annotations

import json
from threading import Event, Thread
from unittest.mock import Mock

import pytest

from syvrio.agent.context import ContextManager
from syvrio.background_memory import BackgroundMemory, context_fill, distillation
from syvrio.infra.config import ChatConfig
from syvrio.infra.jobs import enqueue_context_fill, job_path, read_job
from syvrio.infra.transaction import vault_transaction
from syvrio.memory.inference import source_fingerprint
from syvrio.memory.models import MemoryContext, MemoryDraft, MemoryUpdate

pytestmark = pytest.mark.memory


def message(sessions, session, mid, text):
    sessions.append(session, {"type": "message", "role": "user", "id": mid, "content": text})


def test_compaction_enqueues_only_undistilled_originals(service, monkeypatch):
    owner = BackgroundMemory(service)
    session = owner.sessions.create()
    message(owner.sessions, session, "first", "已经处理的原始消息")
    owner.sessions.advance_distilled(session, "first")
    message(owner.sessions, session, "second", "尚未处理的原始消息")
    summarizer = Mock()
    summarizer.summarize.return_value = "仅用于对话的合成摘要"
    context = ContextManager(
        ChatConfig(context_window=1000, reserve_output_tokens=100),
        summarizer,
        owner.sessions,
        owner.request_distillation,
    )
    plan = context.prepare(session, "system", "", tool_tokens=context.trigger_tokens)
    assert plan.compacted and plan.summary == summarizer.summarize.return_value
    path = job_path(service.paths, "distillation", session.id)
    assert read_job(path)["through"] == "second"

    extract = Mock(return_value=distillation.Result(memories=[]))
    monkeypatch.setattr(distillation, "ask", extract)
    assert distillation.process(service, owner.sessions, path, lambda _: False, Event())
    extract.assert_called_once()
    assert extract.call_args.args[2]["source_text"] == "尚未处理的原始消息"
    assert not path.exists() and not owner.sessions.load(session.id).undistilled_messages()


def test_distillation_commits_each_original_source_and_empty_decision(service, monkeypatch):
    owner = BackgroundMemory(service)
    session = owner.sessions.create()
    for mid, text in [("first", "我种番茄"), ("empty", "谢谢"), ("third", "我种南瓜")]:
        message(owner.sessions, session, mid, text)
    service.add(
        MemoryDraft(topic="garden", text="我种番茄"), session_id=session.id, message_id="first"
    )
    seen = []

    def extract(_service, _prompt, payload, schema):
        seen.append((payload["source_text"], payload["already_saved"]))
        return schema(
            memories=[]
            if payload["source_text"] == "谢谢"
            else [MemoryDraft(topic="garden", text=payload["source_text"])]
        )

    monkeypatch.setattr(distillation, "ask", extract)
    owner.request_distillation(session.id, "empty")
    path = job_path(service.paths, "distillation", session.id)
    assert distillation.process(service, owner.sessions, path, lambda _: True, Event()) is False
    assert not seen
    for _ in range(2):
        distillation.process(service, owner.sessions, path, lambda _: False, Event())
    assert not path.exists()
    assert [r["id"] for r in owner.sessions.load(session.id).undistilled_messages()] == ["third"]
    BackgroundMemory(service)._discover(Event())
    owner.request_distillation(session.id, "first")
    assert read_job(path)["through"] == "third"
    for _ in range(2):
        distillation.process(service, owner.sessions, path, lambda _: False, Event())
    assert [(e.text, e.source_message_id) for e in service.current_facts()] == [
        ("我种番茄", "first"),
        ("我种南瓜", "third"),
    ]
    assert [p[0] for p in seen] == ["我种番茄", "谢谢", "我种南瓜"]
    assert seen[0][1][0]["text"] == "我种番茄"
    assert not owner.sessions.load(session.id).undistilled_messages()


def test_distillation_rolls_back_facts_if_cursor_write_fails(service, monkeypatch):
    owner = BackgroundMemory(service)
    session = owner.sessions.create()
    message(owner.sessions, session, "first", "我养猫和狗")
    owner.request_distillation(session.id)
    path = job_path(service.paths, "distillation", session.id)
    monkeypatch.setattr(
        distillation,
        "ask",
        lambda *_: distillation.Result(
            memories=[
                MemoryDraft(topic="pets", text="养猫"),
                MemoryDraft(topic="pets", text="养狗"),
            ]
        ),
    )
    advance = owner.sessions.advance_distilled

    def fail_after_append(*args):
        advance(*args)
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(owner.sessions, "advance_distilled", fail_after_append)
        with pytest.raises(OSError):
            distillation.process(service, owner.sessions, path, lambda _: False, Event())
    assert path.is_file()
    assert not service.current_facts()
    assert [r["id"] for r in owner.sessions.load(session.id).undistilled_messages()] == ["first"]
    distillation.process(service, owner.sessions, path, lambda _: False, Event())
    assert len(service.current_facts()) == 2
    assert not owner.sessions.load(session.id).undistilled_messages()


def test_distilling_revoked_user_message_cannot_publish(service, monkeypatch):
    owner = BackgroundMemory(service)
    session = owner.sessions.create()
    message(owner.sessions, session, "gone", "原话")
    owner.request_distillation(session.id)
    path = job_path(service.paths, "distillation", session.id)

    def deleted(*_):
        owner.sessions.delete(session.id)
        return distillation.Result(memories=[MemoryDraft(topic="t", text="不应写入")])

    monkeypatch.setattr(distillation, "ask", deleted)
    assert not distillation.process(service, owner.sessions, path, lambda _: False, Event())
    assert not service.current_facts()
    distillation.process(service, owner.sessions, path, lambda _: False, Event())
    assert not path.exists()


def test_context_fill_only_empty_fields_and_quote_grounding(service, monkeypatch):
    owner = BackgroundMemory(service)
    session = owner.sessions.create()
    message(owner.sessions, session, "source", "2024年在杭州参观。当时开心，现在回想很遗憾。")
    fact = service.add(
        MemoryDraft(topic="travel", text="参观展览", context=MemoryContext(location="杭州")),
        session_id=session.id,
        message_id="source",
    )
    enqueue_context_fill(service.paths, fact.id, source_fingerprint(fact))
    path = job_path(service.paths, "context", fact.id)
    calls = []

    def extract(_, prompt, payload, schema):
        calls.append(payload)
        return schema(
            location="苏州",
            event_time="2024",
            emotion={
                "event": "开心",
                "recollection": "很遗憾",
                "unspecified": "天马行空",
            },
        )

    monkeypatch.setattr(context_fill, "ask", extract)
    context_fill.fill(service, path, Event())
    context_fill.fill(service, path, Event())
    assert len(calls) == 1
    assert calls[0]["confirmed_fact"] == fact.text
    assert calls[0]["source"]["content"].startswith("2024年")
    assert service.store.find(fact.id).context.model_dump(exclude_none=True) == {
        "event_time": "2024",
        "location": "杭州",
        "emotion": {"event": "开心", "recollection": "很遗憾"},
    }


@pytest.mark.parametrize("changed", [True, False])
def test_context_no_overwrite_when_version_changes_or_provider_fails(service, monkeypatch, changed):
    fact = service.add(MemoryDraft(topic="t", text="我的正式记忆"))
    enqueue_context_fill(service.paths, fact.id, source_fingerprint(fact))
    path = job_path(service.paths, "context", fact.id)

    def extract(*_):
        if changed:
            service.apply(
                updates=[MemoryUpdate(id=fact.id, context=MemoryContext(location="新地点"))]
            )
            return MemoryContext(location="旧地点", event_time="2020")
        raise TimeoutError

    monkeypatch.setattr(context_fill, "ask", extract)
    context_fill.fill(service, path, Event())
    actual = service.store.find(fact.id)
    assert actual.text == "我的正式记忆"
    assert (actual.context.model_dump(exclude_none=True) if actual.context else {}) == (
        {"location": "新地点"} if changed else {}
    )
    assert not path.exists()


def test_transactional_event_keeps_bad_tail_and_discards_rolled_back_projection(service):
    owner = BackgroundMemory(service)
    session = owner.sessions.create()
    message(owner.sessions, session, "m", "hello")
    with session.path.open("ab") as fh:
        fh.write(b'{"broken"')
    before = session.path.read_bytes()
    with pytest.raises(RuntimeError), vault_transaction(service.paths):
        owner.sessions.advance_distilled(session, "m")
        raise RuntimeError
    assert session.path.read_bytes() == before
    assert not any(r["type"] == "distilled" for r in session.records)
    owner.sessions.advance_distilled(session, "m")
    added = session.path.read_bytes()[len(before) :].strip().splitlines()
    assert [json.loads(line)["type"] for line in added] == ["recovery", "distilled"]
    assert not owner.sessions.load(session.id).undistilled_messages()


def test_resident_startup_retries_durable_session_without_open_client(service, monkeypatch):
    owner = BackgroundMemory(service)
    session = owner.sessions.create()
    message(owner.sessions, session, "m", "一个合成事实")
    called = Event()
    release = Event()

    def extract(*_):
        called.set()
        assert release.wait(5)
        return distillation.Result(memories=[MemoryDraft(topic="t", text="一个合成事实")])

    monkeypatch.setattr(distillation, "ask", extract)
    owner.start()
    try:
        assert called.wait(5)
        # 响应缓慢的供应商不能长期占用写锁或阻塞其他会话。
        saved = Event()
        thread = Thread(
            target=lambda: (service.add(MemoryDraft(topic="t", text="另一事实")), saved.set())
        )
        thread.start()
        try:
            assert saved.wait(4)
        finally:
            release.set()
            thread.join(5)
        import time

        until = time.monotonic() + 6
        while owner.sessions.load(session.id).undistilled_messages() and time.monotonic() < until:
            owner.wake()
            time.sleep(0.025)
        assert not owner.sessions.load(session.id).undistilled_messages()
    finally:
        release.set()
        owner.stop()


@pytest.mark.parametrize("transactional", [False, True])
def test_original_calendar_day_drives_background_distillation_and_source_lookup(
    service, monkeypatch, transactional
):
    owner = BackgroundMemory(service)
    session = owner.sessions.create()
    row = {
        "type": "message",
        "role": "user",
        "id": "source",
        "at": "2024-03-01T00:10:00+08:00",
        "content": "昨天参加了展览，去年也来过。当时很惊喜。",
    }
    if transactional:
        owner.sessions.append_event_transactionally(session, row)
    else:
        owner.sessions.append(session, row)
    fresh = owner.sessions.load(session.id, include_reasoning=False).undistilled_messages()[0]
    assert fresh["at"].startswith("2024-02-29T16:10:00")
    assert fresh["original_local_date"] == "2024-03-01"

    def extract(_, prompt, payload, schema):
        assert payload["original_local_date"] == "2024-03-01"
        assert payload["relative_dates"] == {"昨天": "2024-02-29", "去年": "2023"}
        return schema(
            memories=[
                MemoryDraft(
                    text="参加展览",
                    topic="events",
                    context=MemoryContext(
                        event_time=payload["relative_dates"]["昨天"],
                        emotion={"event": "很惊喜", "recollection": "惴惴不安"},
                    ),
                )
            ]
        )

    monkeypatch.setattr(distillation, "ask", extract)
    owner.request_distillation(session.id)
    path = job_path(service.paths, "distillation", session.id)
    distillation.process(service, owner.sessions, path, lambda _: False, Event())
    entry = service.current_facts()[0]
    assert entry.context.event_time == "2024-02-29"
    assert entry.context.emotion.model_dump(exclude_none=True) == {"event": "很惊喜"}
    assert service.read_memory_source(entry.id)["message"]["original_local_date"] == "2024-03-01"


def test_server_clock_never_supplies_an_unknown_user_timezone(service, monkeypatch):
    owner = BackgroundMemory(service)
    session = owner.sessions.create()
    owner.sessions.append(
        session,
        {
            "type": "message",
            "role": "user",
            "id": "no-offset",
            "content": "我昨天搬家了",
            "original_local_date": "2099-01-01",
        },
    )

    def extract(_, _prompt, data, schema):
        assert not {"original_local_date", "relative_dates"}.intersection(data)
        return schema(memories=[])

    monkeypatch.setattr(distillation, "ask", extract)
    owner.request_distillation(session.id)
    distillation.process(
        service,
        owner.sessions,
        job_path(service.paths, "distillation", session.id),
        lambda _: False,
        Event(),
    )
    assert "original_local_date" not in service.session_query.user_message(session.id, "no-offset")


def test_edited_pending_confirmation_enqueues_once_and_reuses_context_after_disk_error(
    service, monkeypatch
):
    old = service.create_pending(
        MemoryDraft(text="候选正文", topic="diary", context=MemoryContext(location="旧地点")),
        "地点对吗？",
        [],
    )
    memory = service.approve(old.id, "我在杭州看展")
    assert service.approve(old.id, "我在杭州看展").id == memory.id and not service.pending()
    assert memory.context is None
    path = job_path(service.paths, "context", memory.id)
    calls = []

    def extract(_, prompt, data, schema):
        calls.append(data["confirmed_fact"])
        return schema(location="杭州")

    monkeypatch.setattr(context_fill, "ask", extract)
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise OSError("synthetic disk error")

        patch.setattr(service, "apply", fail)
        with pytest.raises(OSError):
            context_fill.fill(service, path, Event())
    assert read_job(path)["context"] == {"location": "杭州"}
    enqueue_context_fill(service.paths, memory.id, source_fingerprint(memory))
    context_fill.fill(service, path, Event())
    assert service.store.find(memory.id).context.location == "杭州"
    assert calls == ["我在杭州看展"]


def test_shutdown_waits_for_inflight_worker_before_new_owner_can_resume(service, monkeypatch):
    from time import monotonic, sleep

    owner = BackgroundMemory(service)
    session = owner.sessions.create()
    message(owner.sessions, session, "m", "关闭时的请求")
    entered, release, stopping, stopped = Event(), Event(), Event(), Event()

    def blocked(*_):
        entered.set()
        assert release.wait(5)
        raise TimeoutError("old provider")

    monkeypatch.setattr(distillation, "ask", blocked)
    owner.start()
    try:
        assert entered.wait(4)

        def shutdown():
            stopping.set()
            owner.stop()
            stopped.set()

        task = Thread(target=shutdown)
        task.start()
        assert stopping.wait(2)
        assert not stopped.wait(1.1)
        release.set()
        assert stopped.wait(3)
        task.join(2)
        path = job_path(service.paths, "distillation", session.id)
        assert "retry_at" not in read_job(path)
        assert owner.sessions.load(session.id).needs_consolidation()
        monkeypatch.setattr(distillation, "ask", lambda *args: distillation.Result(memories=[]))
        restarted = BackgroundMemory(service)
        restarted.start()
        try:
            until = monotonic() + 5
            while path.exists() and monotonic() < until:
                sleep(0.03)
            assert not path.exists()
            assert not owner.sessions.load(session.id).needs_consolidation()
        finally:
            restarted.stop()
    finally:
        release.set()
        owner.stop()
