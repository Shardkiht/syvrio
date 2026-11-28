"""在独立 Vault 和模拟模型上测试网页适配器，无需联网或真实密钥。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from syvrio.agent import runtime as agent_runtime
from syvrio.backend.api.channel import APIChannel as WebBackend
from syvrio.backend.api.channel import APIError as WebError
from syvrio.infra.config import (
    ChatConfig,
    Config,
    GitConfig,
    LLMConfig,
    RetrievalConfig,
    SearchConfig,
)
from syvrio.infra.llm import (
    AssistantTurn,
    ChatModelError,
    GenerationCancelled,
    LiteLLMChatModel,
    ToolCall,
)
from syvrio.infra.storage.database import transaction
from syvrio.infra.storage.locking import LockTimeout
from syvrio.memory.background import distillation
from syvrio.memory.background.jobs import job_key, read_job
from syvrio.memory.models import MemoryDraft
from syvrio.memory.service import MemoryService
from tests.support.proposals import decision_turn
from tests.support.resident_api import create_app
from tests.support.storage import rows

pytestmark = pytest.mark.web

BASE_URL = "http://127.0.0.1:8765"


def test_session_page_only_checks_its_own_title_jobs(service, monkeypatch):
    service.initialize_files()
    channel = WebBackend(service=service)
    ids = []
    for i in range(3):
        session = channel._sessions.create()
        channel._sessions.append(
            session, {"type": "message", "id": "u", "role": "user", "content": str(i)}
        )
        channel._sessions.append(session, {"type": "assistant", "id": "a", "content": "answer"})
        channel._background.request_title(session.id)
        ids.append(session.id)
    looked_up = []
    lookup = channel._background.pending_titles

    def page_lookup(session_ids):
        looked_up.extend(session_ids)
        return lookup(session_ids)

    monkeypatch.setattr(channel._background, "pending_titles", page_lookup)
    page = channel.list_sessions(limit=1, offset=1)
    assert len(page.items) == 1
    assert looked_up == [page.items[0].id]
    assert page.items[0].id in ids and page.items[0].title_pending
    assert not channel.list_sessions(limit=1, archived=True).items
    assert len(looked_up) == 1


def test_session_page_observes_title_completion(service, monkeypatch):
    from syvrio.memory.background import titles

    service.initialize_files()
    channel = WebBackend(service=service)
    session = channel._sessions.create()
    channel._sessions.append(
        session, {"type": "message", "role": "user", "id": "u", "content": "q"}
    )
    channel._sessions.append(session, {"type": "assistant", "id": "a", "content": "answer"})
    channel._background.request_title(session.id)
    lookup = channel._background.pending_titles
    monkeypatch.setattr(titles, "generate", lambda *_: "Finished")

    def finish_during_list(ids):
        with ThreadPoolExecutor(1) as worker:
            result = worker.submit(
                channel._background._step_job,
                "title",
                job_key(service.paths, "title", session.id),
                threading.Event(),
            )
            assert result.result(timeout=5)
        return lookup(ids)

    with monkeypatch.context() as context:
        context.setattr(channel._background, "pending_titles", finish_during_list)
        row = channel.list_sessions(limit=1).items[0]
    assert (row.title, row.title_pending) == ("未命名会话", True)
    refreshed = channel.list_sessions(limit=1).items[0]
    assert (refreshed.title, refreshed.title_pending) == ("Finished", False)


def test_rename_length_agrees_for_command_and_title_editor(client, backend):
    session_id = backend._sessions.create().id
    route = f"/api/sessions/{session_id}"
    assert client.post(f"{route}/switch").status_code == 200
    valid = "🌙" * 80
    assert client.put(f"{route}/title", json={"title": valid}).status_code == 204
    oversized = valid + "星"
    rejected = client.put(f"{route}/title", json={"title": oversized})
    assert rejected.status_code == 422
    result = client.post(
        "/api/commands",
        json={
            "line": f"/rename {oversized}",
            "session_id": session_id,
        },
    ).json()
    assert result["status"] == "error"
    assert result["message"] == rejected.json()["error"]["message"]
    assert client.get("/api/sessions").json()["items"][0]["title"] == valid


@pytest.mark.parametrize("upload", [False, True])
@pytest.mark.parametrize("existing_session", [False, True])
def test_concurrent_redelivery_returns_one_turn_even_if_first_receipt_was_lost(
    client, backend, upload, existing_session
):
    body = {"message": "Synthetic delivery probe", "submission_id": "ab" * 16}
    if existing_session:
        sid = backend._sessions.create().id
        assert client.post(f"/api/sessions/{sid}/switch").status_code == 200
        body["session_id"] = sid

    def post(payload, content=b"source"):
        if upload:
            return client.post(
                "/api/chat/attachments?receipt=1",
                data=payload,
                files={"files": ("note.txt", content)},
            )
        return client.post("/api/chat?receipt=1", json=payload)

    with ThreadPoolExecutor(2) as pool:
        first, retry = list(pool.map(lambda _: post(body), range(2)))
    assert first.status_code == retry.status_code == 202
    receipt = first.json()
    assert receipt["submission_id"] == retry.json()["submission_id"] == body["submission_id"]
    assert retry.json()["turn_id"] == receipt["turn_id"]
    assert client.get(f"/api/chat/turns/{receipt['turn_id']}").status_code == 200
    sid = receipt["session_id"]
    records = client.get(f"/api/sessions/{sid}").json()["items"]
    assert (
        len([row for row in records if row["role"] == "user"]) == backend._runtime.model.calls == 1
    )
    assert post({**body, "message": "A different payload"}).status_code == 409
    if upload:
        assert post(body, b"different bytes").status_code == 409
        assert not list((backend._service.paths.attachments_dir / ".incoming").iterdir())


class FakeModel(LiteLLMChatModel):
    """使用子类，让 AgentRuntime 走原生流式输出和思考处理路径。"""

    def __init__(self, turns):
        super().__init__(LLMConfig())
        self.turns = list(turns)
        self.calls = 0

    def chat(self, messages, tools, on_delta=None, cancel=None, *, on_reasoning=None):
        self.calls += 1
        turn = self.turns.pop(0) if self.turns else AssistantTurn(content="(empty)")
        if callable(turn):
            turn = turn(messages)
        if isinstance(turn, Exception):
            raise turn
        if on_reasoning and turn.reasoning_content:
            on_reasoning(turn.reasoning_content)
        if on_delta and turn.content:
            middle = max(1, len(turn.content) // 2)
            on_delta(turn.content[:middle])
            on_delta(turn.content[middle:])
        return turn


class GatedModel(FakeModel):
    def __init__(self):
        super().__init__([])
        self.entered = threading.Event()
        self.release = threading.Event()

    def chat(self, messages, tools, on_delta=None, cancel=None, *, on_reasoning=None):
        self.entered.set()
        if on_delta:
            on_delta("partial ")
        assert self.release.wait(timeout=10), "test model was never released"
        if cancel is not None and cancel.is_set():
            raise GenerationCancelled("Stopped by user")
        return AssistantTurn(content="partial done")


@pytest.fixture
def backend(tmp_path, monkeypatch):
    # 并发断言只计聊天与沉淀任务；模拟模型没有厂商元数据，临时 Vault 禁用定时 Git 同步。
    monkeypatch.setattr("syvrio.infra.llm.client.model_metadata", lambda _: {})
    monkeypatch.setattr(distillation, "ask", lambda *args: distillation.Result(memories=[]))
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(
            provider="custom", model="fake", api_key_env="SYVRIO_WEB_TEST_KEY", base_url=""
        ),
        chat=ChatConfig(
            auto_recall=0, recall_rewrite=False, extract_on_exit=False, max_tool_steps=4
        ),
        retrieval=RetrievalConfig(method="keyword"),
        search=SearchConfig(enabled=False, auto_start=False),
        git=GitConfig(auto_sync=False),
    )
    service = MemoryService(config)
    service.initialize_files()
    owner = WebBackend(service=service)
    owner._runtime.model = FakeModel([])
    owner.startup()
    yield owner
    owner.shutdown()


@pytest.fixture
def client(backend):
    return TestClient(create_app(backend, autostart=False), base_url=BASE_URL)


def test_manual_memory_browser_and_source_comparison(client, backend):
    def create(topic, text):
        payload = dict(topic=topic, text=text, request_id=uuid4().hex, confirmed=False)
        assert client.post("/api/memories", json=payload).status_code == 409
        payload["confirmed"] = True
        response = client.post("/api/memories", json=payload)
        assert response.status_code == 200, response.text
        assert client.post("/api/memories", json=payload).json() == response.json()
        return response.json()

    first = create("test/one", "Synthetic 100% match A")
    second = create("test/two", "Synthetic 100% match B")
    create("test/two", "Unrelated")
    sources = [first["id"], second["id"]]
    inference = backend._service.propose_inference("Synthetic observation", sources, "habit")
    backend._service.review_inference(inference.id, "confirm")

    params = {"q": "100% MATCH", "limit": 1}
    page = client.get("/api/memories", params=params).json()
    more = client.get("/api/memories", params={**params, "after": page["next"]}).json()
    assert [p["entries"][0]["id"] for p in [page, more]] == sorted(sources)
    assert more["next"] is None
    scoped = client.get("/api/memories", params={"q": "%", "topic": "test/one"}).json()
    assert [r["id"] for r in scoped["entries"]] == [first["id"]]

    url = "/api/memories/" + first["id"]
    edit = dict(expected_revision=first["revision"], text="Corrected synthetic A", confirmed=False)
    assert client.put(url, json=edit).status_code == 409
    edit["confirmed"] = True
    assert client.put(url, json=edit).json()["id"] == first["id"]
    assert client.put(url, json=edit).status_code == 409
    comparison = client.get(f"/api/inferences/{inference.id}/sources").json()["sources"]
    by_id = {s["id"]: s for s in comparison}
    assert set(by_id) == set(sources)
    assert by_id[first["id"]]["cited"]["text"] == "Synthetic 100% match A"
    assert by_id[first["id"]]["current"]["text"] == edit["text"]
    assert by_id[second["id"]]["cited"] == by_id[second["id"]]["current"]
    assert backend._service.list_inferences()[0].status == "confirmed"
    assert client.get("/api/inferences/missing/sources").status_code == 404


def test_memory_edit_conflict_reloads_only_the_active_entry(client, backend):
    added = client.post(
        "/api/memories",
        json=dict(topic="notes", text="Original text", request_id=uuid4().hex, confirmed=True),
    ).json()
    url = "/api/memories/" + added["id"]
    original = client.get(url).json()

    def save(revision, text):
        return client.put(url, json=dict(text=text, expected_revision=revision, confirmed=True))

    latest = save(original["revision"], "Updated elsewhere").json()
    stale = save(original["revision"], "My draft")
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "memory_changed"
    assert "Updated elsewhere" not in stale.text and "My draft" not in stale.text
    reread = client.get(url).json()
    assert reread == {**latest, "text": "Updated elsewhere", "topic": original["topic"]}
    assert save(reread["revision"], "Merged draft").status_code == 200
    assert client.get(url).json()["text"] == "Merged draft"

    backend._service.deprecate(added["id"])
    for unavailable in (client.get(url), save(latest["revision"], "Must not restore the entry")):
        assert unavailable.status_code == 404
        assert unavailable.json()["error"]["code"] == "memory_unavailable"
    assert client.get("/api/memories/missing").status_code == 404


def collect(client, message="hi", session_id=None):
    with client.stream(
        "POST",
        "/api/chat",
        json={"message": message, "session_id": session_id},
    ) as response:
        assert response.status_code == 200, response.text
        events = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        return events


def types(events):
    return [event["type"] for event in events]


def test_gateway_preserves_attachment_if_range(resident_backend):
    from dataclasses import asdict
    from io import BytesIO

    from syvrio.app.web.gateway import create_gateway
    from syvrio.infra.session.attachments import AttachmentBatch

    owner, resident = resident_backend
    sessions = owner._sessions
    session = sessions.create()
    data = b"synthetic attachment"
    batch = AttachmentBatch.stage(sessions.attachments_dir, [("notes.txt", BytesIO(data))])
    with batch.sending(sessions.paths):
        item = batch.files[0]
        sessions.append(
            session,
            {
                "type": "message",
                "role": "user",
                "content": "notes",
                "id": "m-file",
                "attachments": [asdict(item)],
            },
        )
    path = f"/api/sessions/{session.id}/messages/m-file/attachments/{item.id}"
    gateway = create_gateway(resident.base, "127.0.0.1")
    with TestClient(gateway, base_url=BASE_URL) as browser:
        full = browser.get(path)
        assert full.status_code == 200 and full.content == data
        current = browser.get(
            path, headers={"Range": "bytes=0-4", "If-Range": full.headers["etag"]}
        )
        assert current.status_code == 206 and current.content == data[:5]
        stale = browser.get(path, headers={"Range": "bytes=0-4", "If-Range": '"outdated"'})
        assert stale.status_code == 200 and stale.content == data


def test_mixed_attachments_reach_model_and_survive_move(client, backend, tmp_path, monkeypatch):
    import base64
    from io import BytesIO
    from shutil import copytree

    from PIL import Image
    from typer.testing import CliRunner

    from syvrio.app.cli import app as cli
    from syvrio.infra.session import SessionStore
    from syvrio.infra.storage.paths import SyvrioPaths

    image = BytesIO()
    Image.new("RGB", (2, 2), color="blue").save(image, "PNG")
    data = image.getvalue()
    secret_path = tmp_path / "local-only.txt"
    secret_path.write_text("must stay local", encoding="utf-8")

    class Model(FakeModel):
        def chat(self, messages, tools, **kwargs):
            if messages[-1]["role"] == "tool":
                assert "synthetic document" in messages[-1]["content"]
                return AssistantTurn(content="Verified both attachments.")
            parts = messages[-1]["content"]
            assert any(
                part.get("image_url", {}).get("url")
                == "data:image/png;base64," + base64.b64encode(data).decode()
                for part in parts
            )
            schema = next(
                tool["function"] for tool in tools if tool["function"]["name"] == "read_file"
            )
            assert schema["parameters"]["properties"]["path"]["enum"] == ["1. notes.txt"]
            return AssistantTurn(
                tool_calls=[ToolCall("read", "read_file", {"path": "1. notes.txt"})]
            )

    backend._runtime.model = Model([])
    response = client.post(
        "/api/chat/attachments",
        data={"message": f'Analyze these files "{secret_path}"'},
        files=[
            ("files", ("notes.txt", b"synthetic document")),
            ("files", ("renamed.txt", b"synthetic document")),
            ("files", ("picture.png", data)),
        ],
    )
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert response.status_code == 200 and events[-1]["type"] == "done", response.text
    assert events[-1]["text"] == "Verified both attachments."
    sid = events[-1]["session_id"]
    record = client.get(f"/api/sessions/{sid}").json()["items"][0]
    assert [file["name"] for file in record["attachments"]] == ["notes.txt", "picture.png"]
    saved = next(event for event in events if event["type"] == "message_saved")
    assert saved["attachments"] == [
        {key: item[key] for key in ("id", "name", "media_type", "size")}
        for item in record["attachments"]
    ]
    file_id = record["attachments"][1]["id"]
    draft = client.get(f"/api/sessions/{sid}/messages/{record['id']}/draft")
    assert draft.status_code == 200
    assert draft.json()["text"] == record["content"]
    assert [item["name"] for item in draft.json()["attachments"]] == ["notes.txt", "picture.png"]
    assert all(item["message_id"] == record["id"] for item in draft.json()["attachments"])
    url = f"/api/sessions/{sid}/messages/{record['id']}/attachments/{file_id}"
    # 下载线程与持锁的测试线程并发，不能重新加载整段历史。
    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
        patch.setattr(backend._sessions, "load", lambda *a, **kw: pytest.fail("full load"))
        with transaction(backend._service.paths):
            download = pool.submit(client.get, url).result(timeout=3)
    assert download.content == data and download.headers["content-disposition"].startswith(
        "attachment;"
    )
    from syvrio.backend.api.routes import AttachmentDownload

    snapshot_paths = []
    original_send = AttachmentDownload.__call__
    from syvrio.infra.session.attachment_objects import original_path

    live = original_path(backend._service.paths.attachments_dir, file_id)

    async def send_after_removal(response, scope, receive, send):
        snapshot_paths.append(response.path)
        live.unlink()
        await original_send(response, scope, receive, send)

    with monkeypatch.context() as patch:
        patch.setattr(AttachmentDownload, "__call__", send_after_removal)
        ranged = client.get(url, headers={"Range": "bytes=1-5"})
        assert ranged.status_code == 206 and ranged.content == data[1:6]
        assert ranged.headers["content-range"] == f"bytes 1-5/{len(data)}"
        assert not snapshot_paths[0].parent.exists()
        assert client.get(url).status_code == 404
    live.write_bytes(data)
    import errno
    from pathlib import Path
    from unittest.mock import MagicMock

    open_path = Path.open
    failed_directories = []

    def open_full_destination(path, mode="r", *args, **kwargs):
        if path.name == "content" and mode == "xb":
            failed_directories.append(path.parent)
            destination = MagicMock()
            destination.__enter__.return_value.write.side_effect = OSError(errno.ENOSPC, "full")
            return destination
        return open_path(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_full_destination)
        failure = client.get(url)
        assert (
            failure.status_code == 507
            and failure.json()["error"]["code"] == "download_storage_full"
        )
        assert failed_directories and not failed_directories[0].exists()
    preview = client.get(url, params={"preview": True})
    assert preview.content == data and preview.headers["content-type"] == "image/png"
    assert preview.headers["content-disposition"].startswith("inline;")
    assert preview.headers["x-content-type-options"] == "nosniff"
    doc_url = url.rsplit("/", 1)[0] + "/" + record["attachments"][0]["id"]
    assert (
        client.get(doc_url + "?preview=true").headers["content-type"] == "application/octet-stream"
    )
    backend._background.stop()
    assert client.post(f"/api/sessions/{sid}/archive").status_code == 200
    assert client.get(url).status_code == 404
    assert client.get(url, params={"archived": True}).content == data
    assert client.get(url + "?archived=true&preview=true").headers["content-type"] == "image/png"
    assert client.post(f"/api/sessions/{sid}/restore").status_code == 200
    assert client.get(url).content == data
    paths = backend._service.paths
    backend.git.is_dirty()
    backend.git.repo.stage_all()
    staged = backend.git.repo.staged_paths()
    assert any(item.startswith("content/sessions/") for item in staged)
    assert not any(item.startswith("attachments/") for item in staged)
    live.write_bytes(b"x" * len(data))
    assert client.get(url + "?preview=true").status_code == 404
    live.unlink()
    assert client.get(url + "?preview=true").status_code == 404
    live.write_bytes(data)
    # 完成在线校验后停服再搬迁，确保数据库、WAL 和后端独占锁均已释放。
    backend.shutdown()
    moved = SyvrioPaths(tmp_path / "moved-vault", tmp_path / "moved-runtime")
    copytree(paths.vault_dir, moved.vault_dir)
    sessions = SessionStore(moved)
    assert sessions.verify_attachments() == (2, [])
    command = ["verify-attachments", "--vault", str(moved.vault_dir)]
    result = CliRunner().invoke(cli, command)
    assert result.exit_code == 0 and "Verified 2 attachment(s)" in result.output
    assert not moved.runtime_dir.exists()
    path = original_path(moved.attachments_dir, file_id)
    path.write_bytes(b"x" * len(data))
    count, errors = sessions.verify_attachments()
    assert count == 1 and len(errors) == 1 and "checksum" in errors[0]
    path.unlink()
    result = CliRunner().invoke(cli, command)
    assert result.exit_code == 1 and "picture.png" in result.output and "missing" in result.output


def test_latest_message_actions_use_the_saved_id_and_reject_an_outdated_view(client, backend):
    first = collect(client, "same question")
    sid = first[-1]["session_id"]
    first_mid = next(event["message_id"] for event in first if event["type"] == "message_saved")
    second = collect(client, "same question", sid)
    second_mid = next(event["message_id"] for event in second if event["type"] == "message_saved")
    assert first_mid != second_mid
    assert client.get(f"/api/sessions/{sid}/messages/{first_mid}/draft").status_code == 409
    body = {"line": "/undo", "session_id": sid, "expected_message_id": first_mid}
    assert client.post("/api/commands", json=body).json()["status"] == "error"
    assert len(backend._sessions.load(sid).messages) == 2
    body["expected_message_id"] = second_mid
    result = client.post("/api/commands", json=body).json()
    assert result["status"] == "ok" and result["data"]["restored"] == "same question"
    assert [record["id"] for record in backend._sessions.load(sid).messages] == [first_mid]


def test_web_chat_paths_do_not_authorize_server_files(backend, client, tmp_path):
    path = tmp_path / "private.txt"
    path.write_text("local only", encoding="utf-8")

    class Model(FakeModel):
        def chat(self, messages, tools, **kwargs):
            names = {tool["function"]["name"] for tool in tools}
            assert not {"read_file", "import_file"}.intersection(names)
            return AssistantTurn(content="No local file access.")

    backend._runtime.model = Model([])
    assert collect(client, f'Read "{path}"')[-1]["text"] == "No local file access."
    assert (
        client.post("/api/chat/attachments", files=[("files", ("bad.png", b"invalid"))]).status_code
        == 422
    )


def test_clients_generate_in_parallel_and_share_fifo_for_the_same_session(client, backend):
    first, second = (backend._sessions.create().id for _ in range(2))
    peer = TestClient(client.app, base_url=BASE_URL, headers={"X-Syvrio-View": "a" * 32})
    observer = TestClient(client.app, base_url=BASE_URL, headers={"X-Syvrio-View": "b" * 32})
    entered = {label: threading.Event() for label in ("one", "two")}
    finish = threading.Event()

    class Model(FakeModel):
        def chat(self, messages, tools, on_delta=None, cancel=None, *, on_reasoning=None):
            label = messages[-1]["content"]
            if label in entered:
                entered[label].set()
                assert finish.wait(10)
            return AssistantTurn(content=f"reply {label}")

    backend._runtime.model = Model([])
    for tab, target in ((client, first), (peer, second), (observer, first)):
        assert tab.post(f"/api/sessions/{target}/switch").status_code == 200
    with ThreadPoolExecutor(max_workers=4) as pool:
        a = pool.submit(collect, client, "one", first)
        b = pool.submit(collect, peer, "two", second)
        try:
            assert all(event.wait(5) for event in entered.values())
            first_turn = client.get("/api/health").json()["active_turn_id"]
            second_turn = peer.get("/api/health").json()["active_turn_id"]
            assert observer.get("/api/health").json()["active_turn_id"] == first_turn
            assert first_turn and second_turn and first_turn != second_turn
            assert not peer.post("/api/chat/cancel", json={"turn_id": first_turn}).json()[
                "cancelled"
            ]
            queued = pool.submit(collect, observer, "next", first)
            for _ in range(200):
                if backend.scheduling.queue_state()["waiting"]:
                    break
                time.sleep(0.005)
            assert backend.scheduling.queue_state()["waiting"] and not queued.done()
        finally:
            finish.set()
        assert [item.result()[-1]["type"] for item in (a, b, queued)] == ["done"] * 3
    assert backend._sessions.load(first).user_texts() == ["one", "next"]
    assert backend._sessions.load(second).user_texts() == ["two"]
    assert observer.post("/api/sessions").status_code == 201
    assert any(
        row["current"] and row["id"] == first for row in client.get("/api/sessions").json()["items"]
    )


@pytest.mark.parametrize("destination", ["existing", "new", "command"])
def test_one_view_can_leave_a_running_session_and_return_to_its_queue(client, backend, destination):
    model = GatedModel()
    backend._runtime.model = model
    other = backend._sessions.create().id

    def send(text, session):
        response = client.post("/api/chat?receipt=1", json={"message": text, "session_id": session})
        assert response.status_code == 202, response.text
        return response.json()

    first = send("first", None)
    sid = first["session_id"]
    try:
        assert model.entered.wait(5)
        queued = send("second", sid)
        if destination == "existing":
            moved = client.post(f"/api/sessions/{other}/switch")
        elif destination == "new":
            moved = client.post("/api/sessions")
        else:
            moved = client.post("/api/commands", json={"line": "/new", "session_id": sid})
        assert moved.is_success, moved.text
        if destination == "command":
            assert moved.json()["status"] == "ok"
        away = client.get("/api/chat/queue").json()
        assert away["session_id"] == (other if destination == "existing" else None)
        assert away["active"] is None and away["waiting"] == []
        assert not client.post("/api/chat/cancel", json={"turn_id": first["turn_id"]}).json()[
            "cancelled"
        ]
        assert client.get(f"/api/chat/turns/{first['turn_id']}").status_code == 404
        assert client.post(f"/api/sessions/{sid}/archive").status_code == 409
        assert client.post(f"/api/sessions/{sid}/switch").is_success
        returned = client.get("/api/chat/queue").json()
        assert returned["active"]["turn_id"] == first["turn_id"]
        assert [t["turn_id"] for t in returned["waiting"]] == [queued["turn_id"]]
        assert client.post(f"/api/sessions/{other}/switch").is_success
    finally:
        model.release.set()
    # 即使唯一的视图已切换到别处且尚未返回，两个回合仍会完成。
    assert backend.scheduling.registry.get(queued["turn_id"], sid).finished.wait(10)
    assert backend._sessions.load(sid).user_texts() == ["first", "second"]
    assert client.get("/api/chat/queue").json()["session_id"] == other


def wait_active(backend):
    for _ in range(200):
        if backend.active_turn_id() is not None:
            return backend.active_turn_id()
        time.sleep(0.01)
    raise AssertionError("turn never started")


def test_sidebar_activity_tracks_concurrent_replies_after_leaving_a_view(client, backend):
    views = [{"X-Syvrio-View": uuid4().hex} for _ in range(2)]
    models = [GatedModel(), GatedModel()]
    turns = []

    def observed():
        state = client.get("/api/chat/queue").json()
        assert state["session_id"] is None and state["active"] is None
        return set(state["replying_session_ids"])

    assert observed() == set()
    try:
        for headers, model in zip(views, models, strict=True):
            backend.directory.channel(headers["X-Syvrio-View"])._runtime.model = model
            sent = client.post(
                "/api/chat?receipt=1", headers=headers, json={"message": "Reply status test"}
            )
            assert sent.status_code == 202
            turns.append(sent.json())
            assert model.entered.wait(3)
        assert client.post("/api/view/disconnect", headers=views[0]).is_success
        assert observed() == {turn["session_id"] for turn in turns}

        stopped = client.post(
            "/api/chat/cancel", headers=views[1], json={"turn_id": turns[1]["turn_id"]}
        )
        assert stopped.json()["cancelled"]
        models[1].release.set()
        assert backend.scheduling.registry.get(
            turns[1]["turn_id"], turns[1]["session_id"]
        ).finished.wait(5)
        assert observed() == {turns[0]["session_id"]}
    finally:
        for model in models:
            model.release.set()
    assert backend.scheduling.registry.get(
        turns[0]["turn_id"], turns[0]["session_id"]
    ).finished.wait(5)
    assert observed() == set()


# 健康检查、会话与历史


def test_health_and_openapi(client, backend):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["provider"] == "custom"
    assert body["busy"] is False
    assert body["single_client"] is False
    assert client.get("/openapi.json").status_code == 200


def test_context_usage_tracks_requested_session_without_switching(client, backend):
    config = backend._service.config.chat
    config.reserve_output_tokens = 10000
    config.compact_threshold = 0.75
    empty = client.get("/api/context-usage").json()
    assert empty == {
        "session_id": None,
        "used": 0,
        "limit": backend._app_service.settings.context_window,
        "compact_threshold": 0.75,
        "compaction_trigger_tokens": 30000,
    }
    session = backend._sessions.create()
    backend._sessions.append(
        session, {"type": "message", "role": "user", "content": "Synthetic context sample."}
    )
    response = client.get("/api/context-usage", params={"session_id": session.id})
    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == session.id
    assert data["used"] == backend._conversation.estimate_usage(session) > 0
    assert backend._conversation.current_id is None
    assert client.get("/api/context-usage", params={"session_id": "../vault"}).status_code == 422


@pytest.mark.parametrize("stop", [False, True], ids=["finish", "cancel"])
def test_context_usage_tracks_only_the_saved_active_tool_chain(client, backend, stop):
    from dataclasses import replace

    from syvrio.infra.llm.client import _reasoning_route
    from syvrio.infra.llm.tokens import estimate_tokens

    runtime, store, conversation = backend._runtime, backend._sessions, backend._conversation
    config = replace(
        backend._service.config.llm,
        provider="qianwen",
        model="glm-5.3",
        base_url="https://maas.qianwenaiapi.com/compatible-mode/v1",
    )
    runtime.reload_llm(replace(backend._service.config, llm=config))
    source, policy = _reasoning_route(config)
    assert policy == "turn"
    sid = store.create().id
    assert client.post(f"/api/sessions/{sid}/switch").status_code == 200
    session = runtime.current

    def save_call(mid, thought):
        store.append(
            session,
            {
                "id": mid,
                "type": "assistant",
                "content": "",
                "tool_calls": [{"id": mid, "name": "recall", "arguments": {}}],
                "reasoning": {"text": thought, "source": source},
            },
        )

    def check(thought=""):
        response = client.get("/api/context-usage", params={"session_id": sid})
        assert response.status_code == 200
        assert response.json()["used"] == runtime.estimate_usage(session) + estimate_tokens(thought)

    store.append(session, {"id": "old", "type": "message", "role": "user", "content": "Old turn"})
    save_call("old-call", "上次未完成的思考" * 100)
    check()
    conversation.begin_turn(sid)
    tid = conversation.active_turn_id
    thought = "本轮工具链思考" * 400
    try:
        check()
        # 快照可能早于消息保存的回调，此时不能把旧回合算作当前回合。
        conversation.bind_user_message("current")
        check()
        store.append(
            session, {"id": "current", "type": "message", "role": "user", "content": "Next turn"}
        )
        check()
        save_call("new-call", thought)
        check(thought)
        store.append(session, {"type": "tool", "tool_call_id": "new-call", "content": "Result"})
        check(thought)
        if stop:
            response = client.post("/api/chat/cancel", json={"turn_id": tid})
            assert response.status_code == 200 and response.json()["cancelled"]
        else:
            store.append(session, {"type": "assistant", "content": "Final answer"})
        check()
    finally:
        conversation.end_turn(tid)
    check()


def test_session_lifecycle(client, backend):
    before = len(backend._sessions.list())
    created = client.post("/api/sessions")
    assert created.status_code == 201
    body = created.json()
    # 新聊天先打开空视图，发送第一条消息时才创建文件。
    assert body["session_id"] is None
    assert body["draft"] is True
    assert len(backend._sessions.list()) == before

    session = backend._sessions.create()
    listing = client.get("/api/sessions").json()
    assert any(item["id"] == session.id for item in listing["items"])

    history = client.get(f"/api/sessions/{session.id}").json()
    assert history["session_id"] == session.id
    assert history["items"] == []

    switched = client.post(f"/api/sessions/{session.id}/switch")
    assert switched.status_code == 200
    assert switched.json()["session_id"] == session.id
    assert client.get(f"/api/sessions/{session.id}").json()["current"] is True


def test_first_message_persists_the_session(client, backend):
    backend._runtime.model = FakeModel([AssistantTurn(content="hello")])
    before = len(backend._sessions.list())
    events = collect(client)
    assert len(backend._sessions.list()) == before + 1
    # 工作线程在 start 事件之后才创建文件。客户端从后续事件帧中取得真实会话 ID。
    ids = {event["session_id"] for event in events if event.get("session_id")}
    assert len(ids) == 1
    session_id = ids.pop()
    saved = [event for event in events if event["type"] == "message_saved"]
    assert len(saved) == 1 and saved[0]["session_id"] == session_id
    assert types(events).index("message_saved") < types(events).index("content_delta")
    assert backend._sessions.exists(session_id)
    rows = client.get("/api/sessions").json()["items"]
    assert [row["id"] for row in rows if row["current"]] == [session_id]


@pytest.mark.parametrize("reserve_before_tools", [True, False])
def test_web_insert_waits_for_the_model_and_never_splits_a_tool_batch(
    client, backend, monkeypatch, reserve_before_tools
):
    class Model(FakeModel):
        def __init__(self):
            super().__init__([])
            self.entered = threading.Event()
            self.release = threading.Event()
            self.followup = []

        def chat(self, messages, tools, on_delta=None, cancel=None, *, on_reasoning=None):
            self.calls += 1
            if self.calls == 1:
                self.entered.set()
                assert self.release.wait(5)
                return AssistantTurn(tool_calls=[ToolCall("call-1", "recall", {"query": "sample"})])
            self.followup = [dict(message) for message in messages]
            return AssistantTurn(content="Follow-up complete.")

    model = backend._runtime.model = Model()
    offer_reached = threading.Event()
    tool_entered = threading.Event()
    release_tool = threading.Event()
    original_offer = backend._conversation.insert_message
    original_results = agent_runtime._tool_results

    def offer(*args):
        result = original_offer(*args)
        if result is not None:
            offer_reached.set()
        return result

    def gated_tools(*args, **kwargs):
        tool_entered.set()
        assert release_tool.wait(5)
        return original_results(*args, **kwargs)

    monkeypatch.setattr(backend._conversation, "insert_message", offer)
    monkeypatch.setattr(agent_runtime, "_tool_results", gated_tools)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stream = pool.submit(collect, client, "original")
        try:
            assert model.entered.wait(5)
            payload = {
                "turn_id": backend.active_turn_id(),
                "session_id": backend._conversation.current_id,
            }
            pending = None
            if reserve_before_tools:
                pending = pool.submit(
                    client.post, "/api/chat/insert", json={**payload, "message": "a supplement"}
                )
                assert offer_reached.wait(5)
                assert not pending.done()
            model.release.set()
            assert tool_entered.wait(5)
            rejected = client.post(
                "/api/chat/insert", json={**payload, "message": "must not interrupt the tool"}
            )
            assert rejected.status_code == 409
            assert rejected.json()["error"]["code"] == "insertion_unavailable"
            if pending is not None:
                assert not pending.done()
        finally:
            model.release.set()
            release_tool.set()
        if pending is not None:
            assert pending.result(timeout=5).status_code == 204
        events = stream.result(timeout=5)
    if reserve_before_tools:
        assert types(events).index("tool_end") < types(events).index("message_inserted")
        assert model.followup[-2]["role"] == "tool"
        assert model.followup[-1] == {"role": "user", "content": "a supplement"}
        assert backend._conversation.current.user_texts() == ["original", "a supplement"]
        inserted = next(item for item in events if item["type"] == "message_inserted")
        sid, mid = inserted["session_id"], inserted["message_id"]
        draft = client.get(f"/api/sessions/{sid}/messages/{mid}/draft").json()
        assert draft["text"] == "original\n\na supplement"
        undo = client.post("/api/commands", json={"line": "/undo"}).json()
        assert undo["data"]["restored"] == "original\n\na supplement"
        assert backend._conversation.current.user_texts() == []
    else:
        assert "message_inserted" not in types(events)
        assert model.followup[-1]["role"] == "tool"
        assert backend._conversation.current.user_texts() == ["original"]


def test_web_undo_during_generation_returns_saved_text_and_waits_for_rollback(client, backend):
    model = backend._runtime.model = GatedModel()
    with ThreadPoolExecutor(max_workers=2) as pool:
        stream = pool.submit(collect, client, "please undo me")
        try:
            assert model.entered.wait(5)
            session_id = backend._conversation.current_id
            target = backend._conversation.ask_user_message_id
            assert target
            stale = client.post(
                "/api/commands",
                json={
                    "line": "/undo",
                    "session_id": session_id,
                    "expected_message_id": "stale",
                },
            ).json()
            assert stale["status"] == "error" and not backend._conversation.undo_requested
            # 普通命令互斥锁已由另一条命令持有。
            # 撤回会在该锁释放前登记取消请求。
            with backend.scheduling.command_lock:
                command = pool.submit(
                    client.post,
                    "/api/commands",
                    json={"line": "/undo", "session_id": session_id, "expected_message_id": target},
                )
                assert backend._conversation.cancel_event.wait(5)
                assert not command.done()
                duplicate = client.post(
                    "/api/commands", json={"line": "/undo", "session_id": session_id}
                )
                assert duplicate.json()["status"] == "busy"
        finally:
            model.release.set()
        response = command.result(timeout=5).json()
        assert response["status"] == "ok"
        assert response["data"]["restored"] == "please undo me"
        assert response["session_id"] == session_id
        assert types(stream.result(timeout=5))[-1] == "cancelled"
    assert client.get(f"/api/sessions/{session_id}").json()["items"] == []
    assert backend._conversation.current.user_texts() == []


def test_undo_during_tool_write_reverts_the_completed_write_and_stops_the_next(
    client, backend, monkeypatch
):
    service = backend._service
    arguments = {"text": "保存合成偏好", "topic": "undo-inflight"}
    model = backend._runtime.model = FakeModel(
        [
            AssistantTurn(tool_calls=[ToolCall("review", "memorize", arguments)]),
            decision_turn(
                "write-1",
                after=[
                    ToolCall(
                        "write-2",
                        "correct",
                        {"entry_id": "not-run", "text": "must not execute", "topic": "other"},
                    ),
                ],
            ),
        ]
    )
    entered = threading.Event()
    release = threading.Event()

    def affected():
        return service.store.entries("undo-inflight")

    memorize = service.memorize
    written = threading.Event()

    def gated_write(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        result = memorize(*args, **kwargs)
        if affected():
            written.set()
        return result

    monkeypatch.setattr(service, "memorize", gated_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stream = pool.submit(collect, client, "tool undo")
        try:
            assert entered.wait(5)
            session_id = backend._conversation.current_id
            undo = pool.submit(
                client.post, "/api/commands", json={"line": "/undo", "session_id": session_id}
            )
            assert backend._conversation.cancel_event.wait(5)
            assert not undo.done()
        finally:
            release.set()
        response = undo.result(timeout=5).json()
        assert response["status"] == "ok"
        assert response["data"]["restored"] == "tool undo"
        events = stream.result(timeout=5)
    assert written.is_set() and not affected()
    assert model.calls == 2
    assert not any(
        event.get("name") == "correct" for event in events if event["type"] == "tool_start"
    )
    assert backend._conversation.current.user_texts() == []


# 会话删除


def test_searches_all_configured_models_and_can_select_a_result_beyond_the_first_page(
    client, backend, monkeypatch
):
    from syvrio.application.contracts import SettingsResult

    settings = backend._app_service.settings
    catalog = {
        "custom": [f"fixture-{n:03d}" for n in range(45)],
        "deepseek": ["Chat-New-Model"],
        "spark": [],
    }
    requests = []

    def discover(*, cached_only=False, refresh=False):
        requests.append((cached_only, refresh))
        return catalog

    monkeypatch.setattr(settings, "model_catalog", discover)
    selected = []

    def switch(provider, model):
        selected.append((provider, model))
        return SettingsResult(True, "switched")

    monkeypatch.setattr(settings, "apply_model", switch)

    result = client.get("/api/settings/models", params={"q": " new-MODEL "}).json()
    assert [(row["provider"], row["model"]) for row in result["items"]] == [
        ("deepseek", "Chat-New-Model")
    ]
    assert result["unavailable_providers"] == ["spark"] and not selected
    assert requests == [(False, False)]
    assert client.get("/api/settings/models", params={"q": "not-present"}).json()["items"] == []
    assert len(client.get("/api/settings/models", params={"refresh": True}).json()["items"]) == 47
    assert requests[-1] == (False, True)
    choices = client.post("/api/commands", json={"line": "/model"}).json()["data"]["options"]
    assert len(choices) == 47
    assert ("deepseek", "Chat-New-Model") in [(row["provider"], row["model"]) for row in choices]
    applied = client.post("/api/commands", json={"line": "/model deepseek/Chat-New-Model"}).json()
    assert applied["status"] == "ok" and selected == [("deepseek", "Chat-New-Model")]


def test_archive_then_confirm_permanent_deletion(client, backend):
    store = backend._sessions
    target = store.create()
    store.append(
        target, {"type": "message", "id": "m-1", "role": "user", "content": "original bytes"}
    )
    original = store.load(target.id).records
    url = f"/api/sessions/{target.id}"
    permanent = {"permanent": True, "confirmed": True}
    assert client.delete(url, params=permanent).json()["error"]["code"] == "archive_required"
    for params in ({}, {"confirmed": True}):
        assert client.delete(url, params=params).json() == {
            "session_id": target.id,
            "archived": True,
        }
    assert not store.exists(target.id)
    assert store.load(target.id, archived=True).records == original
    assert client.get(url).status_code == 404
    assert (
        client.get(url, params={"archived": True}).json()["items"][0]["content"] == "original bytes"
    )
    assert target.id not in {row["id"] for row in client.get("/api/sessions").json()["items"]}
    assert target.id in {
        row["id"] for row in client.get("/api/sessions?archived=true").json()["items"]
    }
    assert (
        client.delete(url, params={"permanent": True}).json()["error"]["code"] == "confirm_required"
    )
    assert store.exists(target.id, archived=True)
    assert client.delete(url, params=permanent).json() == {"session_id": target.id, "deleted": True}
    assert not store.exists(target.id, archived=True)


def test_archiving_idle_readers_detaches_each_immediately_and_restores_bytes(client, backend):
    store = backend._sessions
    target = store.create()
    url = f"/api/sessions/{target.id}"
    first, second = ({"X-Syvrio-View": uuid4().hex} for _ in range(2))
    for headers in (first, second):
        assert client.post(url + "/switch", headers=headers).status_code == 200
    original = store.load(target.id).records
    assert client.post(url + "/archive", headers=first).status_code == 200
    for headers in (first, second):
        queue = client.get("/api/chat/queue", headers=headers).json()
        assert queue["session_id"] is None and queue["archived_session_id"] == target.id
    assert client.post(url + "/switch", headers=second).status_code == 404
    assert client.post(url + "/restore", headers=second).status_code == 200
    assert store.load(target.id).records == original
    assert client.post(url + "/switch", headers=first).status_code == 200
    assert client.get("/api/chat/queue", headers=first).json()["session_id"] == target.id
    assert client.get("/api/chat/queue", headers=second).json()["session_id"] is None


def test_cli_archive_and_purge_use_resident_and_require_manual_confirmation(
    client, backend, monkeypatch
):
    from typer.testing import CliRunner

    from syvrio.app import cli
    from syvrio.backend.client import ResidentClient

    class LocalClient:
        def request(self, method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            ResidentClient._check(response)
            return response.json() if response.content else None

    monkeypatch.setattr(cli, "_service", lambda *_: LocalClient())
    store = backend._sessions
    target = store.create()
    runner = CliRunner()
    for flag in ("--archive", "--restore", "--archive"):
        result = runner.invoke(cli.app, ["sessions", flag, target.id])
        assert result.exit_code == 0, result.output
        assert store.exists(target.id, archived=flag == "--archive")
    command = ["sessions", "--delete-permanently", target.id]
    assert runner.invoke(cli.app, command, input="\n").exit_code == 0
    assert store.exists(target.id, archived=True)
    assert runner.invoke(cli.app, command, input="y\n").exit_code == 0
    assert not store.exists(target.id, archived=True)


def test_running_turn_and_its_queue_are_protected_from_another_view(client, backend):
    owner, requester = ({"X-Syvrio-View": uuid4().hex} for _ in range(2))
    model = backend._runtime.model = GatedModel()
    first = client.post("/api/chat?receipt=1", headers=owner, json={"message": "first"}).json()
    sid = first["session_id"]
    try:
        assert model.entered.wait(5)
        queued = client.post(
            "/api/chat?receipt=1", headers=owner, json={"message": "second", "session_id": sid}
        ).json()
        response = client.post(f"/api/sessions/{sid}/archive", headers=requester)
        assert response.status_code == 409 and backend._sessions.exists(sid)
        assert response.json()["error"]["code"] == "session_protected"
    finally:
        model.release.set()
    for turn in (first, queued):
        assert client.get(f"/api/chat/turns/{turn['turn_id']}", headers=owner).status_code == 200
    assert backend._sessions.load(sid).user_texts() == ["first", "second"]


def test_full_tool_history_is_scoped_to_visible_records_and_archive(client, backend):
    store = backend._sessions
    session = store.create()
    content = "工具结果" * 200 + "End of result."
    for row in (
        {"id": "withdrawn", "type": "message", "role": "user", "content": "Withdraw this"},
        {"id": "old", "type": "tool", "content": content},
        {"type": "undo", "message_id": "withdrawn"},
        {"id": "question", "type": "message", "role": "user", "content": "Read this"},
        {"id": "long", "type": "tool", "content": content, "tool_call_id": "call-1"},
    ):
        store.append(session, row)
    base = f"/api/sessions/{session.id}"
    record = client.get(base).json()["items"][-1]
    assert record["tool_call_id"] == "call-1" and record["id"] == "long"
    assert record["tool_content_truncated"] and record["tool_content_length"] == len(content)
    assert record["content"] == content[:300]
    assert client.get(base + "/tools/long").json() == {"content": content}
    for hidden in ("old", "question", "missing"):
        assert client.get(base + "/tools/" + hidden).status_code == 404
    assert client.post(base + "/archive").status_code == 200
    assert client.get(base + "/tools/long").status_code == 404
    assert client.get(base + "/tools/long?archived=true").json() == {"content": content}


def test_pending_evidence_and_chat_search_follow_saved_messages(client, backend):
    store, service = backend._sessions, backend._service
    session = store.create()
    store.rename(session, "Library room")
    for record in (
        {"type": "message", "id": "old", "role": "user", "content": "Withdrawn BLUE-17"},
        {"type": "undo", "message_id": "old"},
        {"type": "message", "id": "src", "role": "user", "content": "Reserved BLUE-17 阅读室"},
        {"type": "tool", "id": "hidden", "content": "hidden BLUE-17"},
        {"type": "assistant", "id": "answer", "content": "Confirmed blue-17"},
    ):
        store.append(session, record)
    fact = service.add(MemoryDraft(text="Related library fact", topic="library"))
    draft = MemoryDraft(text="Review reservation", topic="library")
    item = service.create_pending(
        draft, "Is this correct?", [fact.id], session_id=session.id, message_id="src"
    )
    url = f"/api/pending/{item.id}/evidence"
    evidence = client.get(url).json()
    assert evidence["source"]["title"] == "Library room"
    assert evidence["source"]["content"] == "Reserved BLUE-17 阅读室"
    assert evidence["related"] == [{"id": fact.id, "text": fact.text}]
    search = "/api/sessions/search"
    hits = client.get(search, params={"q": "bLuE-17"}).json()["results"]
    assert [hit["message_id"] for hit in hits] == ["answer", "src"]
    assert client.get(search, params={"q": "library room"}).json()["results"][0]["role"] == "title"
    ctx = f"/api/sessions/{session.id}/messages/src/context"
    assert [m["message_id"] for m in client.get(ctx).json()["messages"]] == ["src", "answer"]
    assert client.get(search, params={"q": ".*"}).json()["results"] == []
    assert client.get(search, params={"q": " "}).status_code == 422
    assert client.post(f"/api/sessions/{session.id}/archive").status_code == 200
    assert client.get(search, params={"q": "blue-17"}).json()["results"] == []
    assert client.get(ctx).status_code == 404
    assert client.get(url).json()["source"]["archived"] is True
    service.reject(item.id)
    assert client.get(url).status_code == 404
    orphan = service.create_pending(draft, "Imported?", [])
    assert client.get(f"/api/pending/{orphan.id}/evidence").json()["state"] == "unrecorded"


def test_permanent_deletion_rechecks_pending_revision_and_saved_memory_sources(client, backend):
    service = backend._runtime.service
    store = backend._sessions
    target, other = store.create(), store.create()
    for session in (target, other):
        store.append(
            session, {"type": "message", "role": "user", "content": "original", "id": "m-src"}
        )

    def draft(session, text):
        return service.create_pending(
            MemoryDraft(text=text, topic="topic"),
            "Check wording?",
            [],
            session_id=session.id,
            message_id="m-src",
        ).id

    first, second = draft(target, "First draft"), draft(target, "Second draft")
    survivor = draft(other, "Other session")
    url = f"/api/sessions/{target.id}"
    assert client.post(url + "/archive").status_code == 200
    before = client.get(url + "/deletion-preview").json()
    assert {row["id"] for row in before["pending"]} == {first, second}
    service.reject(second)
    params = {"confirmed": True, "permanent": True, "pending_revision": before["revision"]}
    refused = client.delete(url, params=params)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "pending_changed"
    assert store.exists(target.id, archived=True) and service.queue.get(first)
    after = client.get(url + "/deletion-preview").json()
    assert after["pending"] == [{"id": first, "text": "First draft"}]
    params["pending_revision"] = after["revision"]
    assert client.delete(url, params=params).status_code == 200
    assert not store.exists(target.id, archived=True) and [
        item.id for item in service.pending()
    ] == [survivor]

    url = f"/api/sessions/{other.id}"
    fact = service.add(
        MemoryDraft(text="Persisted", topic="topic"), session_id=other.id, message_id="m-src"
    )
    assert client.post(url + "/archive").status_code == 200
    for response in (client.get(url + "/deletion-preview"), client.delete(url, params=params)):
        assert (
            response.status_code == 409 and response.json()["error"]["code"] == "session_protected"
        )
    assert (
        service.store.find(fact.id)
        and service.queue.get(survivor)
        and store.exists(other.id, archived=True)
    )


def test_delete_rejects_unknown_traversal_and_cross_site(client):
    assert client.delete("/api/sessions/s-missing", params={"confirmed": "true"}).status_code == 404
    traversal = client.delete("/api/sessions/../vault", params={"confirmed": "true"})
    # 路径规范化后为 /api/vault。仅处理 GET 的单页应用回退会对 DELETE 返回 405，
    # 路径穿越请求始终无法到达会话存储。
    assert traversal.status_code in (404, 405, 422)
    assert (
        client.delete(
            "/api/sessions/s-missing",
            params={"confirmed": "true"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )


def test_an_unrelated_running_turn_does_not_block_archive(backend):
    store = backend._sessions
    target = store.create()
    model = GatedModel()
    backend._runtime.model = model
    app = create_app(backend, autostart=False)
    streamer = TestClient(app, base_url=BASE_URL)
    controller = TestClient(app, base_url=BASE_URL)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(collect, streamer, "first")
        wait_active(backend)
        response = controller.delete(f"/api/sessions/{target.id}", params={"confirmed": "true"})
        assert response.status_code == 200
        assert store.exists(target.id, archived=True)
        model.release.set()
        future.result(timeout=15)


@pytest.mark.parametrize("lane", ["title", "distillation", "context"])
def test_archive_interlocks_with_model_call_and_discards_archived_work(
    client, backend, monkeypatch, lane
):
    from syvrio.memory.background import context_fill, titles
    from syvrio.memory.background.jobs import enqueue_context_fill
    from syvrio.memory.inference import source_version
    from syvrio.memory.models import MemoryContext

    background, store, service = backend._background, backend._sessions, backend._service
    background.stop()
    target, other = store.create(), store.create()
    store.append(
        target,
        {"type": "message", "id": "m-user", "role": "user", "content": "Synthetic work item"},
    )
    store.append(target, {"type": "assistant", "id": "a-reply", "content": "Synthetic response"})
    entered, release = threading.Event(), threading.Event()
    calls = []

    def model(*args):
        calls.append(lane)
        entered.set()
        assert release.wait(10)
        return result

    key = target.id
    if lane == "title":
        result = "Synthetic title"
        monkeypatch.setattr(titles, "generate", model)
        background.request_title(key)
    elif lane == "distillation":
        result = distillation.Result(memories=[])
        monkeypatch.setattr(distillation, "ask", model)
        background.request_distillation(key)
    else:
        result = MemoryContext()
        fact = service.add(
            MemoryDraft(text="A synthetic fact", topic="topic"), session_id=key, message_id="m-user"
        )
        key = fact.id
        monkeypatch.setattr(context_fill, "ask", model)
        enqueue_context_fill(service.paths, key, source_version(fact))
    path = job_key(service.paths, lane, key)
    from syvrio.memory.background.jobs import save_job

    original_job = read_job(path)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(background._step_job, lane, path, threading.Event())
        try:
            assert entered.wait(5)
            denied = client.post(f"/api/sessions/{target.id}/archive")
            assert denied.status_code == 409 and lane in denied.json()["error"]["message"]
            assert client.post(f"/api/sessions/{other.id}/archive").status_code == 200
        finally:
            release.set()
        running.result(timeout=10)
    assert client.post(f"/api/sessions/{target.id}/archive").status_code == 200
    save_job(path, original_job)
    assert background._step_job(lane, path, threading.Event())
    assert not read_job(path) and calls == [lane]


def test_archive_tree_is_outside_recall_source_topic_and_background_inputs(
    client, backend, monkeypatch
):
    from pathlib import Path

    from syvrio.memory.background import dream

    store, service, background = backend._sessions, backend._service, backend._background
    background.stop()
    target = store.create()
    marker = "quartzarchiveprobe"
    store.append(target, {"type": "message", "id": "m-source", "role": "user", "content": marker})
    fact = service.add(
        MemoryDraft(text="Visible fact", topic="topic"), session_id=target.id, message_id="m-source"
    )
    assert service.read_memory_source(fact.id)["message"]["content"] == marker
    assert client.post(f"/api/sessions/{target.id}/archive").status_code == 200
    root = service.paths.vault_dir / "archived"
    moved_fact = service.add(MemoryDraft(text=marker, topic="cold-evidence"))
    assert moved_fact.id in {hit.id for hit in service.recall(marker)}
    with transaction(service.paths) as db:
        db.execute("UPDATE topics SET archived=1 WHERE name=?", (moved_fact.topic,))
    for name in ("memories/general/topic.md", "inferences/idea/i.md", "nested/future-format.jsonl"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(marker)

    open_path = Path.open

    def forbid_archive_read(path, *args, **kwargs):
        assert not path.resolve().is_relative_to(root), path
        return open_path(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbid_archive_read)
    assert all(row.id != target.id for row in backend.list_sessions().items)
    assert not store.pending_sessions()
    background._discover(threading.Event())
    assert not read_job(job_key(service.paths, "distillation", target.id))
    with pytest.raises(ValueError, match="unavailable"):
        service.read_memory_source(fact.id)
    assert [entry.id for entry in service.topic_entries("topic")] == [fact.id]
    assert all(marker not in hit.text for hit in service.recall(marker))
    assert not service.topic_entries("cold-evidence")
    assert marker not in str(dream._state(service))


def test_sidebar_orders_by_last_message_not_control_records(client, backend):
    store = backend._sessions
    older = store.create()
    store.append(
        older,
        {
            "type": "message",
            "id": "m-older",
            "role": "user",
            "content": "older message",
            "at": "2026-11-09T10:00:00+08:00",
        },
    )
    # 重命名、生成摘要、提炼或撤回都不能让旧会话排到前面。
    store.append(
        older,
        {
            "type": "title",
            "id": "t-older",
            "title": "renamed later",
            "at": "2999-01-01T00:00:00+08:00",
        },
    )
    newer = store.create()
    store.append(
        newer,
        {
            "type": "message",
            "id": "m-newer",
            "role": "user",
            "content": "newer message",
            "at": "2026-11-09T12:00:00+08:00",
        },
    )

    rows = client.get("/api/sessions").json()["items"]
    by_id = {row["id"]: row for row in rows}
    assert by_id[older.id]["updated_at"] == "2998-12-31T16:00:00+00:00"
    assert by_id[older.id]["last_message_at"] == "2026-11-09T02:00:00+00:00"
    assert by_id[newer.id]["last_message_at"] == "2026-11-09T04:00:00+00:00"
    stamps = [row["last_message_at"] for row in rows]
    assert stamps == sorted(stamps, reverse=True)
    ids = [row["id"] for row in rows if row["id"] in (older.id, newer.id)]
    assert ids == [newer.id, older.id]


def test_invalid_session_id_is_rejected(client):
    for bad in ("../vault", "s-1/../../x", "..", "%2e%2e", "a" * 200):
        response = client.get(f"/api/sessions/{bad}")
        assert response.status_code in (404, 422), bad
        assert "vault" not in response.text.lower() or response.status_code != 200
        if response.status_code == 422:
            assert response.json()["error"]["code"] == "invalid_session_id"


def test_conditional_switch_returns_history_and_reuses_it_after_leaving(
    client, backend, monkeypatch
):
    store = backend._sessions
    first, second = store.create(), store.create()
    store.append(first, {"type": "assistant", "id": "reply", "content": "First answer"})
    url = f"/api/sessions/{first.id}"
    reads, projections = [], []
    load, project = store.load, backend.project_history

    def count_read(path, *, include_reasoning=True, archived=False):
        if path == first.id and include_reasoning:
            reads.append(path)
        return load(path, include_reasoning=include_reasoning)

    def count_projection(session, **kwargs):
        if session.id == first.id:
            projections.append(session.id)
        return project(session, **kwargs)

    monkeypatch.setattr(store, "load", count_read)
    monkeypatch.setattr(backend, "project_history", count_projection)
    first_open = client.post(f"{url}/switch", params={"include_history": True}).json()
    assert first_open["history"] == client.get(url).json()
    assert first_open["history"]["items"][0]["tool_calls"] == []
    assert first_open["history"]["current"] is True
    reader = {"X-Syvrio-View": "55" * 16}
    other = client.get(url, headers=reader, params={"include_version": True})
    assert other.headers["X-Syvrio-History-Version"] == first_open["history_version"]
    assert other.json() == {**first_open["history"], "current": False}
    assert (
        "X-Syvrio-History-Version"
        not in client.get(url, params={"include_version": True, "limit": 1}).headers
    )
    # 切换、分页和上下文占用都不应装载完整的可写工作会话。
    assert client.get("/api/context-usage", params={"session_id": first.id}).json()["used"] > 0
    assert reads == [] and backend._runtime.session is None
    params = {"include_history": True, "known_version": first_open["history_version"]}
    assert client.post(f"/api/sessions/{second.id}/switch").json() == {
        "session_id": second.id,
        "title": second.title(),
    }
    # 离开会话时追加关闭标记，不影响用于显示的历史修订版本。
    assert store.load(first.id).records[-1]["type"] == "closed"
    before = (len(reads), len(projections))
    back = client.post(f"{url}/switch", params=params).json()
    assert back == {**first_open, "history": None}
    assert (len(reads), len(projections)) == before
    store.append_event_transactionally(first, {"type": "distilled", "up_to": "reply"})
    assert client.post(f"{url}/switch", params=params).json() == back


def test_browsing_context_reads_only_replayed_records_and_hydrates_before_edit(
    client, backend, monkeypatch
):
    store = backend._sessions
    first = store.create()
    store.append(first, {"type": "message", "role": "user", "id": "old", "content": "old input"})
    store.append(first, {"type": "assistant", "content": "old answer"})
    covered = len(first.records)
    store.append(
        first,
        {
            "type": "summary",
            "content": "compressed",
            "covered_records": covered,
            "distilled_ids": [],
        },
    )
    store.append(
        first, {"type": "message", "role": "user", "id": "recent", "content": "recent input"}
    )
    expected = backend._conversation.estimate_usage(first)
    original = store.context_history
    captured = []

    def observe(*args, **kwargs):
        summary, history, active = original(*args, **kwargs)
        captured.append(([item["id"] for item in history], summary))
        return summary, history, active

    monkeypatch.setattr(store, "context_history", observe)
    assert client.post(
        f"/api/sessions/{first.id}/switch", params={"include_history": True}
    ).is_success
    for _ in range(2):
        assert (
            client.get("/api/context-usage", params={"session_id": first.id}).json()["used"]
            == expected
        )
    assert captured == [(["recent"], "compressed")]
    assert backend._runtime.session is None and backend._conversation.current_id == first.id
    # 回滚版本号会被下一笔提交复用，事务内估算不能污染共享缓存。
    with pytest.raises(ValueError, match="rollback"), transaction(store.paths):
        store.append(first, {"type": "message", "role": "user", "content": "discarded" * 300})
        transient = backend._conversation.estimate_saved_usage(first.id)
        assert transient > expected
        raise ValueError("rollback")
    assert backend._conversation.estimate_saved_usage(first.id) == expected
    remote = {"type": "message", "role": "user", "id": "outside", "content": "hello"}
    store.append_event_transactionally(first, remote)
    committed = client.get("/api/context-usage", params={"session_id": first.id}).json()["used"]
    assert expected < committed < transient
    assert captured[-1] == (["recent", "outside"], "compressed")
    # 第一次编辑必须以完整会话为基础，保留摘要前的事实与消息。
    assert (
        client.put(f"/api/sessions/{first.id}/title", json={"title": "edited"}).status_code == 204
    )
    assert backend._runtime.current.id == first.id
    assert [item["id"] for item in backend._runtime.current.messages] == [
        "old",
        "recent",
        "outside",
    ]


def test_conditional_switch_uses_committed_history_even_while_bound(client, backend):
    store = backend._sessions
    session = store.create()
    store.append(
        session, {"type": "message", "role": "user", "id": "original", "content": "Original"}
    )
    store.append(
        session,
        {"type": "assistant", "id": "answer", "content": "Answer", "reasoning": {"text": "Before"}},
    )
    url = f"/api/sessions/{session.id}"
    params = {"include_history": True}
    initial = client.post(f"{url}/switch", params=params).json()
    params["known_version"] = initial["history_version"]
    with transaction(store.paths) as db:
        db.execute(
            "UPDATE session_events SET reasoning=? WHERE session_id=? AND message_id='answer'",
            ('{"text":"After"}', session.id),
        )
    changed = client.post(f"{url}/switch", params=params).json()
    assert changed["history_version"] != initial["history_version"]
    assert changed["history"] == client.get(url).json()
    assert changed["history"]["items"][-1]["reasoning"]["text"] == "After"

    # 另一个进程提交撤回和替换操作，未改动常驻服务的 Runtime 对象。
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from pathlib import Path
from syvrio.infra.storage.paths import SyvrioPaths
from syvrio.infra.session import SessionStore
vault, runtime, sid = sys.argv[1:]
store = SessionStore(SyvrioPaths(Path(vault), Path(runtime)))
store.append_event_transactionally(store.load(sid), {"type":"undo","message_id":"original"},
                                  {"type":"message","role":"user","id":"new","content":"Edited"})
""",
            str(store.paths.vault_dir),
            str(store.paths.runtime_dir),
            session.id,
        ],
        check=True,
        timeout=10,
    )
    params["known_version"] = changed["history_version"]
    edited = client.post(f"{url}/switch", params=params).json()
    assert edited["history_version"] != changed["history_version"]
    assert edited["history"] == client.get(url).json()
    assert [item["content"] for item in edited["history"]["items"]] == ["Edited"]
    # 从上一次快照到关闭事件之间的真实写入，必须在关闭快捷路径中得到保留。
    params["known_version"] = edited["history_version"]
    store.rename(session, "New title")
    other = store.create()
    assert client.post(f"/api/sessions/{other.id}/switch").is_success
    renamed = client.post(f"{url}/switch", params=params).json()
    assert renamed["history_version"] != edited["history_version"]
    assert renamed["history"]["title"] == renamed["title"] == "New title"


