from __future__ import annotations

import pytest
from fastmcp import Client

from syvrio.app.server import build_server

pytestmark = pytest.mark.mcp


async def reviewed(client, operation, arguments):
    first = (await client.call_tool(operation, arguments)).data
    assert first["status"] == "review_required"
    return (
        await client.call_tool(operation, {**arguments, "review_token": first["review_token"]})
    ).data


async def test_mcp_memorize_recall_pending(resident_backend):
    _, resident = resident_backend
    async with Client(build_server(client=resident)) as client:
        entry = await reviewed(client, "memorize", {"text": "偏好清淡风味", "topic": "preferences"})
        assert entry["text"] == "偏好清淡风味"
        assert "raw_markdown" not in entry
        recalled = await client.call_tool("recall", {"query": "清淡"})
        assert recalled.data[0]["id"] == entry["id"]
        pending = await reviewed(
            client,
            "create_pending",
            {
                "draft": {"text": "可能喜欢浅烘", "topic": "preferences"},
                "question": "喜欢浅烘吗？",
                "related_ids": [entry["id"]],
            },
        )
        listed = await client.call_tool("review_pending", {})
        assert listed.data[0]["question"] == "喜欢浅烘吗？"
        assert listed.data[0]["related_ids"] == [entry["id"]]
        assert (await client.call_tool("approve", {"item_id": pending["id"]})).data[
            "text"
        ] == "可能喜欢浅烘"
        assert (await client.call_tool("review_pending", {})).data == []


async def test_mcp_rejects_deprecated_target_before_review(resident_backend):
    _, resident = resident_backend
    async with Client(build_server(client=resident)) as client:
        saved = await reviewed(client, "note", {"text": "合成旧事实", "topic": "test"})
        await reviewed(client, "deprecate", {"entry_id": saved["id"]})
        for action in ("correct", "transition"):
            response = await client.call_tool(
                action,
                {"entry_id": saved["id"], "draft": {"text": "合成新事实", "topic": "test"}},
                raise_on_error=False,
            )
            assert response.is_error and "active memory" in response.content[0].text
        again = await reviewed(client, "deprecate", {"entry_id": saved["id"]})
        assert again["id"] == saved["id"]


async def test_mcp_topics_status_and_stale_evidence(resident_backend):
    _, resident = resident_backend
    async with Client(build_server(client=resident)) as client:
        args = {"text": "用手冲滤纸", "topic": "devices"}
        review = (await client.call_tool("memorize", args)).data
        await reviewed(client, "note", {"text": "装有滤杯", "topic": "devices"})
        refreshed = (
            await client.call_tool("memorize", {**args, "review_token": review["review_token"]})
        ).data
        assert refreshed["status"] == "review_required"
        saved = (
            await client.call_tool("memorize", {**args, "review_token": refreshed["review_token"]})
        ).data
        topics = (await client.call_tool("list_topics", {})).data
        assert len(topics) == 1 and topics[0]["entries"] == 2
        topic = (await client.call_tool("read_topic", {"topic": "devices"})).data
        assert len(topic["entries"]) == 2 and any(
            row["id"] == saved["id"] for row in topic["entries"]
        )
        assert (await client.call_tool("status", {})).data["entries"] == 2


async def test_mcp_correct_rechecks_reviewed_revision_before_requesting_review_again(
    resident_backend,
):
    _, resident = resident_backend
    async with Client(build_server(client=resident)) as client:
        old = await reviewed(client, "note", {"text": "初始事实", "topic": "tests"})
        args = {
            "entry_id": old["id"],
            "draft": {"text": "提案", "expected_revision": old["revision"]},
        }
        review = (await client.call_tool("correct", args)).data
        assert review["status"] == "review_required"
        await reviewed(client, "correct", {"entry_id": old["id"], "draft": {"text": "已核准事实"}})
        response = await client.call_tool(
            "correct", {**args, "review_token": review["review_token"]}, raise_on_error=False
        )
        assert response.is_error and "memory changed" in response.content[0].text.lower()


async def test_mcp_search_reports_quota_even_when_fallback_succeeds(resident_backend, monkeypatch):
    from unittest.mock import Mock

    from syvrio.infra.search.base import ProviderError, SearchResult
    from syvrio.infra.search.pauses import ProviderPauses
    from syvrio.infra.search.router import SearchRouter

    owner, resident = resident_backend
    owner._service.config.search.enabled = True
    exhausted = Mock(capabilities={"search"})
    exhausted.search.side_effect = ProviderError(
        "exa", "quota", "NO_MORE_CREDITS", pause_reason="NO_MORE_CREDITS"
    )
    backup = Mock(capabilities={"search"}, last_partial_failures=[])
    backup.search.return_value = [SearchResult("Synthetic report", "https://example.com", "Report")]
    router = SearchRouter(
        {"exa": exhausted, "backup": backup}, provider_pauses=ProviderPauses(owner._service.paths)
    )
    monkeypatch.setattr("syvrio.backend.operations.build_search_router", lambda *_: router)
    async with Client(build_server(client=resident)) as client:
        tools = await client.list_tools()
        assert (
            "quota_exhausted" in next(tool for tool in tools if tool.name == "search").description
        )
        result = (await client.call_tool("search", {"query": "Synthetic report"})).data
        assert result["quota_exhausted"] == ["exa"] and result["provider"] == "backup"
        strict = {"query": "Synthetic report", "providers": ["exa"], "strategy": "strict"}
        failed = (await client.call_tool("search", strict)).data
        assert failed["quota_exhausted"] == ["exa"] and failed["results"] == []
        assert failed["failed"][0]["kind"] == "quota"
