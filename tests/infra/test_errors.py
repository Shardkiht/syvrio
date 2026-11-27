"""异常类型约定：所有主动报告的失败均属于 SyvrioError。

CLI 边界层捕获 SyvrioError 并转换为用户可读的消息，其他异常由 except Exception 兜底。
多重继承保留旧的 except ValueError 和 except RuntimeError 处理方式，
同时允许统一按新基类捕获。
"""

from __future__ import annotations

import click
import pytest
import typer

from syvrio.app import ui
from syvrio.backend.updates.installer import UpdateError
from syvrio.infra.config import ConfigError
from syvrio.infra.errors import SyvrioError
from syvrio.infra.llm.client import ChatModelError
from syvrio.infra.observability.diagnostics import record_once
from syvrio.infra.search.base import ProviderError
from syvrio.infra.storage.database import DatabaseError
from syvrio.infra.storage.documents import DocumentError
from syvrio.infra.system.gitops import GitError
from syvrio.memory.extractor import ExtractorError

pytestmark = pytest.mark.config

CASES = [
    (ConfigError, ValueError),
    (ChatModelError, RuntimeError),
    (DocumentError, ValueError),
    (ExtractorError, RuntimeError),
    (GitError, RuntimeError),
    (DatabaseError, ValueError),
    (UpdateError, ValueError),
    (ProviderError, Exception),
]


@pytest.mark.parametrize(("error_type", "legacy"), CASES)
def test_custom_errors_inherit_syvrio_and_legacy_base(error_type, legacy):
    assert issubclass(error_type, SyvrioError)
    assert issubclass(error_type, legacy)


def _make(error_type):
    """按各异常类型的构造参数创建实例。"""
    if error_type is UpdateError:
        return error_type("stage", "detail")
    if error_type is ProviderError:
        return error_type("provider", "network", "detail")
    return error_type("staged failure")


@pytest.mark.parametrize("error_type", [error for error, _ in CASES])
def test_instances_are_catchable_as_syvrio_error(error_type):
    with pytest.raises(SyvrioError):
        raise _make(error_type)


def test_fail_records_cli_failed_with_command_before_exit(tmp_path, monkeypatch):
    """fail() 会将异常转换为 typer.Exit，必须在转换前记录原始原因及出错命令。
    过去缺少该记录，曾导致误判更新失败原因。
    """
    from syvrio.infra.observability import diagnostics

    monkeypatch.setattr(diagnostics, "_path", None)
    diagnostics.stop_diagnostics()
    path = diagnostics.start_diagnostics(tmp_path / "syvrio.log")
    assert path is not None

    @click.command(name="fake-cmd")
    @click.pass_context
    def _boom(ctx):
        try:
            raise UpdateError("uv", "uv not found on PATH")
        except UpdateError as exc:
            ui.fail(f"更新失败：{exc}", exc=exc)

    try:
        _boom.main(args=[], standalone_mode=False)
    except typer.Exit:
        pass
    diagnostics.stop_diagnostics()

    import json

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    failed = [event for event in events if event["event"] == "cli.failed"]
    assert len(failed) == 1
    assert "uv not found on PATH" in failed[0]["traceback"]
    assert failed[0]["state"]["command"] == "fake-cmd"


def test_record_once_never_duplicates(tmp_path, monkeypatch):
    from syvrio.infra.observability import diagnostics

    monkeypatch.setattr(diagnostics, "_path", None)
    diagnostics.stop_diagnostics()
    monkeypatch.setattr(diagnostics, "_RECORDED_ONCE", set())
    path = diagnostics.start_diagnostics(tmp_path / "syvrio.log")
    assert path is not None
    record_once("degraded.keyring", RuntimeError("backend down"))
    record_once("degraded.keyring", RuntimeError("backend down"))
    diagnostics.stop_diagnostics()

    import json

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events].count("degraded.keyring") == 1