def test_history_restores_reasoning_and_content(client, backend):
    backend._runtime.model = FakeModel(
        [
            AssistantTurn(
                content="Hello 世界",
                reasoning_content="先想一步\n再想一步",
                reasoning_source="local",
            ),
        ]
    )
    events = collect(client, "hi")
    assert "done" in types(events)
    session_id = next(event["session_id"] for event in events if event.get("session_id"))
    history = client.get(f"/api/sessions/{session_id}").json()
    assistant = next(item for item in history["items"] if item["role"] == "assistant")
    assert assistant["content"] == "Hello 世界"
    assert assistant["reasoning"]["text"] == "先想一步\n再想一步"


@pytest.mark.parametrize("order", ["history-first", "switch-first", "concurrent"])
def test_history_and_switch_avoid_hydrating_runtime_across_views(
    client, backend, monkeypatch, order
):
    store = backend._sessions
    session = store.create()
    store.append(
        session,
        {
            "type": "assistant",
            "id": "answer",
            "content": "Reply",
            "reasoning": {"text": "Initial reasoning"},
        },
    )
    reads = []
    original = store.load

    def count(path, *, include_reasoning=True, archived=False):
        if include_reasoning and path == session.id:
            reads.append(path)
        return original(path, include_reasoning=include_reasoning)

    monkeypatch.setattr(store, "load", count)
    url = f"/api/sessions/{session.id}"
    history_view = {"X-Syvrio-View": "11" * 16}
    chat_view = {"X-Syvrio-View": "22" * 16}

    def history():
        response = client.get(url, headers=history_view)
        assert response.status_code == 200
        return response.json()["items"]

    def switch():
        assert client.post(f"{url}/switch", headers=chat_view).status_code == 200

    if order == "concurrent":
        with ThreadPoolExecutor(2) as pool:
            opened = pool.submit(switch)
            assert pool.submit(history).result()[0]["content"] == "Reply"
            opened.result()
    elif order == "history-first":
        history()
        switch()
    else:
        switch()
        history()
    assert reads == []
    with transaction(store.paths) as db:
        db.execute(
            "UPDATE session_events SET reasoning=? WHERE session_id=? AND message_id='answer'",
            ('{"text":"Changed reasoning"}', session.id),
        )
    assert history()[0]["reasoning"]["text"] == "Changed reasoning"
    store.append(
        session, {"type": "message", "role": "user", "id": "followup", "content": "Follow-up"}
    )
    assert history()[-1]["id"] == "followup"
    store.append_event_transactionally(session, {"type": "undo", "message_id": "followup"})
    assert [item["id"] for item in history()] == ["answer"]


