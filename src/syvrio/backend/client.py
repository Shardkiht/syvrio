"""供 CLI、Textual 和 MCP 使用的同步协议适配器。"""

from __future__ import annotations

import json
import time
from base64 import b64encode
from collections.abc import Iterable
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import httpx

from ..infra.errors import SyvrioError
from ..infra.storage.documents import MAX_DOCUMENT_BYTES
from .client_contracts import Entry, Inference, MemoryDraft, PendingItem, RecallEntry
from .endpoint import client_paths, connect


class ResidentRequestError(SyvrioError, ValueError):
    """常驻服务返回的可向用户展示的请求错误。"""


class ResidentClient:
    def __init__(self, config: Path | None = None):
        self.base, self.token = connect(client_paths(config))
        self.view = uuid4().hex

    @classmethod
    def from_endpoint(cls, endpoint: tuple[str, str]) -> ResidentClient:
        """连接已发现的后端，不启动新的实例。"""
        client = cls.__new__(cls)
        client.base, client.token = endpoint
        client.view = uuid4().hex
        return client

    def _http(self):
        return httpx.Client(
            base_url=self.base,
            trust_env=False,
            timeout=httpx.Timeout(None, connect=5),
            headers={"X-Syvrio-Token": self.token, "X-Syvrio-View": self.view},
        )

    @staticmethod
    def _check(response):
        if response.is_success:
            return
        try:
            detail = response.json()["error"]
        except (ValueError, KeyError):
            response.raise_for_status()
        if detail["code"] == "not_found":
            raise KeyError(detail["message"])
        raise ResidentRequestError(detail["message"])

    def request(self, method: str, path: str, **kwargs):
        with self._http() as client:
            response = client.request(method, path, **kwargs)
            self._check(response)
            return response.json() if response.content else None

    def call(self, name: str, **kwargs):
        return self.request("POST", f"/api/resident/{name}", json=kwargs)

    def import_files(self, files: Iterable[Path], apply: bool) -> dict:
        def lines():
            for file in files:
                try:
                    with file.open("rb") as handle:
                        raw = handle.read(MAX_DOCUMENT_BYTES + 1)
                    document = {"name": file.name, "content": b64encode(raw).decode("ascii")}
                except OSError as exc:
                    document = {"name": file.name, "read_error": exc.strerror or type(exc).__name__}
                yield json.dumps(document, ensure_ascii=False).encode() + b"\n"

        return self.request(
            "POST",
            "/api/resident/import-documents",
            content=lines(),
            params={"apply": apply},
            headers={"Content-Type": "application/x-ndjson"},
        )

    def submit(
        self,
        message: str,
        session_id: str | None,
        files: tuple[Path, ...] = (),
        *,
        submission_id: str | None = None,
    ):
        delivery = submission_id or uuid4().hex
        try:
            return self._submit(message, session_id, files, delivery)
        except (httpx.TransportError, json.JSONDecodeError):
            time.sleep(0.35)
            return self._submit(message, session_id, files, delivery)

    def _submit(self, message: str, session_id: str | None, files: tuple[Path, ...], delivery: str):
        fields = {"message": message, "submission_id": delivery}
        if session_id:
            fields["session_id"] = session_id
        if not files:
            return self.request("POST", "/api/chat?receipt=1", json=fields)
        with ExitStack() as opened:
            uploads = [
                ("files", (path.name, opened.enter_context(path.open("rb")))) for path in files
            ]
            return self.request(
                "POST", "/api/chat/attachments?receipt=1", data=fields, files=uploads
            )

    def events(self, turn_id: int, after: int = 0):
        with (
            self._http() as client,
            client.stream("GET", f"/api/chat/turns/{turn_id}", params={"after": after}) as stream,
        ):
            if not stream.is_success:
                stream.read()
                self._check(stream)
            for line in stream.iter_lines():
                if line.startswith("data:"):
                    yield json.loads(line[5:])

    async def aevents(self, turn_id: int, after: int = 0):
        headers = {"X-Syvrio-View": self.view, "X-Syvrio-Token": self.token}
        try:
            async with httpx.AsyncClient(
                base_url=self.base, trust_env=False, headers=headers, timeout=None
            ) as client:
                async with client.stream(
                    "GET", f"/api/chat/turns/{turn_id}", params={"after": after}
                ) as stream:
                    if not stream.is_success:
                        await stream.aread()
                        self._check(stream)
                    async for line in stream.aiter_lines():
                        if line.startswith("data:"):
                            yield json.loads(line[5:])
        except httpx.TransportError as exc:
            raise OSError("Connection to the resident was interrupted.") from exc

    def memorize(self, draft: MemoryDraft):
        return Entry.model_validate(
            self.call("memorize", draft=draft.model_dump(exclude_none=True))
        )

    def recall(self, query: str, limit: int | None = None, **filters):
        return [
            RecallEntry.model_validate(item)
            for item in self.call("recall", query=query, limit=limit, **filters)
        ]

    def read_topic(self, topic):
        return self.call("read_topic", topic=topic)

    def correct(self, entry_id, draft):
        return Entry.model_validate(
            self.call("correct", entry_id=entry_id, draft=draft.model_dump(exclude_none=True))
        )

    def transition(self, entry_id, draft, previous_valid_to=None):
        return Entry.model_validate(
            self.call(
                "transition",
                entry_id=entry_id,
                draft=draft.model_dump(exclude_none=True),
                previous_valid_to=previous_valid_to,
            )
        )

    def deprecate(self, entry_id):
        return Entry.model_validate(self.call("deprecate", entry_id=entry_id))

    def review_inference(self, item_id, action, text=None):
        return Inference.model_validate(
            self.call("review_inference", inference_id=item_id, action=action, text=text)
        )

    def list_inferences(self):
        return [Inference.model_validate(i) for i in self.call("inferences")]

    def pending(self):
        return [PendingItem.model_validate(i) for i in self.call("pending")]

    def approve(self, item_id):
        return Entry.model_validate(self.call("approve", item_id=item_id))

    def reject(self, item_id):
        return PendingItem.model_validate(self.call("reject", item_id=item_id))

    def topics(self):
        return self.call("topics")

    def history(self, limit):
        return self.call("history", limit=limit)

    def commit(self, push=None):
        return self.call("commit", push=push)

    def status(self):
        return self.call("status")
