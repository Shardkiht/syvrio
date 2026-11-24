from __future__ import annotations

import hashlib
import json
import os

import pytest

from syvrio.backend.maintenance import prepare_vault_runtime
from syvrio.infra.storage.paths import RuntimeBindingError, SyvrioPaths
from syvrio.infra.storage.transaction import TransactionError, VaultTransaction
from syvrio.memory.background.jobs import job_path

pytestmark = pytest.mark.memory


@pytest.fixture
def layout(tmp_path):
    paths = SyvrioPaths(tmp_path / "vault", tmp_path / "runtime")
    prepare_vault_runtime(paths)
    return paths


def old_job(paths, kind, key):
    owner = hashlib.sha256(os.path.normcase(str(paths.vault_dir)).encode()).hexdigest()
    dest = job_path(paths, kind, key)
    old = paths.jobs_dir / "background-memory" / owner / kind / dest.name
    old.parent.mkdir(parents=True, exist_ok=True)
    return old, dest


def old_transaction(paths):
    tx = VaultTransaction(paths)
    owner = hashlib.sha256(os.fsencode(os.path.normcase(str(paths.vault_dir)))).hexdigest()
    tx.journal = paths.jobs_dir / "vault-transactions" / (owner + ".json")
    return tx


def test_upgrade_keeps_rollback_files_and_does_not_replay_completed_jobs(layout):
    for lane in ("title", "dream", "distillation", "context"):
        old, dest = old_job(layout, lane, "example")
        old.write_bytes(b'{"state":"pending"}\n')
    prepare_vault_runtime(layout)
    for lane in ("title", "dream", "distillation", "context"):
        old, dest = old_job(layout, lane, "example")
        assert old.read_bytes() == dest.read_bytes() == b'{"state":"pending"}\n'
        dest.unlink()
    prepare_vault_runtime(layout)
    assert not dest.exists()


def test_retry_after_rollback_imports_changes_only(layout):
    old, dest = old_job(layout, "dream", "job")
    removed, new_removed = old_job(layout, "title", "removed")
    old.write_bytes(b'{"cursor":1}')
    removed.write_bytes(b"{}")
    prepare_vault_runtime(layout)
    old.write_bytes(b'{"cursor":2}')
    removed.unlink()
    added, new_added = old_job(layout, "title", "added")
    added.write_bytes(b'{"session":"synthetic"}')
    prepare_vault_runtime(layout)
    assert dest.read_bytes() == old.read_bytes()
    assert not new_removed.exists()
    assert new_added.read_bytes() == added.read_bytes()


def test_divergent_new_and_old_progress_are_preserved(layout):
    old, dest = old_job(layout, "dream", "job")
    old.write_bytes(b'{"cursor":1}')
    prepare_vault_runtime(layout)
    old.write_bytes(b'{"cursor":2}')
    dest.write_bytes(b'{"cursor":3}')
    extra, target = old_job(layout, "context", "new")
    extra.write_bytes(b"{}")
    with pytest.raises(RuntimeBindingError, match="Conflicting"):
        prepare_vault_runtime(layout)
    assert old.read_bytes() == b'{"cursor":2}' and dest.read_bytes() == b'{"cursor":3}'
    assert not target.exists()


def test_failed_import_can_be_retried_without_partial_jobs(layout, monkeypatch):
    old, dest = old_job(layout, "dream", "profile")
    old.write_bytes(b'{"fact_count":2}')
    write = VaultTransaction.write

    def fail_receipt(tx, path, payload):
        if path.name == "upgrade-0.7-background.json":
            raise OSError("synthetic failure")
        write(tx, path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(VaultTransaction, "write", fail_receipt)
        with pytest.raises(OSError, match="synthetic"):
            prepare_vault_runtime(layout)
    assert old.exists() and not dest.exists()
    prepare_vault_runtime(layout)
    assert dest.read_bytes() == old.read_bytes()


def test_recovers_bound_old_journal_before_import(layout):
    old, dest = old_job(layout, "dream", "job")
    old.write_bytes(b'{"cursor":1}')
    tx = old_transaction(layout)
    tx.write(old, b'{"cursor":2}')
    assert tx.journal.is_file()
    prepare_vault_runtime(layout)
    assert not tx.journal.exists()
    assert dest.read_bytes() == old.read_bytes() == b'{"cursor":1}'


def test_changed_file_in_old_transaction_is_preserved(layout):
    old, dest = old_job(layout, "dream", "job")
    tx = old_transaction(layout)
    tx.write(old, b"{}")
    old.write_bytes(b'{"other":true}')
    with pytest.raises(TransactionError, match="outside"):
        prepare_vault_runtime(layout)
    assert tx.journal.is_file() and not dest.exists()
    assert old.read_bytes() == b'{"other":true}'


def test_missing_binding_does_not_adopt_old_jobs(layout):
    old, dest = old_job(layout, "dream", "schedule")
    old.write_bytes(b"{}")
    (layout.state_dir / "vault-binding.json").unlink()
    with pytest.raises(RuntimeBindingError, match="Missing Runtime binding"):
        prepare_vault_runtime(layout)
    assert not dest.exists() and old.read_bytes() == b"{}"


def test_old_deletion_journal_requires_original_recovery(layout):
    journal = layout.jobs_dir / "session-deletions" / "unfinished.json"
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b"{}")
    with pytest.raises(RuntimeBindingError, match="session deletion"):
        prepare_vault_runtime(layout)
    assert journal.read_bytes() == b"{}"


def test_damaged_receipt_blocks_replay(layout):
    old, dest = old_job(layout, "title", "example")
    old.write_bytes(b"{}")
    prepare_vault_runtime(layout)
    dest.unlink()
    (layout.jobs_dir / "upgrade-0.7-background.json").write_text(
        json.dumps({"../bad": "bad"}), encoding="utf-8"
    )
    with pytest.raises(RuntimeBindingError, match="receipt"):
        prepare_vault_runtime(layout)
    assert not dest.exists()


@pytest.mark.parametrize("remove_owner", [True, False])
def test_archiving_the_rollback_directory_keeps_current_tasks(layout, remove_owner):
    old, dest = old_job(layout, "dream", "schedule")
    old.write_bytes(b'{"schedule":1}')
    sibling, _ = old_job(layout, "title", "example")
    sibling.write_bytes(b"{}")
    prepare_vault_runtime(layout)
    folder = old.parent.parent if remove_owner else old.parent
    folder.rename(layout.runtime_dir / "archived-checkpoints")
    prepare_vault_runtime(layout)
    assert dest.read_bytes() == b'{"schedule":1}'


def test_matching_invalid_job_copies_are_not_adopted(layout):
    old, dest = old_job(layout, "dream", "job")
    dest.parent.mkdir(parents=True)
    for path in (old, dest):
        path.write_bytes(b"incomplete")
    with pytest.raises(RuntimeBindingError, match="Invalid background job"):
        prepare_vault_runtime(layout)
    assert old.read_bytes() == dest.read_bytes() == b"incomplete"