# 流式传输


def test_stream_events_are_ordered_and_utf8(client, backend):
    backend._runtime.model = FakeModel(
        [
            AssistantTurn(content="你好，世界 🌍", reasoning_content="思考中"),
        ]
    )
    events = collect(client, "hi")
    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "done"
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert "reasoning_delta" in types(events)
    assert "reasoning_end" in types(events)
    body = "".join(event["text"] for event in events if event["type"] == "content_delta")
    assert body == "你好，世界 🌍"
    assert events[-1]["text"] == "你好，世界 🌍"
    assert events[-1]["session_id"]


def test_thinking_folds_when_answer_starts(client, backend):
    backend._runtime.model = FakeModel(
        [
            AssistantTurn(content="answer", reasoning_content="thinking text"),
        ]
    )
    events = collect(client, "hi")
    order = types(events)
    # 运行时依次发送思考增量、正文增量和最终思考记录。
    # 前端收到首个 content_delta 时折叠思考区。
    assert order.index("reasoning_delta") < order.index("content_delta")
    assert order.index("content_delta") < order.index("reasoning_end")
    assert order[-1] == "done"


def test_tool_call_rounds_keep_reasoning_phases_apart(client, backend):
    backend._runtime.model = FakeModel(
        [
            AssistantTurn(
                content="",
                reasoning_content="first round",
                tool_calls=[ToolCall(id="call-1", name="recall", arguments={"query": "note"})],
            ),
            AssistantTurn(content="final answer", reasoning_content="second round"),
        ]
    )
    events = collect(client, "check my note")
    assert "tool_start" in types(events)
    assert "tool_end" in types(events)
    # 事件一并携带工具结果，以便客户端立即标记失败，无需等到重载历史。
    tool_end = next(event for event in events if event["type"] == "tool_end")
    assert tool_end["name"] == "recall"
    assert tool_end["ok"] is True
    tool_index = types(events).index("tool_start")
    phases = [event["phase"] for event in events if event["type"] == "reasoning_delta"]
    assert len(phases) == 2 and phases[1] > phases[0]
    assert types(events).index("reasoning_delta") < tool_index
    assert any(
        event["type"] == "reasoning_delta" and event["phase"] == phases[1]
        for event in events[tool_index:]
    )
    assert events[-1]["type"] == "done"
    assert events[-1]["text"] == "final answer"


