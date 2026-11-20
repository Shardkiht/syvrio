from __future__ import annotations

import json
from datetime import date

import pytest
from typer.testing import CliRunner

from syvrio.agent.session import dated_message, now_iso
from syvrio.app.cli import app
from syvrio.memory.models import MemoryContext, MemoryDraft
from syvrio.memory.temporal import matches_context, resolve_relative, validate_range

pytestmark = pytest.mark.memory


def test_inference_review_edit_and_source_changes_preserve_history(service):
    source = service.note("偏好阅读")
    item = service.propose_inference("可能喜欢安静活动", [source.id], "爱好")
    path = service.inferences.path_for(item.kind, item.id)
    confirmed = service.review_inference(item.id, "confirm")
    assert confirmed.status == "confirmed"
    initial = path.read_bytes()
    assert (
        service.review_inference(item.id, "confirm") == confirmed and path.read_bytes() == initial
    )
    replacement = service.review_inference(item.id, "correct", "可能喜欢室内活动")
    assert replacement.id != item.id and replacement.status == "confirmed"
    assert replacement.kind == item.kind and replacement.source_ids == [source.id]
    history = {i.id: i for i in service.list_inferences()}
    assert history[item.id].status == "deprecated" and history[item.id].text == item.text
    assert history[replacement.id] == replacement
    assert service.store.entries() == [source]
    paths = [path, service.inferences.path_for(replacement.kind, replacement.id)]
    before = [p.read_bytes() for p in paths]
    service.correct(source.id, MemoryDraft(text="只喜欢阅读散文", topic="preferences"))
    service.list_inferences()
    assert [p.read_bytes() for p in paths] == before


def test_deprecated_inference_stays_deprecated_until_a_new_record(service):
    source = service.note("合成事实")
    item = service.propose_inference("合成假设", [source.id], "主题")
    service.review_inference(item.id, "reject")
    assert service.review_inference(item.id, "reject").status == "deprecated"
    assert service.propose_inference(item.text, item.source_ids, item.kind).id == item.id
    with pytest.raises(ValueError):
        service.review_inference(item.id, "confirm")


def test_unknown_source_or_inference_source_is_rejected(service):
    fact = service.note("合成事实")
    item = service.propose_inference("合成假设", [fact.id], "动态主题")
    for key in ("missing", item.id):
        with pytest.raises(ValueError):
            service.propose_inference("缺少事实来源", [key], "动态主题")
    assert service.list_inferences() == [item]


@pytest.mark.parametrize(
    "text,anchor,expected",
    [
        ("昨天", "2024-03-01T00:10:00+08:00", "2024-02-29"),
        ("昨天", "2024-03-01T23:10:00-08:00", "2024-02-29"),
        ("去年", "2024-03-01T00:10:00+08:00", "2023"),
        ("上个月", "2024-01-01T00:10:00+08:00", "2023-12"),
        ("最近", "2024-01-01T00:10:00+08:00", None),
        ("昨天", None, None),
        ("昨天", date(2024, 3, 1), "2024-02-29"),
        ("上个月", date(2024, 1, 1), "2023-12"),
    ],
)
def test_relative_dates(text, anchor, expected):
    assert resolve_relative(text, anchor) == expected


def test_source_retains_original_time_zone_and_distinct_event_date(service):
    from syvrio.session import SessionStore

    store = SessionStore(service.paths)
    session = store.create()
    at = "2024-03-01T00:10:00+08:00"
    store.append(
        session,
        {
            "type": "message",
            "role": "user",
            "id": "source",
            "at": at,
            "content": "昨天参加展览，当时紧张，现在回想很开心",
        },
    )
    resolved = resolve_relative("昨天", at)
    record = MemoryDraft(
        text="参加展览",
        topic="events",
        context=MemoryContext(
            event_time=resolved, emotion={"event": "紧张", "recollection": "开心"}
        ),
    )
    entry = service.memorize(record, session_id=session.id, message_id="source")
    assert entry.context.event_time == "2024-02-29"
    original = service.read_memory_source(entry.id)["message"]
    assert original["at"] == "2024-02-29T16:10:00+00:00"
    local_day = date.fromisoformat(original["original_local_date"])
    assert resolve_relative("昨天", local_day) == resolved
    assert {"event": "紧张", "recollection": "开心"} == entry.context.emotion.model_dump(
        exclude_none=True
    )


