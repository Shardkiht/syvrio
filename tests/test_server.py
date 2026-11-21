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


async def test_mcp_memorize_recall_pending(service):
    async with Client(build_server(service=service)) as client:
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


async def test_mcp_topics_status_and_stale_evidence(service):
    async with Client(build_server(service=service)) as client:
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