@pytest.mark.parametrize("wrap_up", [False, True])
def test_reasoning_without_a_final_answer_reports_failure_and_keeps_history(
    client, backend, wrap_up
):
    backend._runtime.config.max_tool_steps = 1
    intermediate = AssistantTurn(
        reasoning_content="tool reasoning",
        tool_calls=[ToolCall(id="lookup", name="recall", arguments={"query": "note"})],
    )
    final = AssistantTurn(content=" ", reasoning_content="reasoning without an answer")
    backend._runtime.model = FakeModel([intermediate, final] if wrap_up else [final])
    events = collect(client, "inspect this")
    assert events[-1]["type"] == "failed" and "no visible answer" in events[-1]["message"]
    assert "done" not in types(events)
    assert ("tool_end" in types(events)) == wrap_up
    sid = events[-1]["session_id"]
    history = client.get(f"/api/sessions/{sid}").json()["items"]
    last = [item for item in history if item["role"] == "assistant"][-1]
    assert last["reasoning"]["text"] == final.reasoning_content
    assert not last["content"].strip()
    backend._runtime.model = FakeModel([AssistantTurn(content="visible answer")])
    assert collect(client, "try again", sid)[-1]["text"] == "visible answer"


def test_tool_failure_is_reported_on_the_stream(client, backend):
    backend._runtime.model = FakeModel(
        [
            AssistantTurn(
                content="",
                reasoning_content="",
                tool_calls=[ToolCall(id="call-1", name="no_such_tool", arguments={})],
            ),
            AssistantTurn(content="done"),
        ]
    )
    events = collect(client, "try something odd")
    ends = [event for event in events if event["type"] == "tool_end"]
    assert ends, "a failed call must still close its tool row"
    assert ends[0]["name"] == "no_such_tool"
    assert ends[0]["ok"] is False


