from __future__ import annotations

import sys

import pytest
from typer.testing import CliRunner

from syvrio.app.cli import app

pytestmark = pytest.mark.cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def resident_transport(monkeypatch):
    """使用进程内的模型测试数据，检验相同的认证 HTTP 路由。"""
    from contextlib import ExitStack

    from fastapi.testclient import TestClient

    from syvrio.backend.api.channel import APIChannel as WebBackend
    from syvrio.backend.client import ResidentClient
    from syvrio.memory.service import get_service
    from tests.support.resident_api import create_app

    with ExitStack() as instances:
        clients = {}

        class LocalClient(ResidentClient):
            def __init__(self, config):
                service = get_service(config)
                api = create_app(WebBackend(service=service), resident_token="fixture")
                self.transport = instances.enter_context(
                    TestClient(api, base_url="http://localhost")
                )
                self.transport.headers.update(
                    {"X-Syvrio-Token": "fixture", "X-Syvrio-View": "cli-test"}
                )

            def request(self, method, path, **kwargs):
                response = self.transport.request(method, path, **kwargs)
                self._check(response)
                return response.json() if response.content else None

        def use(config):
            if config not in clients:
                clients[config] = LocalClient(config)
            return clients[config]

        monkeypatch.setattr("syvrio.app.cli._service", use)
        yield


def invoke(args):
    return runner.invoke(app, args)


def test_only_initialization_and_offline_verification_accept_a_vault_path():
    from typer.main import get_command

    commands = get_command(app).commands
    assert {
        name
        for name, cmd in commands.items()
        if any("--vault" in param.opts for param in cmd.params)
    } == {"init", "verify-attachments"}


def test_bare_invocation_falls_back_to_help_without_tty():
    result = invoke([])
    assert result.exit_code == 0
    assert "Usage:" in result.output


@pytest.mark.parametrize("args", [[], ["run"]])
def test_dream_cli_runs_and_reports_resident_status(args, monkeypatch):
    from syvrio.memory.background import BackgroundMemory

    monkeypatch.setattr(BackgroundMemory, "start", lambda self: None)
    before = invoke(["dream", "status"])
    assert before.exit_code == 0 and "State: idle" in before.output
    submitted = invoke(["dream", *args])
    assert submitted.exit_code == 0 and "queued" in submitted.output
    after = invoke(["dream", "status"])
    assert after.exit_code == 0 and "State: queued" in after.output


@pytest.mark.parametrize("args", [["cancel"], ["resume"], ["1"], ["--requests", "1"]])
def test_dream_cli_rejects_removed_actions_and_overrides(args):
    assert invoke(["dream", *args]).exit_code == 2


def test_resident_dream_rejects_legacy_operations_and_budget_overrides(monkeypatch):
    from syvrio.app.cli import _service
    from syvrio.memory.background import BackgroundMemory

    monkeypatch.setattr(BackgroundMemory, "start", lambda self: None)
    api = _service(None).transport
    for operation in ("dream_status", "cancel_dream"):
        assert api.post(f"/api/resident/{operation}", json={}).status_code == 404
    overridden = api.post("/api/resident/dream", json={"max_requests": 1})
    assert overridden.status_code == 422
    for action in ("resume", "cancel", "1", None):
        invalid = api.post("/api/resident/dream", json={"action": action}).json()
        assert invalid["status"] == "unknown"
    status = api.post("/api/resident/dream", json={"action": "status"}).json()
    assert status["data"]["state"] == "idle"


def test_format_tokens_uses_km_units():
    from syvrio.infra.formatting import format_tokens

    assert format_tokens(800) == "800"
    assert format_tokens(16000) == "16K"
    assert format_tokens(64000) == "64K"
    assert format_tokens(1000000) == "1M"
    assert format_tokens(1500000) == "1.5M"


def test_truncate_to_tokens_respects_limit():
    from syvrio.infra.llm.tokens import estimate_tokens, truncate_to_tokens

    text = "清淡咖啡与手冲滤纸" * 50
    truncated = truncate_to_tokens(text, 20)
    assert truncated.endswith("（已截断）")
    assert estimate_tokens(truncated) <= 20

    # 极小预算也不能靠追加标记突破上限。
    assert estimate_tokens(truncate_to_tokens(text, 1)) <= 1
    assert truncate_to_tokens("短文本", 20) == "短文本"


@pytest.mark.parametrize("budget", [0, 1, 2, 5, 20])
@pytest.mark.parametrize("text", ["清淡咖啡" * 30, "coffee and tea " * 30, "合成 coffee 茶 " * 30])
def test_offline_token_truncation_respects_small_budgets(monkeypatch, budget, text):
    from syvrio.infra.llm import tokens

    monkeypatch.setattr(tokens, "_ENCODING", False)
    truncated = tokens.truncate_to_tokens(text, budget)
    assert tokens.estimate_tokens(truncated) <= budget


