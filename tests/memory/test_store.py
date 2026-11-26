from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from syvrio.infra.storage.database import transaction
from syvrio.memory.models import MemoryContext
from syvrio.memory.store import MemoryStore, slugify

pytestmark = pytest.mark.memory


def test_context_tag_roundtrip_and_status_change_preserve_id(tmp_path):
    store = MemoryStore(tmp_path)
    context = MemoryContext(
        event_time="2024-06",
        location="合成`展馆\n入口",
        emotion={"event": "有点紧张", "recollection": "挺\n开心"},
    )
    entry = store.append(
        "events", "参加合成展览", context=context, when=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert store.find(entry.id).context == context
    assert entry.timestamp == "2026-01-01T00:00:00+00:00"
    assert store.set_status(entry.id, "deprecated")
    loaded = store.find(entry.id)
    assert loaded.id == entry.id and loaded.context == context
    assert loaded.text == "参加合成展览"


def test_cached_snapshot_tracks_topic_visibility_and_does_not_share_mutable_context(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("topic", "synthetic", context=MemoryContext(location="原地点"))
    snapshot = store.snapshot()
    assert store.snapshot() is snapshot
    snapshot.by_id[entry.id].context.location = "未保存"
    assert store.snapshot().by_id[entry.id].context.location == "原地点"
    with transaction(store.paths) as db:
        db.execute("UPDATE topics SET archived=1 WHERE name=?", (entry.topic,))
    assert store.snapshot().records == ()
    with transaction(store.paths) as db:
        db.execute("UPDATE topics SET archived=0 WHERE name=?", (entry.topic,))
    assert store.snapshot().records == snapshot.records


def test_empty_context_omits_optional_section(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("test", "旧格式", context=MemoryContext())
    assert store.find(entry.id).context is None


def test_append_creates_heading_and_entry(tmp_path):
    store = MemoryStore(tmp_path)
    store.ensure()

    entry = store.append(
        "preferences", "喜欢清淡、像茶的风味", extra={"custom.origin": "synthetic"}
    )

    assert entry.topic == "general/preferences"
    entries = store.entries()
    assert len(entries) == 1
    assert entries[0].text == "喜欢清淡、像茶的风味"
    assert entries[0].metadata["custom.origin"] == "synthetic"


def test_append_preserves_existing_bytes(tmp_path):
    store = MemoryStore(tmp_path)
    first = store.append(
        "devices", "使用 手冲滤纸", when=datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)
    )
    second = store.append(
        "devices", "买了一个 7L 挎包", when=datetime(2026, 10, 2, 10, 0, 0, tzinfo=UTC)
    )

    assert first.timestamp == "2026-10-01T09:00:00+00:00"
    assert second.timestamp == "2026-10-02T10:00:00+00:00"
    assert store.find(first.id) == first
    assert len(store.entries("devices")) == 2


def test_title_is_preserved(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("preferences", "第一条", title="偏好")
    store.append("preferences", "第二条")

    topic = store.topics()[0]
    assert topic.title == "偏好"
    assert topic.entries == 2


def test_has_text_normalizes_whitespace(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("preferences", "喜欢 清淡 风味")
    assert store.has_text("喜欢清淡风味")
    assert not store.has_text("喜欢浓郁风味")


def test_append_preserves_multiline_text(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("preferences", "标题行\n正文第一行\n正文第二行")

    entries = store.entries()
    assert len(entries) == 1
    assert entries[0].text == "标题行\n正文第一行\n正文第二行"
    assert "标题行" in entries[0].text and "正文第二行" in entries[0].text


def test_recent_context_is_limited(tmp_path):
    store = MemoryStore(tmp_path)
    for index in range(5):
        store.append(
            "devices", f"记录 {index}", when=datetime(2026, 10, 1, 9, index, 0, tzinfo=UTC)
        )

    context = store.recent_context(limit=3)
    assert context.count("\n") == 2
    assert "记录 4" in context
    assert "记录 2" in context
    assert "记录 0" not in context


def test_slugify():
    assert slugify("Preferences") == "preferences"
    assert slugify("food & drink") == "food-drink"
    assert slugify("  ") == "uncategorized"
    assert slugify("学习计划") == "学习计划"


@pytest.mark.parametrize(
    "source", ["import:合成`笔记.md", "合成\n来源\r尾行", "~syvrio-escaped~literal%20"]
)
def test_metadata_special_characters_roundtrip_with_stable_id(tmp_path, source):
    store = MemoryStore(tmp_path)
    entry = store.append("test", "合成正文 `status=deleted`", extra={"custom.note": source})
    assert store.entries() == [entry]
    assert store.set_status(entry.id, "deprecated")
    loaded = store.find(entry.id)
    assert loaded is not None and loaded.status == "deprecated"
    assert loaded.metadata["custom.note"] == source
    assert loaded.text == entry.text
    assert len(store.entries(include_inactive=True)) == 1


def test_metadata_literal_percent_sequences_remain_unchanged(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("test", "合成正文", extra={"custom.origin": "import:synthetic%60file.md"})
    assert store.entries() == [entry]


def test_set_status_rewrites_tag(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("housing", "预算 4000", when=datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC))
    assert store.set_status(entry.id, "deprecated")

    assert store.entries("housing") == []
    assert store.find(entry.id).status == "deprecated"
    assert store.topics()[0].updated == datetime.now().date().isoformat()


def test_inline_code_in_text_is_kept(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("tools", "用 `uv sync` 安装")

    entry = store.entries("tools")[0]
    assert entry.text == "用 `uv sync` 安装"


def test_append_retries_duplicate_entry_id(tmp_path, monkeypatch):
    from syvrio.memory import store as store_module

    store = MemoryStore(tmp_path)
    stamp = datetime(2026, 10, 24, 12, 53, 48, tzinfo=UTC)
    tokens = iter(["175a", "175a", "beef"])
    monkeypatch.setattr(store_module.secrets, "token_hex", lambda _: next(tokens))

    first = store.append("a", "第一条", when=stamp)
    second = store.append("b", "第二条", when=stamp)

    assert first.id.endswith("175a")
    assert second.id.endswith("beef")
    assert len({entry.id for entry in store.entries()}) == 2


def test_trailing_tag_like_text_is_not_metadata(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("misc", "临时记录 `status=deleted`")

    loaded = store.entries()[0]
    assert loaded.status == "active"
    assert loaded.text == "临时记录 `status=deleted`"
    assert loaded.id == entry.id


def test_trailing_fake_id_does_not_replace_entry_id(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("misc", "记录 `id=mem-fake`")

    loaded = store.entries()[0]
    assert loaded.id == entry.id
    assert loaded.text == "记录 `id=mem-fake`"


def test_naive_recorded_at_rejected(tmp_path):
    with pytest.raises(ValueError):
        MemoryStore(tmp_path).append("synthetic", "fact", when=datetime(2020, 1, 1))


def test_offset_bearing_recorded_at_still_loads(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("housing", "预算 4000", when=datetime(2026, 10, 22, 18, 0, 0, tzinfo=UTC))
    assert store.entries()[0].timestamp == "2026-10-22T18:00:00+00:00"
    assert len(store.entries("housing")) == 1


def test_only_actual_changes_log_new_values_and_keep_unmanaged_extensions(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append(
        "personal/exhibit", "参展", context=MemoryContext(emotion={"event": "紧张"})
    )
    context = MemoryContext(emotion={"event": "紧张", "recollection": "开心"}, location="示例展厅")
    store.update(entry.id, context=context, status="deprecated")
    original = store.find(entry.id)
    store.update(entry.id, context=context, status="deprecated")
    assert store.find(entry.id) == original
    with pytest.raises(ValueError, match="already has a value"):
        store.fill_context(entry.id, MemoryContext(emotion={"event": "平静"}))
    assert store.find(entry.id) == original
    with transaction(store.paths, write=False) as db:
        history = [
            dict(id=r[0], change=json.loads(r[1]))
            for r in db.execute("SELECT memory_id,change FROM memory_changes ORDER BY seq")
        ]
    assert history == [
        {"id": entry.id, "change": "created"},
        {
            "id": entry.id,
            "change": {
                "status": "deprecated",
                "context": {"emotion": {"recollection": "开心"}, "location": "示例展厅"},
            },
        },
    ]


def test_fill_range_validated_against_existing_partial_date(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("events", "参加会议", context=MemoryContext(valid_from="2025"))
    with pytest.raises(ValueError):
        store.fill_context(entry.id, MemoryContext(valid_to="2024"))
    assert store.find(entry.id).context.valid_to is None
    with transaction(store.paths, write=False) as db:
        assert db.execute("SELECT count(*) FROM memory_changes").fetchone()[0] == 1


@pytest.mark.parametrize(
    "body", [" \tfirst\n\n~~~~\r\n* name\n", "中文\r    indent\r\n尾部 \n\n", "\n第一行\n最后 "]
)
def test_multiline_memory_text_preserves_whitespace_and_line_endings(tmp_path, body):
    store = MemoryStore(tmp_path)
    entry = store.append("personal/events", body)
    assert entry.text == store.find(entry.id).text == body