def test_failed_turn_reports_error_and_recovers(client, backend):
    backend._runtime.model = FakeModel([ChatModelError("model unavailable")])
    events = collect(client, "hi")
    assert events[-1]["type"] == "failed"
    assert "model unavailable" in events[-1]["message"]
    assert client.get("/api/health").json()["busy"] is False

    backend._runtime.model = FakeModel([AssistantTurn(content="recovered")])
    assert collect(client, "again", events[-1]["session_id"])[-1]["type"] == "done"


def test_partial_reply_and_failure_reason_survive_refresh_and_follow_turn_undo(client, backend):
    class Interrupted(FakeModel):
        def chat(self, messages, tools, on_delta=None, cancel=None, *, on_reasoning=None):
            on_delta("A partial answer")
            raise ChatModelError("synthetic upstream timeout")

    backend._runtime.model = Interrupted([])
    terminal = collect(client, "synthetic history check")[-1]
    assert terminal["type"] == "failed"
    sid = terminal["session_id"]
    history = client.get(f"/api/sessions/{sid}", headers={"X-Syvrio-View": uuid4().hex}).json()[
        "items"
    ]
    assert [row["role"] for row in history] == ["user", "assistant", "error"]
    assert "A partial answer" in history[1]["content"] and history[1]["interrupted"]
    assert history[2]["content"] == "synthetic upstream timeout"
    loaded = backend._sessions.load(sid)
    assert all(record["type"] != "turn_failed" for record in loaded.records_after_summary())
    assert [
        record["message_id"] for record in loaded.records if record["type"] == "turn_failed"
    ] == [history[0]["id"]]
    undone = client.post("/api/commands", json={"line": "/undo", "session_id": sid}).json()
    assert undone["status"] == "ok"
    assert client.get(f"/api/sessions/{sid}").json()["items"] == []