def test_init_uses_existing_vault_and_refuses_to_replace_it(tmp_path):
    import tomllib

    config = tmp_path / "syvrio.toml"
    old_vault = tmp_path / "old-vault"
    new_vault = tmp_path / "new-vault"
    assert invoke(["init", "--vault", str(old_vault), "--config", str(config)]).exit_code == 0

    assert invoke(["init", "--config", str(config)]).exit_code == 0
    before = config.read_bytes()
    result = invoke(["init", "--vault", str(new_vault), "--config", str(config), "--force"])
    assert result.exit_code != 0 and "已配置 Vault" in result.output
    assert not new_vault.exists()
    assert config.read_bytes() == before

    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["vault"] == old_vault.as_posix()


def test_init_pins_default_vault_in_an_existing_configuration(tmp_path, monkeypatch):
    import tomllib

    config = tmp_path / "settings.toml"
    config.write_text("[git]\nauto_sync = false\n", encoding="utf-8")
    vault = tmp_path / "selected"
    monkeypatch.setenv("SYVRIO_VAULT", str(vault))
    result = invoke(["init", "--config", str(config)])
    assert result.exit_code == 0, result.output
    saved = tomllib.loads(config.read_text(encoding="utf-8"))
    assert saved["vault"] == vault.as_posix() and saved["git"]["auto_sync"] is False
    monkeypatch.setenv("SYVRIO_VAULT", str(tmp_path / "unused"))
    assert invoke(["init", "--config", str(config)]).exit_code == 0
    assert not (tmp_path / "unused").exists()


def test_layout_widths_wide_left():
    from syvrio.app.ui import layout_widths

    for cols in (80, 120, 200):
        left, width, right = layout_widths(cols)
        assert left + width + right == cols
        # 宽幅左对齐：固定小左边距，内容约占 90% 且随窗口伸缩、不设上限。
        assert left == 2
        assert width == max(40, min(cols - 4, round(cols * 0.9)))
    assert layout_widths(200)[1] == 180


def test_padded_leaves_right_slack():
    # 对话列不铺满终端：整行恰好占满时，窗口拉伸/收窄会让行尾回绕。
    from rich.console import Console
    from rich.text import Text

    from syvrio.app.ui import padded

    console = Console(width=100)
    with console.capture() as capture:
        console.print(padded(Text("─" * 90), 100))
        console.print(padded("长" * 120, 100))
    lines = capture.get().splitlines()
    assert lines
    assert all(len(line) <= 98 for line in lines)
    assert max(len(line) for line in lines) >= 90  # 内容仍占宽幅主列


def test_escape_cancels_questionary_prompt():
    import questionary
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from syvrio.app.dialogs import ask

    with create_pipe_input() as inp:
        question = questionary.text("name", input=inp, output=DummyOutput())
        inp.send_text("\x1b")
        assert ask(question) is None


