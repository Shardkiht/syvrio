from __future__ import annotations

import ipaddress
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import click
import typer

from .. import __version__
from ..backend.client import ResidentClient
from ..backend.ownership import BackendLease
from ..errors import SyvrioError
from ..infra.config import (
    Config,
    default_config_path,
    default_vault_path,
    load_config,
    write_example_config,
)
from ..infra.diagnostics import record_event, start_diagnostics, stop_diagnostics
from ..infra.documents import SUPPORTED_SUFFIXES, DocumentError, read_document
from ..infra.gitops import GitError
from ..memory.extractor import ExtractorError, split_chunks
from ..memory.models import MemoryContext, MemoryDraft
from ..memory.service import MemoryService
from ..session import SESSION_ID_RE, SessionStore
from .session_ui import print_sessions
from .tui import run_chat
from .ui import console, fail, padded, print_hits, styled_table

app = typer.Typer(
    help="Syvrio — Local-first personal AI agent with persistent memory.",
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
)

CONFIG_OPTION = typer.Option(None, "--config", "-c", help="配置文件路径")
VAULT_OPTION = typer.Option(None, "--vault", help="覆盖配置中的记忆库路径")


@dataclass
class _Invocation:
    command: str = "unknown"


@app.callback()
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show Syvrio version", is_eager=True),
) -> None:
    """无子命令时直接进入对话；非交互环境回退到帮助。"""
    if isinstance(ctx.obj, _Invocation):
        ctx.obj.command = ctx.invoked_subcommand or "chat"
    if version:
        typer.echo(f"Syvrio {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is not None:
        return
    if not sys.stdin.isatty():
        console.print(ctx.get_help())
        return
    service = _service(None, None)
    try:
        run_chat(service)
    except Exception as exc:
        record_event("tui.failed", exc)
        console.print(padded(f"[#b8b8b8]无法启动对话界面：{exc}[/]"))
        console.print(ctx.get_help())


def _service(config: Path | None, vault: Path | None) -> ResidentClient:
    return ResidentClient(config, vault)


@app.command("verify-attachments")
def verify_attachments(
    vault: Annotated[Path, typer.Option("--vault", exists=True, file_okay=False)],
) -> None:
    """核对附件迁移结果。检查会话清单、文件大小和 SHA-256。"""
    checked, errors = SessionStore(vault.resolve()).verify_attachments()
    for detail in errors:
        typer.echo(detail, err=True)
    typer.echo(f"Verified {checked} attachment(s), {len(errors)} error(s).")
    if errors:
        raise typer.Exit(code=1)


@app.command()
def update() -> None:
    """从 PyPI 更新受管理的 Syvrio Web 服务。"""
    import tomllib

    from ..infra.paths import syvrio_home
    from ..infra.update import update as apply_update

    root = syvrio_home() / "update"
    config_path = root / "config.toml"
    try:
        with config_path.open("rb") as file:
            config = tomllib.load(file)
    except FileNotFoundError:
        fail(f"更新配置缺失（{config_path}），请先按 Web 文档配置受管理的服务。")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        record_event("update.failed", exc)
        typer.echo(f"更新失败：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not config.get("service"):
        fail("更新配置缺少 service，请先按 Web 文档配置受管理的服务。")
    # Only the config lookup above owns the "missing configuration" wording: a FileNotFoundError
    # raised while updating means a missing executable (uv, previously the service binary), and
    # must not be reported as an unconfigured service.
    try:
        result = apply_update(
            root,
            config.get("channel", "stable"),
            config["service"],
            config.get("port", 8765),
            config.get("python"),
            config.get("uv"),
        )
    except SyvrioError as exc:
        # Deliberate, staged failure (missing uv, broken venv, failed switch…):
        # the message already tells the user what to do.
        record_event("update.failed", exc)
        typer.echo(f"更新失败：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        # Defense in depth for anything the updater does not stage itself; the
        # full traceback lands in the diagnostics log via update.failed.
        record_event("update.failed", exc)
        typer.echo(f"更新失败：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(result)


@app.command()
def init(
    vault: Annotated[Path | None, typer.Option("--vault", help="记忆库目录")] = None,
    config: Path | None = CONFIG_OPTION,
    force: bool = typer.Option(False, "--force", help="目标目录非空且不是记忆库时仍然继续"),
) -> None:
    """初始化记忆库与 Git 仓库。"""
    vault = (vault if vault is not None else default_vault_path()).expanduser().resolve()
    if vault.exists() and any(vault.iterdir()) and not (vault / ".git").exists() and not force:
        fail(f"{vault} 非空且不是 Syvrio 记忆库；确认无误后加 --force。")
    config_path = config.expanduser() if config else default_config_path()
    cfg = load_config(config_path) if config_path.exists() else Config(vault=vault)
    vault_changed = cfg.vault.expanduser().resolve() != vault
    cfg.vault = vault
    with BackendLease(cfg.paths(config_path)):
        result = MemoryService(cfg, config_path=config_path).init_vault()
    if not config_path.exists():
        write_example_config(config_path, vault)
    elif vault_changed:
        # 已有配置也要跟随 --vault，否则下条命令仍写回旧库。
        from ..infra.providers import write_config

        write_config(config_path, cfg)
    console.print(f"记忆库已就绪：{result['vault']}")
    console.print(f"配置文件：{config_path}")
    if result["commit"]:
        console.print(f"初始提交：{result['commit']}")


@app.command()
def setup(
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """交互式配置模型、搜索与网页抓取 API。"""
    from ..infra.providers import run_setup

    try:
        from ..backend.endpoint import client_paths

        _, paths = client_paths(config, vault)
        with BackendLease(paths):
            result = run_setup(config, vault)
    except KeyboardInterrupt:
        fail("已取消。")
        return
    console.print(f"配置已保存：{result['config_path']}")
    if result.get("mode") == "search":
        console.print(f"Search providers：{result['search_providers']}")
        console.print(f"Fetch providers：{result['fetch_providers']}")
        for message in result["messages"]:
            console.print(message)
        return
    console.print(
        f"供应商：{result['base_url']} · 模型：{result['model']} · provider：{result['provider']}"
    )
    console.print(
        f"上下文：{result['context_window']} · 思考强度：{result['reasoning_effort'] or '默认'}"
    )
    if result["key_saved"]:
        console.print(f"[bold]API Key 已保存到系统凭据[/]（环境变量 {result['api_key_env']} 优先）")
    elif result["keyring_error"]:
        console.print(f"[#b8b8b8]{result['keyring_error']}[/]")
    if result["checked"]:
        console.print(f"[bold]连接测试：{result['check_detail']}[/]")
    elif result["base_url"]:
        console.print(f"[dim]{result['check_detail']}[/dim]")
    if result["initialized"]:
        console.print(f"[bold]记忆库已初始化：{result['vault']}[/]")


@app.command()
def chat(
    resume: str | None = typer.Option(None, "--resume", "-r", help="恢复指定会话 ID"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """启动交互式记忆 Agent。支持 /new /sessions /compact 等命令。"""
    run_chat(_service(config, vault), resume)


@app.command("sessions")
def list_sessions(
    limit: int = typer.Option(20, "--limit", "-n"),
    archived: bool = typer.Option(False, "--archived", help="列出已归档的会话"),
    archive: str | None = typer.Option(None, "--archive", metavar="ID", help="将会话移入归档"),
    restore: str | None = typer.Option(None, "--restore", metavar="ID", help="还原已归档的会话"),
    purge: str | None = typer.Option(
        None, "--delete-permanently", metavar="ID", help="确认后永久删除已归档的会话"
    ),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """列出、归档或还原会话。"""
    if sum(value is not None for value in (archive, restore, purge)) + archived > 1:
        raise typer.BadParameter("请只选择一个会话操作。")
    target = next((value for value in (archive, restore, purge) if value is not None), None)
    if target is not None and not SESSION_ID_RE.fullmatch(target):
        raise typer.BadParameter("无效的会话 ID。")
    service = _service(config, vault)
    if target is None:
        print_sessions(
            service.call("sessions", limit=limit, archived=True)
            if archived
            else service.call("sessions", limit=limit)
        )
        return
    path = f"/api/sessions/{target}"
    try:
        if purge is not None:
            preview = service.request("GET", path + "/deletion-preview")
            for item in preview["pending"]:
                typer.echo(f"将同时移除候选 {item['id']}  {item['text']}")
            if not typer.confirm(f"永久删除已归档的会话 {target} 及其附件？", default=False):
                return
            service.request(
                "DELETE",
                path,
                params={
                    "permanent": True,
                    "confirmed": True,
                    "pending_revision": preview["revision"],
                },
            )
            typer.echo(f"已永久删除 {target}")
        else:
            operation = "archive" if archive is not None else "restore"
            service.request("POST", f"{path}/{operation}")
            typer.echo(f"{'已归档' if archive is not None else '已还原'} {target}")
    except (KeyError, ValueError) as exc:
        fail(str(exc), exc=exc)


@app.command("note")
@app.command()
def memorize(
    text: str = typer.Argument(..., help="已确认的事实原文"),
    topic: str = typer.Option("uncategorized", "--topic", "-t", help="记忆主题"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """保存一条明确事实。"""
    try:
        entry = _service(config, vault).memorize(MemoryDraft(text=text, topic=topic))
        console.print(f"已记录 [{entry.topic}] {entry.text}", markup=False)
    except ValueError as exc:
        fail(str(exc), exc=exc)


@app.command("import")
def import_memories(
    path: Annotated[
        Path, typer.Argument(help="要导入的文本、聊天导出或文档文件/目录", exists=True)
    ],
    apply: bool = typer.Option(False, "--apply", help="实际写入；默认只打印计划"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """导入文本或文档：AI 整理成条目，程序裁决写入；默认 dry-run（仍调用模型）。"""
    files = (
        sorted(
            file
            for file in path.iterdir()
            if file.is_file() and file.suffix.lower() in SUPPORTED_SUFFIXES
        )
        if path.is_dir()
        else [path]
    )
    if not files:
        fail("没有找到支持导入的文件；目录只扫描当前层。")
        return
    service = _service(config, vault)
    totals = {"admitted": 0, "queued": 0, "duplicates": 0, "blocked": 0}
    failed_chunks = 0
    total_chunks = 0
    failed_files = 0
    for file in files:
        try:
            chunks = split_chunks(read_document(file))
        except DocumentError as exc:
            failed_files += 1
            console.print(f"文件解析失败：{file.name}：{exc}", markup=False)
            continue
        file_failed = 0
        for chunk_index, chunk in enumerate(chunks, 1):
            total_chunks += 1
            try:
                result = service.import_text(
                    chunk, source=f"import:{file.name}#chunk:{chunk_index}", dry_run=not apply
                )
            except ExtractorError as exc:
                file_failed += 1
                console.print(f"[#b8b8b8]块抽取失败：{exc}[/]")
                continue
            totals["admitted"] += len(result.admitted)
            totals["queued"] += len(result.queued)
            totals["duplicates"] += len(result.duplicates)
            totals["blocked"] += len(result.blocked)
        failed_chunks += file_failed
        note = f"，失败 {file_failed} 块" if file_failed else ""
        console.print(f"{file.name}：{len(chunks)} 块{note}", markup=False)
    if not total_chunks or failed_chunks == total_chunks:
        fail("所有块抽取失败，未写入任何内容。")
        return
    mode = "已写入" if apply else "计划写入（dry-run，确认无误后加 --apply 落盘）"
    console.print(
        f"[bold]{mode}[/]：直写 {totals['admitted']} · 待确认 {totals['queued']} · "
        f"重复 {totals['duplicates']} · 拦截 {totals['blocked']}"
    )
    if apply and totals["queued"]:
        console.print("运行 syvrio pending 查看待确认条目。")
    if failed_files or failed_chunks:
        console.print(
            f"部分导入失败：{failed_files} 个文件、{failed_chunks} 个文本块；已处理部分不会回滚。"
        )
        raise typer.Exit(code=1)


@app.command()
def recall(
    query: str = typer.Argument(..., help="检索关键词"),
    limit: int | None = typer.Option(None, "--limit", "-n", min=1, help="最多返回条数"),
    date_from: str | None = typer.Option(None, "--from", help="事件起始日期：年、年月或年月日"),
    date_to: str | None = typer.Option(None, "--to", help="事件截止日期"),
    location: str | None = typer.Option(None, "--location", help="明确记载的地点"),
    emotion: str | None = typer.Option(None, "--emotion", help="明确表达的情绪"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """检索记忆。"""
    try:
        hits = _service(config, vault).recall(
            query,
            limit,
            time_range={"start": date_from, "end": date_to} if date_from or date_to else None,
            location=location,
            emotion=emotion,
        )
    except ValueError as exc:
        fail(str(exc), exc=exc)
        return
    print_hits(query, hits)


@app.command("evolve", hidden=True)
@app.command()
def transition(
    entry_id: str = typer.Argument(...),
    text: str = typer.Argument(..., help="变化后的事实"),
    topic: str = typer.Option(..., "--topic", "-t", help="新状态的主题"),
    effective_from: str | None = typer.Option(None, "--from", help="新状态的起始日期"),
    previous_valid_to: str | None = typer.Option(None, "--previous-until", help="旧状态的截止日期"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """记录已发生的状态变化，保留历史；原先记错请使用 correct。"""
    try:
        context = MemoryContext(valid_from=effective_from) if effective_from else None
        draft = MemoryDraft(text=text, topic=topic, context=context)
        entry = _service(config, vault).transition(entry_id, draft, previous_valid_to)
        console.print(f"已记录时间变化：{entry.id}")
    except (ValueError, KeyError) as exc:
        fail(str(exc), exc=exc)


@app.command("profile")
def profile(
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """查看最近一次 Dream 生成的画像。"""
    console.print(
        _service(config, vault).call("profile") or "暂无画像，可运行 syvrio dream。", markup=False
    )


@app.command("dream")
def dream(
    requests: int | None = typer.Option(None, "--requests", min=0, max=20, help="本次模型请求上限"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """增量回顾有效事实，生成待审查假设；默认使用配置中的请求预算。"""
    job = _service(config, vault).call("dream", max_requests=requests)
    console.print(f"已提交 Dream 任务 {job}", markup=False)


@app.command("inferences")
def inferences(
    inference_id: str | None = typer.Argument(None),
    action: str | None = typer.Option(None, "--action", help="confirm / reject / correct"),
    text: str | None = typer.Option(None, "--text", help="更正后的假设内容"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """查看或审查推断；确认不会自动写入事实记忆。"""
    service = _service(config, vault)
    try:
        if action:
            if not inference_id:
                raise ValueError("操作时必须提供推断 ID。")
            items = [service.review_inference(inference_id, action, text)]
        else:
            items = [
                item
                for item in service.list_inferences()
                if not inference_id or item.id == inference_id
            ]
        for item in items:
            console.print(item.model_dump_json(), markup=False)
    except (KeyError, ValueError) as exc:
        fail(str(exc), exc=exc)


@app.command()
def pending(
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """列出待确认候选。"""
    items = _service(config, vault).pending()
    if not items:
        console.print("[dim]没有待确认项。[/dim]")
        return
    table = styled_table("待确认")
    table.add_column("ID")
    table.add_column("主题")
    table.add_column("待确认问题")
    table.add_column("关联记忆")
    table.add_column("内容", overflow="fold")
    for item in items:
        table.add_row(item.id, item.topic, item.question, ", ".join(item.related_ids), item.text)
    console.print(table)


@app.command()
def approve(
    item_id: str = typer.Argument(..., help="待确认项 ID"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """确认一条候选并写入记忆库。"""
    try:
        entry = _service(config, vault).approve(item_id)
    except KeyError as exc:
        fail(str(exc), exc=exc)
        return
    except ValueError as exc:
        fail(str(exc), exc=exc)
        return
    console.print(f"[bold]已写入[/] [{entry.topic}] {entry.text}")


@app.command()
def reject(
    item_id: str = typer.Argument(..., help="待确认项 ID"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """拒绝一条待确认候选，保留处理记录。"""
    try:
        item = _service(config, vault).reject(item_id)
    except KeyError as exc:
        fail(str(exc), exc=exc)
        return
    console.print(f"[dim]已拒绝[/dim] {item.id} {item.text}")


@app.command()
def correct(
    entry_id: str = typer.Argument(..., help="要更正的记忆 ID"),
    text: str = typer.Argument(..., help="更正后的完整表述"),
    topic: str = typer.Option(..., "--topic", "-t", help="更正后的主题"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """写入更正后的新条目并将原记忆标为弃用。"""
    try:
        entry = _service(config, vault).correct(entry_id, MemoryDraft(text=text, topic=topic))
    except KeyError as exc:
        fail(str(exc), exc=exc)
        return
    except ValueError as exc:
        fail(str(exc), exc=exc)
        return
    console.print(f"[bold]已更正[/] [{entry.topic}] {entry.text}")
    console.print(f"[dim]旧条目 {entry_id} 已弃用，审计已记录[/dim]")


@app.command("forget", hidden=True)
@app.command()
def deprecate(
    entry_id: str = typer.Argument(..., help="要弃用的记忆 ID"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """弃用一条记忆，保留 ID、原文、来源和审计记录。"""
    try:
        entry = _service(config, vault).deprecate(entry_id)
    except KeyError as exc:
        fail(str(exc), exc=exc)
        return
    except ValueError as exc:
        fail(str(exc), exc=exc)
        return
    console.print(f"[bold]已弃用[/] {entry.id} [{entry.topic}] {entry.text}")


@app.command("topics")
def list_topics(
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """列出记忆主题。"""
    topics = _service(config, vault).topics()
    if not topics:
        console.print("[dim]记忆库为空。[/dim]")
        return
    table = styled_table("记忆主题")
    table.add_column("主题")
    table.add_column("标题")
    table.add_column("条目")
    table.add_column("更新")
    for topic in topics:
        table.add_row(topic["topic"], topic["title"], str(topic["entries"]), topic["updated"])
    console.print(table)


@app.command()
def history(
    limit: int = typer.Option(10, "--limit", "-n"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """查看提交历史。"""
    commits = _service(config, vault).history(limit)
    if not commits:
        console.print("[dim]暂无提交。[/dim]")
        return
    table = styled_table("提交历史")
    table.add_column("提交")
    table.add_column("时间")
    table.add_column("摘要", overflow="fold")
    for commit in commits:
        table.add_row(commit["hash"], commit["date"], commit["subject"])
    console.print(table)


@app.command()
def commit(
    push: bool | None = typer.Option(
        None, "--push/--no-push", help="是否推送到 remote；默认读取 auto_push"
    ),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """生成总结并提交变更，供本人审阅。"""
    service = _service(config, vault)
    try:
        result = service.commit(push=push)
    except GitError as exc:
        fail(str(exc), exc=exc)
        return
    if not result["committed"]:
        console.print(f"[dim]{result['summary']}[/dim]")
        if result.get("pushed"):
            console.print("[bold]已推送[/]")
        elif result.get("push_error"):
            console.print(f"[#b8b8b8]推送未成功：{result['push_error']}[/]")
        return
    console.print("[bold]本次提交总结[/bold]")
    console.print(result["summary"])
    console.print(f"提交：{result['hash']}")
    if result["pushed"]:
        console.print("[bold]已推送[/]")
    elif result.get("push_error"):
        console.print(f"[#b8b8b8]本地提交已完成，推送未成功：{result['push_error']}[/]")


@app.command()
def status(
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """查看记忆库状态。"""
    info = _service(config, vault).status()
    console.print(f"记忆库：{info['vault']}")
    console.print(
        f"主题 {info['topics']} · 条目 {info['entries']} · 待确认 {info['pending']} · 未提交 {info['dirty']}"
    )
    console.print(f"检索 {info['retrieval']}")


@app.command()
def serve(
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """以 stdio 启动 MCP server。"""
    service = _service(config, vault)
    from .server import build_server

    build_server(service=service).run()


@app.command("backend")
def backend_command(
    action: str = typer.Argument("status", help="run / start / status / stop"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """查看、启动或关闭此 Vault 的常驻后端。"""
    from ..backend.endpoint import client_paths, connect, discover

    _, paths = client_paths(config, vault)
    if action == "run":
        from ..backend.server import serve_resident

        serve_resident(config, paths.vault_dir)
    elif action == "start":
        base, _ = connect(paths)
        typer.echo(f"Running: {base}")
    elif action == "status":
        found = discover(paths)
        typer.echo(f"Running: {found[0]}" if found else "Stopped")
    elif action == "stop":
        if found := discover(paths):
            import httpx

            with httpx.Client(trust_env=False, timeout=10) as client:
                response = client.post(
                    found[0] + "/api/resident/shutdown",
                    headers={"X-Syvrio-Token": found[1]},
                    json={},
                )
                response.raise_for_status()
            typer.echo("Shutdown requested.")
        else:
            typer.echo("Stopped")
    else:
        fail("Choose run, start, status or stop.")


@app.command()
def web(
    action: str | None = typer.Argument(None, help="share：通过 ngrok 和 GitHub 登录分享 Web"),
    github_id: str | None = typer.Option(
        None, "--github-id", help="share：唯一允许的 GitHub 数字账号 ID"
    ),
    yes: bool = typer.Option(False, "--yes", help="share：确认开放指定域名，适用于服务启动"),
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址；默认仅 loopback"),
    port: int = typer.Option(
        8765, "--port", envvar="SYVRIO_WEB_PORT", min=1, max=65535, help="服务端口 / 远程访问端口"
    ),
    domain: str = typer.Option("", "--domain", help="本地域名；写入 hosts 映射并放行 Host/Origin"),
    domain_ip: str = typer.Option(
        "127.0.0.1", "--domain-ip", help="域名映射目标 IP；远程 Tailscale 填 100.x.y.z"
    ),
    map_only: bool = typer.Option(
        False, "--map-only", help="只写 hosts 映射并打印地址，不启动本机服务（远程访问）"
    ),
    remove_domain: bool = typer.Option(False, "--remove-domain", help="删除该域名的 hosts 映射"),
    map_hosts: bool = typer.Option(True, "--hosts/--no-hosts", help="是否自动更新系统 hosts 文件"),
    config: Path | None = CONFIG_OPTION,
    vault: Path | None = VAULT_OPTION,
) -> None:
    """启动私有 Web 客户端，或为本机/远程实例配置本地域名映射。"""
    from .web.hosts import ensure_hosts_mapping, normalize_domain, remove_hosts_mapping
    from .web.server import run_server

    if action == "share":
        from .web.share import start_share

        if host != "127.0.0.1" or map_only or remove_domain or domain_ip != "127.0.0.1":
            fail("web share 仅使用 127.0.0.1 网关，无需 hosts 映射。")
        try:
            start_share(port, domain, github_id, yes, config, vault)
        except SyvrioError as exc:
            fail(str(exc), exc=exc)
        return
    if action is not None or github_id is not None or yes:
        fail("使用 syvrio web 或 syvrio web share --help。")
    domains: list[str] = []
    if domain.strip() or map_only or remove_domain:
        try:
            name = normalize_domain(domain)
            ipaddress.ip_address(domain_ip)
        except ValueError as exc:
            fail(str(exc), exc=exc)
            return
        domains.append(name)
        if remove_domain:
            typer.echo(f"[hosts] {remove_hosts_mapping(name, domain_ip).message}")
            return
        if map_hosts:
            typer.echo(f"[hosts] {ensure_hosts_mapping(name, domain_ip).message}")
        else:
            typer.echo(f"[hosts] Add manually: {domain_ip}\t{name}")
        suffix = "" if port == 80 else f":{port}"
        typer.echo(f"Web URL: http://{name}{suffix}/")
        if domain_ip != "127.0.0.1":
            typer.echo(
                "Remote note: the remote host must allow this hostname in "
                "SYVRIO_WEB_ALLOWED_HOSTS. If it uses Tailscale Serve HTTPS, "
                "open https://<machine>.<tailnet>.ts.net/ instead (a local alias "
                "will not match the Tailscale certificate)."
            )
        if map_only:
            return
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if not loopback:
        typer.echo(
            "Warning: a non-loopback --host exposes the Web API without authentication. "
            "Host/Origin checks only block cross-site requests, not direct network access. "
            "Restrict the network (e.g. Tailscale) or keep --host 127.0.0.1."
        )
    try:
        run_server(host, port, config_path=config, vault_path=vault, domains=domains)
    except RuntimeError as exc:
        fail(str(exc), exc=exc)


def main() -> None:
    # Windows pipes use the locale code page, which may not encode CLI help.
    # Keep native console handling intact; JSON/MCP and redirected CLI use UTF-8.
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if not stream.isatty() and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    config_file = None
    arguments = sys.argv[1:]
    for index, argument in enumerate(arguments):
        if argument == "--":
            break
        if argument in {"--config", "-c"} and index + 1 < len(arguments):
            config_file = Path(arguments[index + 1])
        elif argument.startswith("--config="):
            config_file = Path(argument.split("=", 1)[1])
    start_diagnostics(config_file)
    invocation = _Invocation()
    try:
        app(obj=invocation)
    except (typer.Exit, click.Abort):
        # Deliberate exits (fail(), --version, Ctrl+C on a prompt) are control
        # flow, not failures; recording them hides the real cause. fail() with
        # exc= already recorded cli.failed before raising.
        raise
    except Exception as exc:
        record_event("cli.failed", exc, command=invocation.command)
        raise
    finally:
        stop_diagnostics()


if __name__ == "__main__":
    main()