# 取消、先进先出队列与断连


def test_cancel_only_the_active_turn(backend):
    model = GatedModel()
    backend._runtime.model = model
    app = create_app(backend, autostart=False)
    streamer = TestClient(app, base_url=BASE_URL)
    controller = TestClient(app, base_url=BASE_URL)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(collect, streamer, "long answer")
        turn_id = wait_active(backend)
        response = controller.post("/api/chat/cancel", json={"turn_id": turn_id})
        assert response.status_code == 200
        assert response.json()["cancelled"] is True
        # 过期的回合 ID 不能取消任何任务。
        assert (
            controller.post("/api/chat/cancel", json={"turn_id": turn_id + 999}).json()["cancelled"]
            is False
        )
        model.release.set()
        events = future.result(timeout=15)
    assert any(event["type"] == "cancelled" for event in events)
    assert backend.active_turn_id() is None
    assert client_health_idle(app)


def client_health_idle(app):
    probe = TestClient(app, base_url=BASE_URL)
    return probe.get("/api/health").json()["busy"] is False


def test_second_chat_request_gets_busy_error(backend):
    model = GatedModel()
    backend._runtime.model = model
    app = create_app(backend, autostart=False)
    streamer = TestClient(app, base_url=BASE_URL)
    controller = TestClient(app, base_url=BASE_URL)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(collect, streamer, "first")
        wait_active(backend)
        response = controller.post("/api/chat", json={"message": "second", "session_id": None})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "busy"
        model.release.set()
        assert future.result(timeout=15)[-1]["type"] == "done"
    assert backend.active_turn_id() is None


@pytest.mark.parametrize("explicit_stop", [False, True])
def test_disconnect_keeps_turn_running_until_done_or_explicit_stop(backend, explicit_stop):
    model = GatedModel()
    backend._runtime.model = model

    async def scenario():
        turn = backend.start_turn("hi", None)
        generator = backend.stream(turn)
        await generator.__anext__()
        await generator.aclose()
        try:
            assert backend.active_turn_id() == turn.turn_id
            with pytest.raises(WebError) as busy:
                backend.start_turn("second", None)
            assert busy.value.code == "busy"
            if explicit_stop:
                assert backend.cancel(turn.turn_id)
        finally:
            model.release.set()
        assert await asyncio.to_thread(turn.finished.wait, 10)
        await asyncio.sleep(0)
        replay = [frame async for frame in backend.stream(turn)]
        assert any(
            ("event: cancelled" if explicit_stop else "event: done") in frame for frame in replay
        )

    asyncio.run(scenario())
    assert backend.active_turn_id() is None
    session_id = backend._conversation.current_id
    assert session_id is not None
    history = backend.history(session_id)
    (reply,) = [item for item in history.items if item.role == "assistant"]
    assert reply.interrupted is explicit_stop
    assert ("generation stopped by user" in reply.content) is explicit_stop
    if not explicit_stop:
        assert reply.content == "partial done"


def test_reselecting_active_session_preserves_the_turn(backend):
    model = GatedModel()
    backend._runtime.model = model
    app = create_app(backend, autostart=False)
    streamer = TestClient(app, base_url=BASE_URL)
    controller = TestClient(app, base_url=BASE_URL)
    created = backend._sessions.create().id
    controller.post(f"/api/sessions/{created}/switch")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(collect, streamer, "first", created)
        wait_active(backend)
        response = controller.post(f"/api/sessions/{created}/switch")
        assert response.status_code == 200
        assert response.json()["session_id"] == created
        assert backend.active_turn_id() is not None
        model.release.set()
        future.result(timeout=15)


# 安全检查


def test_host_and_origin_are_checked(client):
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 403
    assert (
        client.post("/api/sessions", headers={"Origin": "https://evil.example"}).status_code == 403
    )
    assert client.post("/api/sessions", headers={"Origin": BASE_URL}).status_code == 201
    # 代理请求头不能扩大信任范围。
    assert (
        client.get("/api/health", headers={"X-Forwarded-Host": "evil.example"}).status_code == 200
    )


def test_custom_local_domain_is_trusted_for_host_and_origin(backend):
    app = create_app(backend, autostart=False, allowed_hosts=["syvrio.local"])
    probe = TestClient(app, base_url="http://syvrio.local:8765")
    assert probe.get("/api/health").status_code == 200
    assert (
        probe.post("/api/sessions", headers={"Origin": "http://syvrio.local:8765"}).status_code
        == 201
    )
    assert (
        probe.post("/api/sessions", headers={"Origin": "https://evil.example"}).status_code == 403
    )
    assert probe.get("/api/health", headers={"Host": "evil.example"}).status_code == 403


