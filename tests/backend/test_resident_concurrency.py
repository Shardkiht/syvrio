"""验证更新交接与跨会话发送时的并发边界。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from syvrio.application.turns import ConversationTurn
from syvrio.backend.api.channel import APIChannel
from syvrio.backend.api.routes import create_api
from syvrio.infra.session.errors import ConversationError
from syvrio.infra.storage.locking import VaultLock
from syvrio.memory.background import titles
from syvrio.memory.background.jobs import job_key

pytestmark = pytest.mark.web


def test_update_waits_for_title_and_prevents_new_background_work(service, tmp_path, monkeypatch):
    service.initialize_files()
    owner = APIChannel(service=service)
    bg = owner._background
    monkeypatch.setattr("syvrio.backend.updates.activity.syvrio_home", lambda: tmp_path)
    entered, finish = Event(), Event()

    def generate(*args):
        entered.set()
        assert finish.wait(10)
        return "Saved title"

    monkeypatch.setattr(titles, "generate", generate)
    sid = bg.sessions.create()
    bg.sessions.append(sid, {"type": "message", "id": "m1", "role": "user", "content": "Question"})
    bg.sessions.append(sid, {"type": "assistant", "id": "a1", "content": "Answer"})
    bg.request_title(sid.id)
    key = job_key(service.paths, "title", sid.id)
    api = create_api(owner, resident_token="local")
    with TestClient(
        api, base_url="http://127.0.0.1", headers={"X-Syvrio-Token": "local"}
    ) as client:
        ticket = {"ticket": "af" * 16}
        with ThreadPoolExecutor() as pool:
            work = pool.submit(bg._step_job, "title", key, Event())
            try:
                assert entered.wait(5)
                assert client.get("/api/update/instance").json()["busy"]
                assert client.post("/api/resident/update_drain", json=ticket).status_code == 409
                assert (
                    client.post(
                        "/api/update", json={"version": "1.0.0", "confirmed": True}
                    ).status_code
                    == 409
                )
            finally:
                finish.set()
            assert work.result(5)
        assert bg.sessions.load(sid.id).saved_title() == "Saved title"
        queued = bg.sessions.create()
        bg.sessions.append(
            queued, {"type": "message", "id": "m2", "role": "user", "content": "Next"}
        )
        bg.sessions.append(queued, {"type": "assistant", "id": "a2", "content": "Reply"})
        bg.request_title(queued.id)
        key = job_key(service.paths, "title", queued.id)
        with VaultLock(tmp_path / "update" / "update.lock"):
            assert client.post("/api/resident/update_drain", json=ticket).status_code == 200
            assert not bg._step_job("title", key, Event())
        # 无需新的 HTTP 写请求，更新进程退出后后台通道也会恢复。
        assert bg._step_job("title", key, Event())
        assert bg.sessions.load(queued.id).saved_title() == "Saved title"


def test_delivery_wait_does_not_block_other_view_or_rebind_after_navigation(service, monkeypatch):
    service.initialize_files()
    owner = APIChannel(service=service)
    directory = owner.directory
    first, second = (owner._sessions.create().id for _ in range(2))
    directory.select("a", first)
    directory.select("b", second)
    a = directory.channel("a").scheduling
    b = directory.channel("b").scheduling
    entered, finish = Event(), Event()

    def paused(text, sid, files):
        entered.set()
        assert finish.wait(10)
        return ConversationTurn(sid, text)

    monkeypatch.setattr(a, "submit", paused)
    monkeypatch.setattr(b, "submit", lambda text, sid, files: ConversationTurn(sid, text))
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(directory.submit_once, "a", "ab" * 16, "first", first)
        try:
            assert entered.wait(5)
            assert pool.submit(directory.queue_state, "b").result(2)["session_id"] == second
            other = pool.submit(directory.submit_once, "b", "cd" * 16, "second", second)
            assert other.result(2)["session_id"] == second
            with pytest.raises(ConversationError, match="being submitted"):
                directory.set_archived(first, True)
            pool.submit(directory.select, "a", second).result(2)
        finally:
            finish.set()
        assert pending.result(5)["session_id"] == first
    assert directory.queue_state("a")["session_id"] == second


def test_polling_one_busy_channel_leaves_the_directory_available(service, monkeypatch):
    service.initialize_files()
    directory = APIChannel(service=service).directory
    a = directory.channel("a").scheduling
    b = directory.channel("b").scheduling
    entered, finish = Event(), Event()
    original = a.queue_state

    def paused():
        entered.set()
        assert finish.wait(10)
        return original()

    monkeypatch.setattr(a, "queue_state", paused)
    with ThreadPoolExecutor() as pool:
        first = pool.submit(directory.queue_state, "a")
        try:
            assert entered.wait(5)
            assert (
                pool.submit(directory.queue_state, "b").result(2)["revision"]
                == b.queue_state()["revision"]
            )
        finally:
            finish.set()
        assert first.result(5)["session_id"] is None


def test_sqlite_receipt_waiting_for_writer_does_not_block_unrelated_view(service, monkeypatch):
    service.initialize_files()
    owner = APIChannel(service=service)
    first, second = (owner._sessions.create().id for _ in range(2))
    directory = owner.directory
    directory.select("a", first)
    directory.select("b", second)
    key = "ac" * 16
    receipts = owner._sessions.receipts
    receipts.reserve(key, "different-request", "previous")
    entered = Event()
    reserve = receipts.reserve

    def observed(*args):
        entered.set()
        return reserve(*args)

    monkeypatch.setattr(receipts, "reserve", observed)
    with ThreadPoolExecutor() as pool:
        with service.work.write():
            waiting = pool.submit(directory.submit_once, "a", key, "message", first)
            assert entered.wait(5)
            assert pool.submit(directory.queue_state, "b").result(2)["session_id"] == second
            assert not waiting.done()
        with pytest.raises(ConversationError) as rejected:
            waiting.result(5)
        assert rejected.value.code == "submission_conflict"
