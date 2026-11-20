"""Synchronous protocol adapter for the CLI, Textual and MCP."""

from __future__ import annotations

import json
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import httpx

from ..memory.models import Entry, Inference, MemoryDraft, PendingItem, RecallEntry
from ..memory.service import MemorizeResult
from .endpoint import client_paths, connect


class ResidentClient:
    def __init__(self, config: Path | None = None, vault: Path | None = None):
        self.config, self.paths = client_paths(config, vault)
        self.base, self.token = connect(self.paths)
        self.view = uuid4().hex

    def _http(self):
        return httpx.Client(base_url=self.base, trust_env=False, timeout=httpx.Timeout(None, connect=5),
                            headers={"X-Syvrio-Token": self.token, "X-Syvrio-View": self.view})

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
        raise ValueError(detail["message"])

    def request(self, method: str, path: str, **kwargs):
        with self._http() as client:
            response = client.request(method, path, **kwargs)
            self._check(response)
            return response.json() if response.content else None

    def call(self, name: str, **kwargs):
        return self.request("POST", f"/api/resident/{name}", json=kwargs)

    def submit(self, message: str, session_id: str | None, files: tuple[Path, ...] = ()):
        if not files:
            return self.request("POST", "/api/chat?receipt=1", json={"message": message, "session_id": session_id})
        with ExitStack() as opened:
            uploads = [("files", (path.name, opened.enter_context(path.open("rb")))) for path in files]
            fields = {"message": message}
            if session_id:
                fields["session_id"] = session_id
            return self.request("POST", "/api/chat/attachments?receipt=1", data=fields, files=uploads)

    def events(self, turn_id: int, after: int = 0):
        with self._http() as client, client.stream("GET", f"/api/chat/turns/{turn_id}", params={"after": after}) as stream:
            if not stream.is_success:
                stream.read()
                self._check(stream)
            for line in stream.iter_lines():
                if line.startswith("data:"):
                    yield json.loads(line[5:])

    async def aevents(self, turn_id: int, after: int = 0):
        headers = {"X-Syvrio-View": self.view, "X-Syvrio-Token": self.token}
        try:
            async with httpx.AsyncClient(base_url=self.base, trust_env=False, headers=headers, timeout=None) as client:
                async with client.stream("GET", f"/api/chat/turns/{turn_id}", params={"after": after}) as stream:
                    if not stream.is_success:
                        await stream.aread()
                        self._check(stream)
                    async for line in stream.aiter_lines():
                        if line.startswith("data:"):
                            yield json.loads(line[5:])
        except httpx.TransportError as exc:
            raise OSError("Connection to the resident was interrupted.") from exc

    def memorize(self, draft: MemoryDraft):
        return Entry.model_validate(self.call("memorize", draft=draft.model_dump(exclude_none=True)))

    def import_text(self, text, source, dry_run, *, extractor=None):
        if extractor is not None:
            raise ValueError("Document extraction is performed by the resident backend.")
        data = self.call("import_text", text=text, source=source, dry_run=dry_run)
        return MemorizeResult([Entry.model_validate(e) for e in data["admitted"]],
                              [PendingItem.model_validate(e) for e in data["queued"]], data["duplicates"], data["blocked"])

    def recall(self, query: str, limit: int = 7, **filters):
        return [RecallEntry.model_validate(item) for item in self.call("recall", query=query, limit=limit, **filters)]

    def read_topic(self, topic):
        return self.call("read_topic", topic=topic)

    def correct(self, entry_id, draft):
        return Entry.model_validate(self.call("correct", entry_id=entry_id, draft=draft.model_dump(exclude_none=True)))

    def transition(self, entry_id, draft, previous_valid_to=None):
        return Entry.model_validate(self.call("transition", entry_id=entry_id, draft=draft.model_dump(exclude_none=True),
                                             previous_valid_to=previous_valid_to))

    def deprecate(self, entry_id):
        return Entry.model_validate(self.call("deprecate", entry_id=entry_id))

    def review_inference(self, item_id, action, text=None):
        return Inference.model_validate(self.call("review_inference", inference_id=item_id, action=action, text=text))

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

    def search(self, query):
        return self.call("search", query=asdict(query))