def test_consistent_error_shape_and_no_stacktrace(backend, monkeypatch):
    # raise_server_exceptions=False 对应 Uvicorn 的实际行为，
    # ServerErrorMiddleware 返回应用的 JSON 响应，服务器继续运行。
    probe = TestClient(
        create_app(backend, autostart=False),
        base_url=BASE_URL,
        raise_server_exceptions=False,
    )
    response = probe.get("/api/sessions/no-such-session")
    assert response.status_code == 404
    assert set(response.json()) == {"error"}
    assert set(response.json()["error"]) == {"code", "message"}

    def explode(*args, **kwargs):
        raise RuntimeError("SECRET INTERNAL DETAIL")

    monkeypatch.setattr(backend, "list_sessions", explode)
    failed = probe.get("/api/sessions")
    assert failed.status_code == 500
    assert "SECRET" not in failed.text
    assert "Traceback" not in failed.text


def test_invalid_payload_and_empty_message(client):
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
    assert client.post("/api/chat", json={}).status_code == 422
    assert client.post("/api/chat/cancel", json={"turn_id": "x"}).status_code == 422


def test_client_busy_is_reported(backend, monkeypatch):
    # 打开子视图和未获得租约的服务都不能执行启动恢复。
    monkeypatch.setattr(
        type(backend._sessions), "recover_deletions", lambda _: pytest.fail("unexpected recovery")
    )
    type(backend)(parent=backend)

    def denied(*args, **kwargs):
        raise LockTimeout("occupied")

    monkeypatch.setattr(backend._client_lock, "acquire", denied)
    with pytest.raises(WebError) as error:
        WebBackend(service=backend._service).startup()
    assert error.value.code == "client_busy"


# 静态资源


def test_missing_assets_are_reported_explicitly(backend, tmp_path, monkeypatch):
    # 指向空目录，避免本地构建的 static/ 资源影响测试结果。
    monkeypatch.delenv("SYVRIO_WEB_STATIC", raising=False)
    app = create_app(backend, autostart=False, static_dir=tmp_path / "no-assets")
    probe = TestClient(app, base_url=BASE_URL)
    assert probe.get("/api/health").json()["static_ready"] is False
    response = probe.get("/")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "assets_missing"