def test_message_anchor_rejects_missing_timezone():
    with pytest.raises(ValueError, match="explicit UTC offset"):
        resolve_relative("昨天", "2024-01-01T00:10:00")
    with pytest.raises(ValueError, match="explicit UTC offset"):
        dated_message({"content": "昨天", "at": "2024-03-01T00:00:00"})
    assert dated_message({"content": "昨天"}) == "昨天"
    assert now_iso().endswith("+00:00")


def test_context_range_and_missing_metadata():
    context = MemoryContext(event_time="2024", location="合成展馆")
    assert matches_context(context, "2024-06", "2024-06", "展馆", None)
    assert not matches_context(context, "2025", None, None, None)
    assert not matches_context(None, "2024", None, None, None)
    with pytest.raises(ValueError):
        validate_range("2025", "2024")


def test_filtered_recall_filters_before_limit(service):
    from syvrio.retrieval.base import SearchHit

    first = service.note("合成展览旧记录")
    second = service.add(
        MemoryDraft(
            text="合成展览新记录", topic="events", context=MemoryContext(event_time="2024-06")
        )
    )

    class Ranked:
        def search(self, query, limit=10):
            return [SearchHit(id=e.id, text=e.text, topic=e.topic) for e in (first, second)][:limit]

    service.retriever = Ranked()
    assert [h.id for h in service.recall("展览", 1, time_range={"start": "2024"})] == [second.id]
    assert [h.id for h in service.recall("展览", 1)] == [first.id]


def test_keyword_context_retrieval_keeps_old_memories_without_filters(service):
    from datetime import UTC, datetime

    old = service.store.append("events", "合成展览旧记录", when=datetime(2024, 6, 1, tzinfo=UTC))
    wanted = service.store.append(
        "events",
        "合成展览新记录",
        context=MemoryContext(
            event_time="2024-06",
            location="合成展馆",
            emotion={"event": "满怀期待", "recollection": "释然"},
        ),
    )
    service.store.append("events", "合成展览另一记录", context=MemoryContext(event_time="2025"))
    hits = service.recall(
        "合成展览", time_range={"start": "2024", "end": "2024"}, location="展馆", emotion="期待"
    )
    assert [hit.id for hit in hits] == [wanted.id]
    assert hits[0].model_dump(exclude_none=True, warnings="error")["context"]["emotion"] == {
        "event": "满怀期待",
        "recollection": "释然",
    }
    assert [hit.id for hit in service.recall("合成展览", emotion="释然")] == [wanted.id]
    assert not service.recall("合成展览", emotion="待释")
    assert old.id in {hit.id for hit in service.recall("合成展览")}
    assert old.id not in {
        hit.id for hit in service.recall("合成展览", time_range={"start": "2024", "end": "2024"})
    }


def test_inference_cli(service, monkeypatch):
    monkeypatch.setattr("syvrio.app.cli._service", lambda *args: service)
    source = service.note("合成事实")
    item = service.propose_inference("合成假设", [source.id], "日常")
    result = CliRunner().invoke(app, ["inferences", item.id, "--action", "confirm"])
    assert result.exit_code == 0, result.output
    assert "confirmed" in result.output


