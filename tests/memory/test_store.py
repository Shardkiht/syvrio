from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from syvrio.infra.storage.paths import SyvrioPaths
from syvrio.infra.storage.transaction import TransactionError, vault_transaction
from syvrio.memory.models import MemoryContext
from syvrio.memory.schema import Core, SchemaError, render_record
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
    raw = store.topic_path("events").read_text(encoding="utf-8")
    assert "~syvrio-escaped~" in raw
    assert "`emotion.event`" in raw and "`emotion.recollection`" in raw
    assert "unspecified" not in raw
    assert len([line for line in raw.splitlines() if line.startswith("* ")]) == 1


def test_empty_context_omits_optional_section(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("test", "旧格式", context=MemoryContext())
    assert store.find(entry.id).context is None
    assert "**Context**" not in store.topic_path("test").read_text(encoding="utf-8")


def test_append_creates_heading_and_entry(tmp_path):
    store = MemoryStore(tmp_path)
    store.ensure()

    entry = store.append(
        "preferences", "喜欢清淡、像茶的风味", extra={"custom.origin": "synthetic"}
    )

    assert entry.topic == "memories/general/preferences.md"
    path = store.topic_path("preferences")
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("# preferences\n\n")
    assert not raw.startswith("---")
    assert "喜欢清淡、像茶的风味" in raw

    entries = store.entries()
    assert len(entries) == 1
    assert entries[0].text == "喜欢清淡、像茶的风味"
    assert any(
        n.key == "custom.origin" and n.value == "synthetic" for n in entries[0].extension_fields
    )


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
    entry = store.append("test", "合成正文 `status=deleted`", extra={"custom.note": source})
    assert store.entries() == [entry]
    assert store.set_status(entry.id, "deprecated")
    loaded = store.find(entry.id)
    assert loaded is not None and loaded.status == "deprecated"
    assert next(n.value for n in loaded.extension_fields if n.key == "custom.note") == source
    assert loaded.text == entry.text
    assert len(store.entries(include_inactive=True)) == 1


def test_metadata_literal_percent_sequences_remain_unchanged(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("test", "合成正文", extra={"custom.origin": "import:synthetic%60file.md"})
    assert store.entries() == [entry]


def test_entry_uses_core_and_extensions(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append(
        "housing",
        "在找市区的房子",
        extra={"session_id": "s-1", "message_id": "m-2"},
        when=datetime(2026, 10, 22, 18, tzinfo=UTC),
    )
    assert entry.raw_markdown in store.topic_path("housing").read_text(encoding="utf-8")
    assert entry.raw_markdown.startswith("* **Core**\n")
    assert entry.raw_markdown.index("`text`: `在找市区的房子`") < entry.raw_markdown.index(
        "**Extensions**"
    )
    assert dict((f.key, f.value) for f in entry.extension_fields) == {
        "recorded_at": "2026-10-22T18:00:00+00:00",
        "session_id": "s-1",
        "message_id": "m-2",
    }
    assert "confidence" not in entry.raw_markdown and "`source`" not in entry.raw_markdown


def test_set_status_rewrites_tag(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append("housing", "预算 4000", when=datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC))
    assert store.set_status(entry.id, "deprecated")

    raw = store.topic_path("housing").read_text(encoding="utf-8")
    assert "`status`: `deprecated`" in raw
    assert store.entries("housing") == []
    assert store.find(entry.id).status == "deprecated"
    assert store.topics()[0].updated == datetime.now().date().isoformat()


def test_write_failure_leaves_original_file_intact(tmp_path, monkeypatch):
    from syvrio.infra.storage import files as file_module

    store = MemoryStore(tmp_path)
    store.append("housing", "原始条目")
    path = store.topic_path("housing")
    before = path.read_text(encoding="utf-8")

    def fail_replace(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(file_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        store.append("housing", "新条目")

    assert path.read_text(encoding="utf-8") == before
    assert not [item for item in path.parent.iterdir() if item.name.endswith(".tmp")]


def test_inline_code_in_text_is_kept(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("tools", "用 `uv sync` 安装")

    entry = store.entries("tools")[0]
    assert entry.text == "用 `uv sync` 安装"


def test_status_change_refuses_malformed_memory_and_keeps_file(tmp_path):
    path = tmp_path / "memories" / "invalid.md"
    path.parent.mkdir(parents=True)
    original = b"# Memory\n\n- malformed record\n"
    path.write_bytes(original)
    with pytest.raises(SchemaError, match="Core"):
        MemoryStore(tmp_path).set_status("mem-test", "deprecated")
    assert path.read_bytes() == original


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


def test_naive_recorded_at_rejected_without_touching_original(tmp_path):
    store = MemoryStore(tmp_path)
    path = store.topic_path("identity")
    path.parent.mkdir(parents=True)
    raw = render_record(
        Core(id="mem-1", text="历史记录", status="active"),
        extensions=[("recorded_at", "2026-10-24T12:53:44")],
    )
    path.write_bytes(raw.encode())
    with pytest.raises(SchemaError, match="recorded_at"):
        store.document(path)
    assert path.read_bytes() == raw.encode()


def test_offset_bearing_recorded_at_still_loads(tmp_path):
    store = MemoryStore(tmp_path)
    store.append("housing", "预算 4000", when=datetime(2026, 10, 22, 18, 0, 0, tzinfo=UTC))
    path = store.topic_path("housing")
    assert "`recorded_at`: `2026-10-22T18:00:00+00:00`" in path.read_text(encoding="utf-8")
    assert len(store.entries("housing")) == 1


def test_only_actual_changes_log_new_values_and_keep_unmanaged_extensions(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.append(
        "personal/exhibit", "参展", context=MemoryContext(emotion={"event": "紧张"})
    )
    path = store.topic_path(entry.topic)
    extra = "  * `lab.metadata`:\n    * `phase`: `初稿`\n    * `phase`: `确认`\n"
    path.write_bytes(path.read_bytes() + extra.encode())
    context = MemoryContext(emotion={"event": "紧张", "recollection": "开心"}, location="示例展厅")
    store.update(entry.id, context=context, status="deprecated")
    original = path.read_bytes()
    store.update(entry.id, context=context, status="deprecated")
    assert path.read_bytes() == original and extra.encode() in original
    with pytest.raises(ValueError, match="already has a value"):
        store.fill_context(entry.id, MemoryContext(emotion={"event": "平静"}))
    assert path.read_bytes() == original
    assert [json.loads(line) for line in store.changes_path.read_bytes().splitlines()] == [
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
    assert len(store.changes_path.read_bytes().splitlines()) == 1


@pytest.mark.parametrize(
    "body", [" \tfirst\n\n~~~~\r\n* name\n", "中文\r    indent\r\n尾部 \n\n", "\n第一行\n最后 "]
)
def test_multiline_memory_text_preserves_whitespace_and_line_endings(tmp_path, body):
    store = MemoryStore(tmp_path)
    entry = store.append("personal/events", body)
    assert entry.text == store.find(entry.id).text == body
    assert "~~~" in entry.raw_markdown


def test_caught_nested_failure_rolls_back_fact_log_audit_and_undo_receipt(tmp_path):
    from syvrio.memory.audit import AuditLog
    from syvrio.memory.turn_undo import TurnFileJournal

    store = MemoryStore(tmp_path)
    journal = TurnFileJournal(
        store.paths, "session", "turn", lambda ids, _: {key: set() for key in ids}
    )
    audit = AuditLog(store.paths)
    with journal.capture(), pytest.raises(TransactionError, match="nested"):
        with vault_transaction(store.paths):
            store.append("events", "写入失败后重试", entry_id="mem-retry")
            audit.write("add", memory_id="mem-retry")
            try:
                store.append("events", "重复ID", entry_id="mem-retry")
            except ValueError:
                pass
    assert store.entries() == [] and not store.changes_path.exists() and not audit.path.exists()
    assert not journal.path.exists()
    with journal.capture():
        store.append("events", "现在成功", entry_id="mem-retry")
    assert set(journal.plan()) == {store.topic_path("events"), store.changes_path}


def test_process_exit_restores_initial_bytes_for_repeated_writes_and_checkpoint(tmp_path):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    store = MemoryStore(paths)
    entry = store.append("home", "original")
    path = store.topic_path("home")
    originals = path.read_bytes(), store.changes_path.read_bytes()
    code = """
import os, sys
from pathlib import Path
from syvrio.infra.storage.paths import SyvrioPaths
from syvrio.infra.storage.transaction import vault_transaction
from syvrio.memory.store import MemoryStore
from syvrio.memory.models import MemoryContext
p = SyvrioPaths(Path(sys.argv[1]), Path(sys.argv[2]))
s = MemoryStore(p)
with vault_transaction(p) as tx:
    s.fill_context(sys.argv[3], MemoryContext(location='test'))
    s.update(sys.argv[3], status='deprecated')
    tx.write(p.jobs_dir / 'checkpoint.json', b'partial')
    os._exit(27)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(paths.vault_dir), str(paths.runtime_dir), entry.id],
        timeout=30,
        capture_output=True,
    )
    assert result.returncode == 27, result.stderr
    assert (paths.jobs_dir / "checkpoint.json").read_bytes() == b"partial"
    assert MemoryStore(paths).find(entry.id).status == "active"
    assert (path.read_bytes(), store.changes_path.read_bytes()) == originals
    assert not (paths.jobs_dir / "checkpoint.json").exists()


def test_competing_process_appends_share_the_same_fact_and_change_history(tmp_path):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    code = """
import sys
from pathlib import Path
from syvrio.infra.storage.paths import SyvrioPaths
from syvrio.memory.store import MemoryStore
store = MemoryStore(SyvrioPaths(Path(sys.argv[1]), Path(sys.argv[2])))
for n in range(3):
    store.append('shared/topic', 'fixture ' + str(n), entry_id=sys.argv[3] + str(n))
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(paths.vault_dir), str(paths.runtime_dir), pfx],
            stderr=subprocess.PIPE,
        )
        for pfx in ["left", "right"]
    ]
    try:
        for process in processes:
            _, error = process.communicate(timeout=45)
            assert process.returncode == 0, error
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    store = MemoryStore(paths)
    ids = {entry.id for entry in store.entries()}
    rows = [json.loads(row) for row in store.changes_path.read_bytes().splitlines()]
    assert len(ids) == len(rows) == 6
    assert {row["id"] for row in rows if row["change"] == "created"} == ids


def test_undo_refuses_new_incoming_inference_and_interleaved_file_change(tmp_path):
    from syvrio.infra.time import utc_timestamp
    from syvrio.memory.inference import InferenceStore
    from syvrio.memory.models import Inference
    from syvrio.memory.turn_undo import TurnFileJournal, TurnUndoError

    store = MemoryStore(tmp_path)
    inferences = InferenceStore(store.paths)

    def references(ids, restored):
        return {
            key: {
                r.id
                for r in inferences.all()
                if key in r.source_ids and inferences.path_for(r.kind, r.id) not in restored
            }
            for key in ids
        }

    journal = TurnFileJournal(store.paths, "session", "turn", references)
    with journal.capture():
        entry = store.append("events", "synthetic")
    receipt = journal.path.read_bytes()
    inf = Inference(
        id="inf1",
        text="possible",
        kind="work",
        source_ids=[entry.id],
        created_at=utc_timestamp(),
        updated_at=utc_timestamp(),
    )
    inferences.add(inf)
    with pytest.raises(TurnUndoError, match="references"):
        journal.plan()
    assert journal.path.read_bytes() == receipt and store.find(entry.id) == entry
    store.append("events", "other session")
    with journal.capture():
        store.update(entry.id, status="deprecated")
    with pytest.raises(TurnUndoError, match="Another turn"):
        journal.plan()


def test_inference_edit_rollback_and_path_validation(tmp_path, monkeypatch):
    from syvrio.infra.storage import transaction
    from syvrio.infra.time import utc_timestamp
    from syvrio.memory.inference import InferenceStore
    from syvrio.memory.models import Inference

    store = MemoryStore(tmp_path)
    infs = InferenceStore(store.paths)
    entry = store.append("reading", "读完一本书")
    item = Inference(
        id="inf-1",
        kind="读书",
        text="也许爱读",
        source_ids=[entry.id],
        created_at=utc_timestamp(),
        updated_at=utc_timestamp(),
    )
    infs.add(item)
    path = infs.path_for(item.kind, item.id)
    unknown = "  * `custom.reviewer`: `甲`\n".encode()
    path.write_bytes(path.read_bytes() + unknown)
    original = path.read_bytes()
    for invalid in ["../outside", "a/b", "CON", "AUX.md", "C:foo", ".git", "trailing."]:
        with pytest.raises(ValueError):
            infs.add(item.model_copy(update={"kind": invalid, "id": "new"}))
    with pytest.raises(ValueError, match="Unknown source"):
        infs.add(item.model_copy(update={"id": "new", "source_ids": [item.id]}))
    with pytest.raises(ValueError, match="new id"):
        infs.record(item.model_copy(update={"text": "other"}))
    revised = item.model_copy(update={"id": "inf-2", "text": "爱读中文", "status": "confirmed"})
    retired = item.model_copy(update={"status": "deprecated"})
    target = infs.path_for(revised.kind, revised.id)
    replace = transaction.atomic_write

    def fail_new_file(path, content):
        if path == target:
            raise OSError("publication failed")
        return replace(path, content)

    with monkeypatch.context() as patch:
        patch.setattr(transaction, "atomic_write", fail_new_file)
        with pytest.raises(OSError, match="publication failed"):
            infs.record(retired, revised)
    assert path.read_bytes() == original and not target.exists()
    infs.record(retired, revised)
    assert infs.all() == [retired, revised] and unknown in path.read_bytes()
    store.set_status(entry.id, "deprecated")
    assert infs.all() == [retired, revised]


def test_undo_preserves_shared_log_prefix_without_archiving_it_in_every_turn(tmp_path):
    from syvrio.memory.turn_undo import TurnFileJournal

    store = MemoryStore(tmp_path)
    store.append("earlier", "already committed")
    initial = store.changes_path.read_bytes()
    undo = TurnFileJournal(store.paths, "session", "turn", lambda ids, _: {i: set() for i in ids})
    with undo.capture():
        entry = store.append("new", "this turn")
        store.update(entry.id, status="deprecated")
    receipt = json.loads(undo.path.read_bytes())
    assert receipt["files"]["records/memory-changes.jsonl"]["prefix_size"] == len(initial)
    assert undo.plan()[store.changes_path] == initial


async def test_transaction_cannot_leak_into_an_independent_task_or_outlive_its_lock(tmp_path):
    import asyncio
    from contextvars import Context, copy_context

    store = MemoryStore(tmp_path)
    path = store.changes_path

    async def another_task():
        with pytest.raises(TransactionError, match="tasks or threads"):
            with vault_transaction(store.paths):
                pass

    with vault_transaction(store.paths) as tx:
        await asyncio.create_task(another_task())
        with pytest.raises(TransactionError, match="interleave"):
            Context().run(store.entries)
        retained_context = copy_context()
    with pytest.raises(TransactionError, match="no longer active"):
        tx.write(path, b"stale")
    assert not path.exists()
    assert retained_context.run(store.append, "events", "new transaction").text == "new transaction"


def test_async_source_version_survives_append_but_detects_topic_move(tmp_path):
    from syvrio.memory.inference import source_fingerprint

    store = MemoryStore(tmp_path)
    fact = store.append("personal/reading", "first")
    version = source_fingerprint(fact)
    store.append("personal/reading", "second")
    assert source_fingerprint(store.find(fact.id)) == version
    new_path = store.topic_path("work/reading")
    new_path.parent.mkdir(parents=True)
    store.topic_path(fact.topic).rename(new_path)
    assert source_fingerprint(store.find(fact.id)) != version