def test_static_files_and_spa_fallback(backend, tmp_path):
    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>syvrio</html>", encoding="utf-8")
    (static_dir / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    app = create_app(backend, autostart=False, static_dir=static_dir)
    probe = TestClient(app, base_url=BASE_URL)
    assert probe.get("/api/health").json()["static_ready"] is True
    assert "syvrio" in probe.get("/").text
    assert "console.log" in probe.get("/assets/app.js").text
    assert probe.get("/some/client/route").status_code == 200
    assert probe.get("/api/unknown").status_code == 404


# 命令并发


def test_long_command_keeps_health_and_dream_status_responsive(backend, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    backend._background.stop()
    real_execute = backend._commands.execute

    def slow_execute(line, *, busy=False, confirmed=False):
        if line.strip() == "/dream run":
            entered.set()
            assert release.wait(timeout=10), "command was never released"
        return real_execute(line, busy=busy, confirmed=confirmed)

    monkeypatch.setattr(backend._commands, "execute", slow_execute)
    app = create_app(backend, autostart=False)
    command_client = TestClient(app, base_url=BASE_URL)
    controller = TestClient(app, base_url=BASE_URL)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            lambda: command_client.post(
                "/api/commands", json={"line": "/dream run", "confirmed": True}
            )
        )
        assert entered.wait(timeout=10)
        # 健康检查反映正在执行的命令，且不会阻塞在该命令的锁上。
        assert controller.get("/api/health").json()["busy"] is True
        # 命令运行期间，聊天请求应收到忙碌响应，避免并发执行。
        chat = controller.post("/api/chat", json={"message": "hi"})
        assert chat.status_code == 409
        assert chat.json()["error"]["code"] == "busy"
        # 手工提交占用命令互斥锁时，仍可读取进度。
        status = controller.post("/api/commands", json={"line": "/dream status"})
        assert status.status_code == 200 and status.json()["data"]["state"] == "idle"
        release.set()
        assert future.result(timeout=10).json()["data"]["state"] == "queued"
    assert backend.scheduling.activity() == (False, None)
    assert backend.active_turn_id() is None


def test_chat_rejects_a_stale_session_id(client, backend):
    current = backend._sessions.create().id
    client.post(f"/api/sessions/{current}/switch")
    other = backend._sessions.create().id
    response = client.post("/api/chat", json={"message": "hi", "session_id": other})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_mismatch"
    assert backend.active_turn_id() is None


class GatedExtractor:
    def __init__(self, *, fail=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.fail = fail

    def ask(self, service, prompt, payload, schema):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=10)
        if self.fail:
            raise RuntimeError("synthetic extraction failure")
        return distillation.Result(memories=[])


def seed_distillation(backend):
    session = backend._runtime.ensure_session()
    for index in range(3):
        backend._sessions.append(
            session,
            {"type": "message", "role": "user", "id": f"m-seed-{index}", "content": "synthetic"},
        )
    backend._runtime.config.extract_on_exit = True
    return session


def wait_until(check):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    assert check()


def wait_for_distillation(backend, session):
    path = job_key(backend._service.paths, "distillation", session.id)
    wait_until(lambda: not read_job(path))


@pytest.mark.parametrize("outcome", ["generated", "renamed", "undone", "failure"])
def test_background_title_and_manual_rename_across_views(
    client, backend, monkeypatch, isolate_llm_credentials, outcome
):
    from uuid import uuid4

    from syvrio.memory.background import titles

    entered, release, finished = (threading.Event() for _ in range(3))
    original_process = titles.process
    seen = []
    generated, manual = "道路结冰原因", "完整标题" * 19 + "保留结尾"
    question, answer = "道路为什么会结冰？", "路面温度低于冰点时，水会凝固。"

    def ask(service, prompt, payload, schema, *, api_key):
        assert api_key == "synthetic"
        seen.append(payload)
        entered.set()
        assert release.wait(10)
        if outcome == "failure":
            raise ValueError("Invalid title response")
        return schema(title=generated)

    def run_job(*args):
        try:
            return original_process(*args)
        finally:
            finished.set()

    isolate_llm_credentials[backend._service.config.llm.api_key_env] = "synthetic"
    monkeypatch.setattr(titles, "ask", ask)
    monkeypatch.setattr(titles, "process", run_job)
    backend._runtime.model = FakeModel([AssistantTurn(content=answer, reasoning_content="private")])
    try:
        events = collect(client, question)
        assert events[-1]["type"] == "done" and entered.wait(5)
        sid = events[-1]["session_id"]
        other = {"X-Syvrio-View": str(uuid4())}
        row = client.get("/api/sessions", headers=other).json()["items"][0]
        assert row["id"] == sid and row["title"] == "未命名会话" and row["title_pending"]
        if outcome == "renamed":
            result = client.put(f"/api/sessions/{sid}/title", json={"title": manual}, headers=other)
            assert result.status_code == 204
            assert not client.get("/api/sessions").json()["items"][0]["title_pending"]
        elif outcome == "undone":
            result = client.post("/api/commands", json={"line": "/undo", "session_id": sid})
            assert result.json()["status"] == "ok"
    finally:
        release.set()
    assert finished.wait(5)
    expected = {"generated": generated, "renamed": manual}.get(outcome, "未命名会话")
    row = client.get("/api/sessions").json()["items"][0]
    assert row["title"] == expected and not row["title_pending"]
    assert backend._runtime.current.title() == backend._sessions.load(sid).title() == expected
    assert seen == [{"question": question, "answer": answer}]


@pytest.mark.parametrize("entry", ["create", "switch", "command"])
def test_navigation_and_chat_finish_while_distillation_waits(client, backend, entry, monkeypatch):
    session = seed_distillation(backend)
    target = backend._sessions.create()
    extractor = GatedExtractor()
    monkeypatch.setattr(distillation, "ask", extractor.ask)
    try:
        if entry == "create":
            assert client.post("/api/sessions").status_code == 201
        elif entry == "switch":
            assert client.post(f"/api/sessions/{target.id}/switch").status_code == 200
        else:
            response = client.post("/api/commands", json={"line": "/new", "session_id": session.id})
            assert response.json()["intent"] == "new_session"
        assert extractor.entered.wait(timeout=3)
        assert client.get("/api/health").json()["busy"] is False
        assert client.post(f"/api/sessions/{target.id}/switch").status_code == 200
        assert client.post("/api/sessions").status_code == 201
        assert "done" in types(collect(client, "synthetic next conversation"))
        assert extractor.calls == 1
    finally:
        extractor.release.set()
    wait_for_distillation(backend, session)
    loaded = backend._sessions.load(session.id)
    assert loaded.user_texts_after_distilled() == []
    assert [row["up_to"] for row in loaded.records if row["type"] == "distilled"] == [
        "m-seed-0",
        "m-seed-1",
        "m-seed-2",
    ]
    assert extractor.calls >= 3


def test_background_does_not_publish_stale_snapshot_after_resuming(client, backend, monkeypatch):
    session = seed_distillation(backend)
    extractor = GatedExtractor()
    monkeypatch.setattr(distillation, "ask", extractor.ask)
    try:
        assert client.post("/api/sessions").status_code == 201
        assert extractor.entered.wait(timeout=3)
        assert client.post(f"/api/sessions/{session.id}/switch").status_code == 200
        assert "done" in types(collect(client, "new synthetic message", session.id))
    finally:
        extractor.release.set()
    wait_for_distillation(backend, session)
    assert [
        row["content"] for row in backend._sessions.load(session.id).undistilled_messages()
    ] == ["new synthetic message"]
    assert client.post("/api/sessions").status_code == 201
    wait_for_distillation(backend, session)
    loaded = backend._sessions.load(session.id)
    assert not loaded.undistilled_messages()
    assert [row["up_to"] for row in loaded.records if row["type"] == "distilled"] == [
        "m-seed-0",
        "m-seed-1",
        "m-seed-2",
        loaded.last_user_message_id(),
    ]


def test_background_failure_leaves_a_retryable_job(client, backend, monkeypatch):
    session = seed_distillation(backend)
    extractor = GatedExtractor(fail=True)
    monkeypatch.setattr(distillation, "ask", extractor.ask)
    extractor.release.set()
    assert client.post("/api/sessions").status_code == 201
    path = job_key(backend._service.paths, "distillation", session.id)
    wait_until(lambda: (read_job(path) or {}).get("failures"))
    assert len(backend._sessions.load(session.id).undistilled_messages()) == 3
    extractor.fail = False
    wait_for_distillation(backend, session)
    assert not backend._sessions.load(session.id).undistilled_messages()


def test_shutdown_stops_background_publication_and_keeps_client_lock(client, backend, monkeypatch):
    session = seed_distillation(backend)
    extractor = GatedExtractor()
    monkeypatch.setattr(distillation, "ask", extractor.ask)
    assert client.post("/api/sessions").status_code == 201
    try:
        assert extractor.entered.wait(timeout=3)

        stop = backend._background._stop

        def release_on_shutdown():
            assert stop.wait(timeout=5)
            assert backend._client_lock.runtime_lock.locked
            assert client.post("/api/sessions").status_code == 503
            extractor.release.set()

        with ThreadPoolExecutor() as pool:
            future = pool.submit(release_on_shutdown)
            backend.shutdown()
            future.result(timeout=5)
    finally:
        extractor.release.set()
    assert not backend._client_lock.runtime_lock.locked
    assert len(backend._sessions.load(session.id).undistilled_messages()) == 3


def test_chat_rejects_stale_empty_view(client, backend):
    session = backend._sessions.create()
    assert client.post(f"/api/sessions/{session.id}/switch").status_code == 200
    response = client.post("/api/chat", json={"message": "synthetic", "session_id": None})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_mismatch"
    assert not backend._sessions.load(session.id).messages


def test_new_backend_resumes_jobs_after_shutdown(backend, monkeypatch):
    session = seed_distillation(backend)
    extractor = GatedExtractor()
    monkeypatch.setattr(distillation, "ask", extractor.ask)
    backend._conversation.switch(None)
    assert extractor.entered.wait(5)
    stop = backend._background._stop

    def release_after_stop():
        assert stop.wait(5)
        extractor.release.set()

    with ThreadPoolExecutor() as pool:
        releasing = pool.submit(release_after_stop)
        backend.shutdown()
        releasing.result(5)
    assert len(backend._sessions.load(session.id).undistilled_messages()) == 3
    successor = WebBackend(service=backend._service)
    try:
        successor.startup()
        wait_for_distillation(successor, session)
        assert not successor._sessions.load(session.id).undistilled_messages()
    finally:
        successor.shutdown()


def test_empty_view_accepts_first_message_after_new_and_refresh(client, backend):
    previous = collect(client, "synthetic first")[-1]["session_id"]
    assert previous
    response = client.post("/api/commands", json={"line": "/new", "session_id": previous})
    assert response.json()["status"] == "ok"
    assert not any(row["current"] for row in client.get("/api/sessions").json()["items"])
    fresh = collect(client, "synthetic after refresh", None)[-1]["session_id"]
    assert fresh and fresh != previous
    assert [row["content"] for row in backend._sessions.load(fresh).messages] == [
        "synthetic after refresh"
    ]


def test_targeted_edit_checks_uploads_and_replays_only_same_submission(client, backend):
    events = collect(client, "original")
    session = backend._conversation.current_id
    original = next(event["message_id"] for event in events if event["type"] == "message_saved")
    path = f"/api/chat/attachments?receipt=1&replace={original}"
    fields = {"message": "replacement", "session_id": session}

    def submit(contents=b"first"):
        return client.post(path, data=fields, files={"files": ("note.txt", contents)})

    invalid = client.post(path, data=fields, files={"files": ("bad.png", b"not an image")})
    assert invalid.status_code == 422
    slash = client.post(
        f"/api/chat?replace={original}", json={"session_id": session, "message": "/new"}
    )
    assert slash.status_code == 422
    assert backend._conversation.current.user_texts() == ["original"]

    response = submit()
    assert response.status_code == 202, response.text
    receipt = response.json()
    assert client.get(f"/api/chat/turns/{receipt['turn_id']}").status_code == 200
    # 回执丢失后，即使用重新暂存的文件重试也应返回原有回合。
    assert submit().json()["turn_id"] == receipt["turn_id"]
    assert submit(b"changed bytes").status_code == 409
    assert backend._runtime.model.calls == 2
    assert backend._conversation.current.user_texts() == ["replacement"]
    assert not list((backend._service.paths.attachments_dir / ".incoming").glob("*"))


def test_retry_edit_after_worker_launch_failure_cannot_roll_back_another_message(
    client, backend, monkeypatch
):
    def target(text):
        events = collect(client, text, backend._conversation.current_id)
        return next(event["message_id"] for event in events if event["type"] == "message_saved")

    def fail_start(*args):
        raise RuntimeError("worker unavailable")

    for intervening_turn in (False, True):
        original = target("original")
        session = backend._conversation.current_id
        path = f"/api/chat?replace={original}&receipt=1"
        body = {"message": "replacement", "session_id": session}
        history = f"/api/sessions/{session}"
        before = client.get(history).json()["items"]
        with monkeypatch.context() as patch:
            patch.setattr(backend.scheduling, "_launch", fail_start)
            failed = client.post(path, json=body)
            assert failed.status_code == 503
            assert failed.json()["error"]["code"] == "edit_not_sent"
        assert client.get(history).json()["items"] == before
        if intervening_turn:
            target("another turn")
            assert client.post(path, json=body).status_code == 409
            assert backend._conversation.current.user_texts()[-1] == "another turn"
        else:
            accepted = client.post(path, json=body)
            assert accepted.status_code == 202, accepted.text
            client.get(f"/api/chat/turns/{accepted.json()['turn_id']}")
            assert backend._conversation.current.user_texts() == ["replacement"]


@pytest.mark.parametrize("worker_fails", [False, True])
def test_edit_of_active_turn_preserves_its_partial_reply_and_queue_until_accepted(
    client, backend, monkeypatch, worker_fails
):
    model = backend._runtime.model = GatedModel()
    original = client.post("/api/chat?receipt=1", json={"message": "original"}).json()
    session = original["session_id"]
    assert model.entered.wait(5)
    target = backend._conversation.ask_user_message_id
    assert target
    waiting = client.post(
        "/api/chat?receipt=1", json={"message": "waiting", "session_id": session}
    ).json()
    body = {"message": "replacement", "session_id": session}
    path = f"/api/chat?receipt=1&replace={target}"
    start = threading.Thread.start

    def fail_worker(thread):
        if thread.name.startswith("syvrio-turn-"):
            raise RuntimeError("synthetic worker startup failure")
        return start(thread)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
        if worker_fails:
            patch.setattr(threading.Thread, "start", fail_worker)
        replace = pool.submit(client.post, path, json=body)
        try:
            assert backend._conversation.cancel_event.wait(5)
            competing = client.post(
                "/api/chat?receipt=1", json={"message": "interloper", "session_id": session}
            )
            assert competing.status_code == 409
            assert client.get("/api/health").status_code == 200
        finally:
            model.release.set()
        result = replace.result(timeout=5)
    if worker_fails:
        assert result.status_code == 503 and result.json()["error"]["code"] == "edit_not_sent"
        retained = client.get(f"/api/sessions/{session}").json()["items"]
        assert [item["content"] for item in retained if item["role"] == "user"] == ["original"]
        assert retained[-1]["interrupted"] and "partial" in retained[-1]["content"]
        queue = client.get("/api/chat/queue").json()
        assert queue["active"] is None and queue["paused"]
        assert session not in queue["replying_session_ids"]
        assert [item["turn_id"] for item in queue["waiting"]] == [waiting["turn_id"]]
        result = client.post(path, json=body)
    assert result.status_code == 202, result.text
    assert client.get(f"/api/chat/turns/{waiting['turn_id']}").status_code == 200
    assert backend._conversation.current.user_texts() == ["replacement", "waiting"]


def test_targeted_edit_from_another_view_and_after_receipt_eviction_is_stale(client, backend):
    events = collect(client, "original")
    session = backend._conversation.current_id
    target = next(event["message_id"] for event in events if event["type"] == "message_saved")
    path = f"/api/chat?receipt=1&replace={target}"
    body = {"message": "replacement", "session_id": session}
    headers = {"X-Syvrio-View": "9c6a32bb-7d49-42d9-8652-68c11289f008"}
    assert client.post(f"/api/sessions/{session}/switch", headers=headers).status_code == 200
    receipt = client.post(path, json=body).json()
    client.get(f"/api/chat/turns/{receipt['turn_id']}")
    assert client.post(path, json=body, headers=headers).status_code == 409
    for index in range(10):
        collect(client, f"later {index}", session)
    assert client.post(path, json=body).status_code == 409
    assert backend._runtime.model.calls == 12
    assert backend._conversation.current.user_texts() == [
        "replacement",
        *(f"later {i}" for i in range(10)),
    ]


@pytest.mark.parametrize("failure", ["bind", "write", "after_write"])
def test_edit_waits_for_the_user_record_before_returning_a_receipt(
    client, backend, monkeypatch, failure
):
    from syvrio.infra.session import attachments

    first = client.post(
        "/api/chat/attachments?receipt=1",
        data={"message": "original"},
        files={"files": ("original.txt", b"original file")},
    ).json()
    assert client.get(f"/api/chat/turns/{first['turn_id']}").status_code == 200
    session = backend._conversation.current_id
    history = f"/api/sessions/{session}"
    before = client.get(history).json()["items"]
    target = before[0]["id"]
    download = f"{history}/messages/{target}/attachments/{before[0]['attachments'][0]['id']}"
    path = f"/api/chat/attachments?receipt=1&replace={target}"
    fields = {"message": "replacement", "session_id": session}
    files = {"files": ("note.txt", b"retain me")}
    store = backend._sessions
    original = store.load(session).records
    append = store._insert

    def fail_write(db, sid, record):
        if sid == session and record.get("content") == "replacement":
            if failure == "after_write":
                append(db, sid, record)
            raise OSError("synthetic storage failure")
        return append(db, sid, record)

    def fail_bind(*args):
        raise OSError("synthetic attachment failure")

    with monkeypatch.context() as patch:
        if failure == "bind":
            patch.setattr(attachments, "publish", fail_bind)
        else:
            patch.setattr(store, "_insert", fail_write)
        response = client.post(path, data=fields, files=files)
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "edit_not_sent"
    assert client.get(history).json()["items"] == before
    assert store.load(session).records == original
    assert backend._conversation.current.user_texts() == ["original"]
    assert client.get(download).content == b"original file"
    assert len(rows(backend._service.paths, "attachments")) == 1
    assert not list((backend._service.paths.attachments_dir / ".incoming").iterdir())
    response = client.post(path, data=fields, files=files)
    assert response.status_code == 202, response.text
    assert response.json()["message_id"]
    assert backend._conversation.current.user_texts() == ["replacement"]
    assert not list((backend._service.paths.attachments_dir / ".incoming").glob("*"))


@pytest.mark.parametrize("foreign_write", [False, True])
def test_failed_edit_preserves_the_original_tool_writes(
    client, backend, monkeypatch, foreign_write
):

    service = backend._service
    args = {"text": "合成书签使用蓝色", "topic": "synthetic-bookmarks"}
    model = backend._runtime.model = FakeModel(
        [
            AssistantTurn(tool_calls=[ToolCall("review", "memorize", args)]),
            decision_turn("apply"),
            AssistantTurn(content="original reply"),
        ]
    )
    events = collect(client, "synthetic bookmarks")
    sid = events[-1]["session_id"]
    target = next(event["message_id"] for event in events if event["type"] == "message_saved")
    affected = service.store.entries(args["topic"])[0]
    assert affected.text == args["text"]
    if foreign_write:
        service.deprecate(affected.id)
    store = backend._sessions
    before = {
        k: rows(service.paths, k) for k in ("memories", "undo_effects", "audit", "session_events")
    }
    path = f"/api/chat?receipt=1&replace={target}"
    body = {"session_id": sid, "message": "replacement"}
    append = store._insert

    def fail_after_publish(db, key, record):
        append(db, key, record)
        if key == sid:
            assert not service.store.find(affected.id)
            raise OSError("synthetic write failure")

    with monkeypatch.context() as patch:
        if not foreign_write:
            patch.setattr(store, "_insert", fail_after_publish)
        response = client.post(path, json=body)
    assert response.status_code == (409 if foreign_write else 503), response.text
    assert response.json()["error"]["code"] == (
        "edit_conflict" if foreign_write else "edit_not_sent"
    )
    assert all(rows(service.paths, k) == expected for k, expected in before.items())
    assert client.get(f"/api/sessions/{sid}").json()["items"][-1]["content"] == "original reply"
    assert model.calls == 3
    if not foreign_write:
        accepted = client.post(path, json=body)
        assert accepted.status_code == 202
        client.get(f"/api/chat/turns/{accepted.json()['turn_id']}")
        assert not service.store.find(affected.id)
        assert client.post(path, json=body).json()["turn_id"] == accepted.json()["turn_id"]
        assert model.calls == 4


def test_history_refresh_during_a_failed_edit_sees_the_committed_exchange(
    client, backend, monkeypatch
):

    events = collect(client, "original")
    sid = events[-1]["session_id"]
    target = next(event["message_id"] for event in events if event["type"] == "message_saved")
    history_path = f"/api/sessions/{sid}"
    original = client.get(history_path).json()["items"]
    store = backend._sessions
    published, reading, fail = threading.Event(), threading.Event(), threading.Event()
    append, history = store._insert, backend.history

    def delay_failure(db, key, record):
        append(db, key, record)
        if key == sid:
            published.set()
            assert fail.wait(5)
            raise OSError("synthetic failure before commit")

    def refresh(*args, **kwargs):
        reading.set()
        return history(*args, **kwargs)

    monkeypatch.setattr(store, "_insert", delay_failure)
    monkeypatch.setattr(backend, "history", refresh)
    with ThreadPoolExecutor(2) as pool:
        edit = pool.submit(
            client.post,
            f"/api/chat?receipt=1&replace={target}",
            json={"session_id": sid, "message": "replacement"},
        )
        try:
            assert published.wait(5)
            reader = pool.submit(client.get, history_path)
            assert reading.wait(5)
            # The reader may finish immediately with the prior committed snapshot.
            if reader.done():
                assert reader.result().json()["items"] == original
        finally:
            fail.set()
        assert edit.result(5).status_code == 503
        assert reader.result(5).json()["items"] == original


def test_reloaded_view_can_recover_only_matching_edit_token_session_and_payload(client, backend):
    import httpx

    from syvrio.app.web.gateway import create_gateway

    events = collect(client, "original")
    target = next(event["message_id"] for event in events if event["type"] == "message_saved")
    session = backend._conversation.current_id
    token = {"X-Syvrio-Edit": "e" * 32}
    after_reload = {"X-Syvrio-View": "ad39884c-901c-4110-ad0f-7b5cb14befcf"}
    path = f"/api/chat?receipt=1&replace={target}"
    body = {"session_id": session, "message": "revised"}
    gateway = create_gateway(BASE_URL, "127.0.0.1")
    with TestClient(gateway, base_url=BASE_URL) as browser:
        upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app))
        gateway.state.client = upstream
        try:
            receipt = browser.post(path, json=body, headers=token).json()
            assert (
                browser.post(f"/api/sessions/{session}/switch", headers=after_reload).status_code
                == 200
            )
            assert browser.post(path, json=body, headers=after_reload).status_code == 409
            assert (
                browser.post(
                    path, json={**body, "message": "different"}, headers=after_reload | token
                ).status_code
                == 409
            )
            replay = browser.post(path, json=body, headers=after_reload | token)
            assert replay.status_code == 202, replay.text
            assert replay.json()["turn_id"] == receipt["turn_id"]
            browser.get(f"/api/chat/turns/{receipt['turn_id']}")
        finally:
            browser.portal.call(upstream.aclose)
    assert backend._runtime.model.calls == 2