def test_formal_updates_do_not_read_or_rewrite_a_damaged_inference(service):
    source = service.note("合成事实")
    item = service.propose_inference("合成假设", [source.id], "动态主题")
    path = service.inferences.path_for(item.kind, item.id)
    original = path.read_bytes()
    broken = original + b"\nmalformed Markdown\n"
    path.write_bytes(broken)
    for operation in (
        service.list_inferences,
        lambda: service.review_inference(item.id, "confirm"),
    ):
        with pytest.raises(ValueError):
            operation()
    assert service.deprecate(source.id).status == "deprecated"
    assert path.read_bytes() == broken
    path.write_bytes(original)
    assert service.list_inferences() == [item]


def test_review_preserves_unknown_extensions_and_missing_final_newline(service):
    source = service.note("合成事实")
    item = service.propose_inference("合成假设", [source.id], "阅读")
    path = service.inferences.path_for(item.kind, item.id)
    extra = b"  * `custom.reviewer`: `manual`"
    path.write_bytes(path.read_bytes() + extra)
    assert service.review_inference(item.id, "confirm").status == "confirmed"
    assert path.read_bytes().endswith(extra)


@pytest.mark.parametrize("action", ["deprecate", "correct", "transition"])
def test_formal_mutations_require_no_inference_review_and_keep_audit(service, monkeypatch, action):
    source = service.note("合成事实", "work")
    item = service.propose_inference("合成假设", [source.id], "工作")
    confirmed = service.review_inference(item.id, "confirm")
    path = service.inferences.path_for(item.kind, item.id)
    original = path.read_bytes()

    def fail(*args):
        pytest.fail("A formal fact change must not mutate inferences.")

    monkeypatch.setattr(service.inferences, "record", fail)
    if action == "deprecate":
        service.deprecate(source.id)
    elif action == "correct":
        corrected = service.correct(source.id, MemoryDraft(text="更正事实", topic="work"))
        assert corrected.supersedes == source.id
    else:
        new = service.transition(
            source.id, MemoryDraft(text="新的事实", topic="work"), previous_valid_to="2026-10"
        )
        assert (
            new.supersedes == source.id
            and service.store.find(source.id).context.valid_to == "2026-10"
        )
    assert service.list_inferences() == [confirmed] and path.read_bytes() == original
    audit = [json.loads(row) for row in service.audit.path.read_bytes().splitlines()]
    assert audit[0]["action"] == "add" and audit[-1]["action"] == "update"


@pytest.mark.parametrize("known_end", [True, False])
def test_transition_keeps_history_and_does_not_invent_dates(service, known_end):
    old = service.note("住在 A", "home")
    new = service.transition(
        old.id,
        MemoryDraft(text="搬到了 B", topic="home"),
        previous_valid_to="2025" if known_end else None,
    )
    recovered = service.store.find(old.id)
    assert recovered.status == "active" and recovered.text == old.text
    assert recovered.context == (MemoryContext(valid_to="2025") if known_end else None)
    assert new.context is None and new.supersedes == old.id


@pytest.mark.agent
def test_agent_recall_passes_context_filters(service):
    from syvrio.agent.session import SessionStore
    from syvrio.agent.tools import build_tools

    service.config.search.enabled = False
    captured = {}

    def recall(query, limit, **filters):
        captured.update(filters)
        return []

    service.recall = recall
    tools = build_tools(service, SessionStore(service.config.vault).create())
    result = tools.execute(
        "recall", {"query": "展览", "time_range": {"start": "2024"}, "location": "合成展馆"}
    )
    assert result.ok
    assert captured["time_range"] == {"start": "2024"} and captured["location"] == "合成展馆"


@pytest.mark.mcp
async def test_mcp_recall_passes_context_filters(service):
    from fastmcp import Client

    from syvrio.app.server import build_server

    service.config.search.enabled = False
    captured = {}

    def recall(query, limit, **filters):
        captured.update(filters)
        return []

    service.recall = recall
    async with Client(build_server(service=service)) as client:
        result = await client.call_tool(
            "recall", {"query": "展览", "time_range": {"start": "2024"}, "location": "合成展馆"}
        )
        assert result.data == []
    assert captured["time_range"] == {"start": "2024"}
    assert captured["location"] == "合成展馆"
