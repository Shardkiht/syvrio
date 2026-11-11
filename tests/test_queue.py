from __future__ import annotations

import json

import pytest

from syvrio.memory.models import PendingItem
from syvrio.memory.queue import PendingQueue

pytestmark = pytest.mark.memory


def make_item(item_id: str = "p-1") -> PendingItem:
    return PendingItem(
        id=item_id,
        created_at="2026-10-22T12:00:00+00:00",
        source="chat",
        text="候选事实",
        topic="preferences",
        confidence=0.5,
        kind="add",
        reason="low confidence 0.50",
    )


def test_add_and_load(tmp_path):
    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    assert [item.id for item in queue.all()] == ["p-1"]
    assert queue.get("p-1").text == "候选事实"


def test_resolve_removes_and_logs(tmp_path):
    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    queue.add(make_item("p-2"))

    resolved = queue.resolve("p-1", "approved")

    assert resolved.id == "p-1"
    assert [item.id for item in queue.all()] == ["p-2"]
    log = (tmp_path / "records" / "memory" / "resolved.jsonl").read_text(encoding="utf-8")
    assert '"status": "approved"' in log
    assert "p-1" in log
    record = json.loads(log)
    assert record["created_at"].endswith("+00:00")
    assert record["resolved_at"].endswith("+00:00")


def test_resolve_missing_returns_none(tmp_path):
    assert PendingQueue(tmp_path).resolve("nope", "rejected") is None


def test_save_failure_leaves_pending_queue_intact(tmp_path, monkeypatch):
    from syvrio.memory import queue as queue_module

    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    before = queue.pending_path.read_text(encoding="utf-8")

    def fail_replace(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(queue_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        queue.add(make_item("p-2"))

    assert queue.pending_path.read_text(encoding="utf-8") == before
    assert [item.id for item in queue.all()] == ["p-1"]
    assert not [item for item in queue.dir.iterdir() if item.name.endswith(".tmp")]


def test_resolve_only_removes_the_targeted_duplicate(tmp_path):
    queue = PendingQueue(tmp_path)
    queue.add(make_item("p-dup"))
    queue.add(make_item("p-dup"))

    queue.resolve("p-dup", "approved")

    # 历史数据若出现同 ID，只处理目标那一条，不能把另一条一并抹掉。
    assert len(queue.all()) == 1


def test_resolve_log_failure_keeps_candidate(tmp_path):
    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    queue.resolved_path.mkdir()
    with pytest.raises(OSError):
        queue.resolve("p-1", "rejected")
    assert queue.get("p-1") is not None


@pytest.mark.parametrize("stage", ["log", "pending"])
def test_resolve_write_failure_keeps_item_and_retry_logs_once(tmp_path, monkeypatch, stage):
    from pathlib import Path

    queue = PendingQueue(tmp_path)
    queue.add(make_item())
    before = queue.pending_path.read_bytes()
    with monkeypatch.context() as patch:
        if stage == "log":
            original = Path.open

            def fail_log(path, mode="r", *args, **kwargs):
                if path == queue.resolved_path and mode == "a":
                    raise OSError("synthetic log failure")
                return original(path, mode, *args, **kwargs)

            patch.setattr(Path, "open", fail_log)
        else:

            def fail_save(*args):
                raise OSError("synthetic pending failure")

            patch.setattr(queue, "_save", fail_save)
        with pytest.raises(OSError, match="synthetic"):
            queue.resolve("p-1", "approved")
    assert queue.pending_path.read_bytes() == before
    assert not queue.resolved_path.exists()
    queue.resolve("p-1", "approved")
    assert queue.all() == []
    assert len(queue.resolved_path.read_text(encoding="utf-8").splitlines()) == 1


def test_rollback_failure_remains_explicit(tmp_path, monkeypatch):
    from syvrio.memory import queue as queue_module

    path = tmp_path / "synthetic.txt"
    path.write_bytes(b"original")

    def fail_replace(*args):
        raise OSError("synthetic rollback failure")

    with pytest.raises(ExceptionGroup) as caught:
        with queue_module.rollback_files([path]):
            path.write_bytes(b"changed")
            monkeypatch.setattr(queue_module.os, "replace", fail_replace)
            raise OSError("synthetic original failure")
    assert len(caught.value.exceptions) == 2
    assert "original failure" in str(caught.value.exceptions[0])
    assert "rollback failure" in str(caught.value.exceptions[1])
    assert not [item for item in tmp_path.iterdir() if item.name != path.name]
