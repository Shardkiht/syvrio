from __future__ import annotations

from datetime import UTC, datetime

import pytest

from syvrio.memory.legacy import parse_legacy_line
from syvrio.memory.models import MemoryContext
from syvrio.memory.schema import SchemaError
from syvrio.memory.store import MemoryStore, slugify

pytestmark = pytest.mark.memory


def test_context_tag_roundtrip_and_status_change_preserve_id(tmp_path):
    store = MemoryStore(tmp_path)
    context = MemoryContext(
        event_time="2024-06",
        location="合成`展馆\n入口",
        location_kind="event",
        emotion="当时开心",
        emotion_at="event",
        emotion_source="explicit",
    )
    entry = store.append(
        "events", "参加合成展览", context=context, when=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert store.find(entry.id).context == context
    assert entry.timestamp == "2026-01-01T00:00:00+00:00"
    assert store.set_status(entry.id, "deleted")
    loaded = store.find(entry.id)
    assert loaded.id == entry.id and loaded.context == context
    assert loaded.text == "参加合成展览"
    raw = store.topic_path("events").read_text(encoding="utf-8")
    assert "~syvrio-escaped~" in raw
    assert len([line for line in raw.splitlines() if line.startswith("* ")]) == 1


def test_empty_context_omits_optional_section(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("test", "旧格式", context=MemoryContext())
    assert store.find(entry.id).context is None
    assert "**Context**" not in store.topic_path("test").read_text(encoding="utf-8")


def test_append_creates_heading_and_entry(tmp_path):
    store = MemoryStore(tmp_path)
    store.ensure()

    entry = store.append("preferences", "喜欢清淡、像茶的风味", source="chat", confidence=0.93)

    assert entry.topic == "memories/general/preferences.md"
    path = store.topic_path("preferences")
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("# preferences\n\n")
    assert not raw.startswith("---")
    assert "喜欢清淡、像茶的风味" in raw

    entries = store.entries()
    assert len(entries) == 1
    assert entries[0].text == "喜欢清淡、像茶的风味"
    assert entries[0].metadata["source"] == "chat"
    assert entries[0].metadata["confidence"] == "0.93"


def test_append_preserves_existing_bytes(tmp_path):
    store = MemoryStore(tmp_path)
    first = store.append(
        "devices", "使用 手冲滤纸", when=datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)
    )
    original = store.topic_path("devices").read_bytes()
    second = store.append(
        "devices", "买了一个 7L 挎包", when=datetime(2026, 10, 2, 10, 0, 0, tzinfo=UTC)
    )

    assert first.timestamp == "2026-10-01T09:00:00+00:00"
    assert second.timestamp == "2026-10-02T10:00:00+00:00"
    assert store.topic_path("devices").read_bytes().startswith(original)
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
    entry = store.append("test", "合成正文 `status=deleted`", source=source, note=source)
    assert store.entries() == [entry]
    assert store.set_status(entry.id, "deleted")
    loaded = store.find(entry.id)
    assert loaded is not None and loaded.status == "deleted"
    assert loaded.metadata["source"] == source
    assert loaded.metadata["note"] == source
    assert loaded.text == entry.text
    assert len(store.entries(include_inactive=True)) == 1


def test_metadata_literal_percent_sequences_remain_unchanged(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("test", "合成正文", source="import:synthetic%60file.md")
    assert store.entries() == [entry]


def test_entry_uses_core_and_extensions(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append(
        "housing",
        "在找市区的房子",
        source="chat",
        confidence=0.91,
        extra={"session": "s-1", "message": "m-2"},
        when=datetime(2026, 10, 22, 18, 0, 0, tzinfo=UTC),
    )

    raw = store.topic_path("housing").read_text(encoding="utf-8")
    assert entry.raw_markdown in raw
    assert entry.raw_markdown == (
        "* 在找市区的房子\n\n  **Core**\n"
        f"  * `id`: `{entry.id}`\n"
        "  * `status`: `active`\n  * `source`: `chat`\n  * `confidence`: `0.91`\n\n"
        "  **Extensions**\n  * `syvrio.recorded_at`: `2026-10-22T18:00:00+00:00`\n"
        "  * `syvrio.session`: `s-1`\n  * `syvrio.message`: `m-2`\n"
    )


def test_set_status_rewrites_tag(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("housing", "预算 4000", when=datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC))
    assert store.set_status(entry.id, "superseded")

    raw = store.topic_path("housing").read_text(encoding="utf-8")
    assert "`status`: `superseded`" in raw
    assert store.entries("housing") == []
    assert store.find(entry.id).status == "superseded"
    assert store.topics()[0].updated == datetime.now().date().isoformat()


def test_write_failure_leaves_original_file_intact(tmp_path, monkeypatch):
    from syvrio.memory import store as store_module

    store = MemoryStore(tmp_path)
    store.append("housing", "原始条目")
    path = store.topic_path("housing")
    before = path.read_text(encoding="utf-8")

    def fail_replace(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        store.append("housing", "新条目")

    assert path.read_text(encoding="utf-8") == before
    assert not [item for item in path.parent.iterdir() if item.name.endswith(".tmp")]


def test_inline_code_in_text_is_kept(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("tools", "用 `uv sync` 安装")

    entry = store.entries("tools")[0]
    assert entry.text == "用 `uv sync` 安装"


def test_legacy_comment_line_still_parses(tmp_path):
    path = tmp_path / "memories" / "old.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\ntopic: old\ntitle: 旧\n---\n\n# 旧\n\n"
        "- <!-- id=mem-old; status=active --> 2026-10-01T09:00:00 — 旧事实"
        "  (source: chat, confidence: 0.80)\n",
        encoding="utf-8",
    )
    store = MemoryStore(tmp_path)

    line = next(
        line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")
    )
    entry = parse_legacy_line(line, "old")
    assert entry is not None
    assert entry.text == "旧事实"
    assert entry.metadata["confidence"] == "0.80"

    with pytest.raises(SchemaError, match="explicit v2 migration"):
        store.set_status("mem-old", "deleted")
    assert "status=active -->" in path.read_text(encoding="utf-8")


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


def test_legacy_comment_without_id_has_stable_id(tmp_path):
    path = tmp_path / "memories" / "old.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "- <!-- status=active --> 2026-10-01T09:00:00 — 无 ID 的旧条目\n",
        encoding="utf-8",
    )
    line = path.read_text(encoding="utf-8")
    first = parse_legacy_line(line, "old")
    second = parse_legacy_line(line, "old")
    assert first.id == second.id and first.id.startswith("mem-legacy-")