def test_init_note_recall_commit(tmp_path):
    from syvrio.infra.config import load_config, write_config

    config = tmp_path / "syvrio.toml"
    vault = tmp_path / "vault"

    result = invoke(["init", "--vault", str(vault), "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert config.exists()
    assert (vault / "export" / ".git").is_dir()

    # 即使每日同步已经到期，该变更也仍须归入手工提交。
    settings = load_config(config)
    settings.git.auto_sync = False
    write_config(config, settings)

    result = invoke(
        [
            "note",
            "喝咖啡偏好清淡风味",
            "--topic",
            "preferences",
            "--config",
            str(config),
        ]
    )
    assert result.exit_code == 0, result.output
    assert "已记录" in result.output

    result = invoke(["recall", "清淡", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "喝咖啡偏好清淡风味" in result.output

    result = invoke(["topics", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "preferences" in result.output

    result = invoke(["pending", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "没有待确认项" in result.output

    result = invoke(["status", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "条目 1" in result.output

    result = invoke(["commit", "--config", str(config), "--no-push"])
    assert result.exit_code == 0, result.output
    assert "本次提交总结" in result.output

    result = invoke(["history", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "memory:" in result.output


def test_explicit_notes_are_not_deduplicated_by_text(tmp_path):
    config = tmp_path / "syvrio.toml"
    vault = tmp_path / "vault"
    invoke(["init", "--vault", str(vault), "--config", str(config)])
    first = invoke(["note", "同一条事实", "--config", str(config)])
    assert first.exit_code == 0
    second = invoke(["note", "同一条事实", "--config", str(config)])
    assert second.exit_code == 0, second.output
    assert "已记录" in second.output


def test_explicit_memorize_works_without_model_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config = tmp_path / "syvrio.toml"
    vault = tmp_path / "vault"
    invoke(["init", "--vault", str(vault), "--config", str(config)])

    result = invoke(["memorize", "一段对话", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "已记录" in result.output


def test_import_checks_all_upload_records_before_mutation(monkeypatch):
    from syvrio.app.cli import _service
    from syvrio.application.memory import MemoryUseCases

    called = []
    monkeypatch.setattr(
        MemoryUseCases, "import_documents", lambda *args, **kwargs: called.append(True)
    )
    api = _service(None).transport
    records = b'{"name":"first.txt","content":"b2s="}\n{"private-fixture":"private-value"}\n'
    response = api.post("/api/resident/import-documents?apply=true", content=records)
    assert response.status_code == 422
    assert "private-" not in response.text
    assert not called


def test_import_without_api_key_reports_hint(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config = tmp_path / "syvrio.toml"
    vault = tmp_path / "vault"
    invoke(["init", "--vault", str(vault), "--config", str(config)])
    source = tmp_path / "notes.md"
    source.write_text("# 档案\n\n- 咖啡偏好清淡风味。\n", encoding="utf-8")

    result = invoke(["import", str(source), "--config", str(config)])

    assert result.exit_code == 1
    assert "syvrio setup" in result.output


@pytest.mark.parametrize("suffix", [".md", ".txt", ".jsonl"])
def test_import_dry_run_then_apply(tmp_path, monkeypatch, suffix):
    from syvrio.memory import service as service_module
    from syvrio.memory.models import Candidate, ExtractionResult

    class FakeImportExtractor:
        def __init__(self, config, topics=None):
            self.topics = topics or []

        def extract(self, text, evidence):
            assert "memories" in evidence
            return ExtractionResult(
                memories=[Candidate(text="咖啡偏好清淡风味", topic="preferences")]
            )

    monkeypatch.setattr(service_module, "ImportExtractor", FakeImportExtractor)
    config = tmp_path / "syvrio.toml"
    vault = tmp_path / "vault"
    invoke(["init", "--vault", str(vault), "--config", str(config)])
    source = tmp_path / ("notes" + suffix)
    source.write_text("# 档案\n\n- 咖啡偏好清淡风味。\n", encoding="utf-8")

    result = invoke(["import", str(source), "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "直写 1" in result.output
    from syvrio.memory.store import MemoryStore

    assert not MemoryStore(vault).entries("preferences")

    result = invoke(["import", str(source), "--apply", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "已写入" in result.output
    assert len(MemoryStore(vault).entries("preferences")) == 1


def test_cli_correct_and_forget(tmp_path):
    from syvrio.infra.config import Config, LLMConfig, write_config
    from syvrio.memory.service import MemoryService

    vault = tmp_path / "vault"
    config = Config(vault=vault, llm=LLMConfig())
    path = tmp_path / "settings.toml"
    write_config(path, config)
    service = MemoryService(config)
    service.initialize_files()
    entry = service.note("旧表述：住在城西", topic="housing")

    result = invoke(
        ["correct", entry.id, "新表述：住在滨江", "--topic", "housing", "--config", str(path)]
    )
    assert result.exit_code == 0, result.output
    assert "已更正" in result.output
    current = service.store.find(entry.id)
    assert current.status == "active" and current.text == "新表述：住在滨江"
    assert [row["text"] for row in service.store.version_history(entry.id)] == [
        entry.text,
        current.text,
    ]

    active = service.store.entries(topic="housing")
    assert len(active) == 1
    result = invoke(["deprecate", active[0].id, "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert "已弃用" in result.output
    assert service.store.entries(topic="housing") == []
    assert service.store.find(active[0].id).status == "deprecated"


def test_cli_correct_rejects_inactive_entry(tmp_path):
    from syvrio.infra.config import Config, LLMConfig, write_config
    from syvrio.memory.service import MemoryService

    vault = tmp_path / "vault"
    config = Config(vault=vault, llm=LLMConfig())
    path = tmp_path / "settings.toml"
    write_config(path, config)
    service = MemoryService(config)
    service.initialize_files()
    entry = service.note("一条记录", topic="misc")
    service.deprecate(entry.id)

    result = invoke(
        ["correct", entry.id, "试图更正已弃用的记忆", "--topic", "misc", "--config", str(path)]
    )
    assert result.exit_code == 1
    assert "active memory" in result.output


@pytest.mark.skipif(sys.platform != "win32", reason="Windows redirected stream encoding")
def test_cli_help_with_western_pipe_encoding():
    import os
    import subprocess

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252:strict"
    result = subprocess.run(
        [sys.executable, "-m", "syvrio.app.cli", "--help"], capture_output=True, env=env, timeout=30
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert "Usage:" in result.stdout.decode("utf-8")
    assert "记忆" in result.stdout.decode("utf-8")


def _update_home(tmp_path, monkeypatch, **config):
    from syvrio.infra.storage import paths

    directory = tmp_path / ".syvrio" / "update"
    directory.mkdir(parents=True)
    lines = [f'{key} = "{value}"' for key, value in config.items()]
    (directory / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(paths, "syvrio_home", lambda: tmp_path / ".syvrio")
    return directory


def test_update_without_configuration_exits_nonzero(tmp_path, monkeypatch):
    from syvrio.infra.storage import paths

    monkeypatch.setattr(paths, "syvrio_home", lambda: tmp_path / ".syvrio")
    result = invoke(["update"])

    assert result.exit_code == 1
    text = result.output + result.stderr
    assert "更新配置缺失" in text
    assert "config.toml" in text


def test_update_missing_executable_is_not_reported_as_missing_config(tmp_path, monkeypatch):
    """uv 等可执行文件缺失属于更新失败，不能被翻译成「配置缺失」。"""
    from syvrio.backend.updates import installer as update_module

    _update_home(tmp_path, monkeypatch, channel="stable", service="syvrio-web.service")

    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "uv")

    monkeypatch.setattr(update_module, "update", missing)
    result = invoke(["update"])

    assert result.exit_code == 1
    text = result.output + result.stderr
    assert "更新配置缺失" not in text
    assert "更新失败" in text and "uv" in text


def test_update_rejects_configuration_without_service(tmp_path, monkeypatch):
    _update_home(tmp_path, monkeypatch, channel="stable")
    result = invoke(["update"])

    assert result.exit_code == 1
    assert "更新配置缺少 service" in result.output + result.stderr


def test_update_forwards_python_and_uv_paths(tmp_path, monkeypatch):
    from syvrio.backend.updates import installer as update_module

    _update_home(
        tmp_path,
        monkeypatch,
        service="syvrio-web.service",
        python="/usr/bin/python3.13",
        uv="/opt/uv/bin/uv",
    )
    seen: dict[str, object] = {}

    def fake(root, channel, service, port, python=None, uv=None, *, prepare_restart, on_progress):
        seen.update(root=root, channel=channel, service=service, port=port, python=python, uv=uv)
        on_progress(update_module.UpdateProgress("check", "Checking PyPI"))
        on_progress(update_module.UpdateProgress("download", "Downloading wheel", 0, 10))
        on_progress(update_module.UpdateProgress("download", "Downloading wheel", 10, 10))
        on_progress(update_module.UpdateProgress("install", "Installing dependencies"))
        return "ok"

    monkeypatch.setattr(update_module, "update", fake)
    result = invoke(["update"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
    assert "Checking PyPI" in result.stderr and "100%" in result.stderr
    assert "Installing dependencies" in result.stderr and "\x1b[" not in result.stderr
    assert seen["uv"] == "/opt/uv/bin/uv"
    assert seen["python"] == "/usr/bin/python3.13"
    assert seen["port"] == 8765
    assert seen["channel"] == "stable"


def test_update_failure_reaches_diagnostics(tmp_path, monkeypatch):
    """fail() 把异常换成 typer.Exit 后原始原因会丢，失败必须在退出前先落诊断事件。"""
    from syvrio.app import cli as cli_module
    from syvrio.backend.updates import installer as update_module

    _update_home(tmp_path, monkeypatch, channel="stable", service="syvrio-web.service")
    recorded: list[tuple[str, object]] = []
    monkeypatch.setattr(
        cli_module,
        "record_event",
        lambda event, error=None, **state: recorded.append((event, error)),
    )

    def broken(*args, **kwargs):
        raise update_module.UpdateError("uv", "uv not found on PATH")

    monkeypatch.setattr(update_module, "update", broken)
    result = invoke(["update"])

    assert result.exit_code == 1
    assert [event for event, _ in recorded] == ["update.failed"]
    assert isinstance(recorded[0][1], update_module.UpdateError)


def test_update_continues_when_progress_reader_closes(tmp_path, monkeypatch):
    import io

    from syvrio.app import ui
    from syvrio.backend.updates import installer as update_module

    class ClosedPipe(io.StringIO):
        def write(self, text):
            raise BrokenPipeError()

    console = ui._UpdateConsole(file=ClosedPipe(), force_terminal=False)
    monkeypatch.setattr(ui, "_UpdateConsole", lambda **kwargs: console)
    _update_home(tmp_path, monkeypatch, service="syvrio-web.service")

    def apply(*args, on_progress, **kwargs):
        on_progress(update_module.UpdateProgress("install", "Installing"))
        return "Verified update"

    monkeypatch.setattr(update_module, "update", apply)
    result = invoke(["update"])
    assert result.exit_code == 0 and result.stdout.strip() == "Verified update"
