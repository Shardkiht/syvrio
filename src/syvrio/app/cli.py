from __future__ import annotations

import ipaddress
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import click
import typer

from .. import __version__
from ..backend import maintenance
from ..backend.client import ResidentClient
from ..backend.client_contracts import SESSION_ID_RE, SUPPORTED_SUFFIXES, MemoryContext, MemoryDraft
from ..backend.diagnostics import start_diagnostics
from ..infra.errors import SyvrioError
from ..infra.observability.diagnostics import record_event, stop_diagnostics
from .session_ui import print_sessions
from .tui.launcher import run_chat
from .ui import console, fail, padded, print_hits, styled_table

app = typer.Typer(
    help="Syvrio — Local-first personal AI agent with persistent memory.",
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
)

CONFIG_OPTION = typer.Option(None, "--config", "-c", help="配置文件路径")


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
    service = _service(None)
    try:
        run_chat(service)
    except Exception as exc:
        record_event("tui.failed", exc)
        console.print(padded(f"[#b8b8b8]无法启动对话界面：{exc}[/]"))
        console.print(ctx.get_help())


def _service(config: Path | None) -> ResidentClient:
    return ResidentClient(config)


@app.command()
def backup(destination: Path, config: Path | None = CONFIG_OPTION):
    """Copy a complete offline database and its external originals into a new directory."""
    typer.echo(maintenance.backup_database(config, destination))


@app.command()
def restore(source: Path, destination: Path):
    """Verify a complete backup and restore into a new Vault directory."""
    typer.echo(maintenance.restore_database(source, destination))


@app.command("verify-attachments")
def verify_attachments(
    vault: Annotated[Path, typer.Option("--vault")],
) -> None:
    """按会话清单核验附件的文件大小和 SHA-256。"""
    try:
        checked, errors = maintenance.verify_local_attachments(vault)
    except maintenance.MaintenanceError as exc:
        fail(str(exc), exc=exc)
    for detail in errors:
        typer.echo(detail, err=True)
    typer.echo(f"Verified {checked} attachment(s), {len(errors)} error(s).")
    if errors:
        raise typer.Exit(code=1)


@app.command()
def update() -> None:
    """从 PyPI 更新受管理的 Syvrio Web 服务。"""
    from .ui import show_update_progress

    try:
        with show_update_progress() as display:
            result = maintenance.update_managed(on_progress=display)
    except Exception as exc:
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
    try:
        result = maintenance.initialize_configured_vault(config, vault, force=force)
    except maintenance.MaintenanceError as exc:
        fail(str(exc), exc=exc)
    console.print(f"记忆库已就绪：{result['vault']}")
    console.print(f"配置文件：{result['config_path']}")
    if result["commit"]:
        console.print(f"初始提交：{result['commit']}")


@app.command()
def setup(
    config: Path | None = CONFIG_OPTION,
) -> None:
    """交互式配置模型、搜索与网页抓取 API。"""
    from .setup import run_setup

    try:
        result = run_setup(config)
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
        f"上下文：{result['context_window']} · 思考档位：{result['thinking_selection'] or '默认'}"
    )
    if result["key_saved"]:
        console.print("[bold]API Key 已保存到系统凭据[/]")
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
) -> None:
    """启动交互式记忆 Agent。支持 /new /sessions /compact 等命令。"""
    run_chat(_service(config), resume)


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
) -> None:
    """列出、归档或还原会话。"""
    if sum(value is not None for value in (archive, restore, purge)) + archived > 1:
        raise typer.BadParameter("请只选择一个会话操作。")
    target = next((value for value in (archive, restore, purge) if value is not None), None)
    if target is not None and not SESSION_ID_RE.fullmatch(target):
        raise typer.BadParameter("无效的会话 ID。")
    service = _service(config)
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
) -> None:
    """保存一条明确事实。"""
    try:
        entry = _service(config).memorize(MemoryDraft(text=text, topic=topic))
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
) -> None:
    """导入文本或文档：AI 整理成条目，程序裁决写入；默认 dry-run（仍调用模型）。"""
    try:
        files = (
            sorted(
                file
                for file in path.iterdir()
                if file.is_file() and file.suffix.lower() in SUPPORTED_SUFFIXES
            )
            if path.is_dir()
            else [path]
        )
        result = _service(config).import_files(files, apply)
    except (OSError, ValueError) as exc:
        fail(str(exc), exc=exc)
    for file, chunks in result["file_chunks"]:
        console.print(f"{file}：{chunks} 块", markup=False)
    for message in result["errors"]:
        console.print(message, markup=False)
    if not result["chunks"]:
        fail("所有块抽取失败，未写入任何内容。")
    mode = "已写入" if apply else "计划写入（dry-run，确认无误后加 --apply 落盘）"
    console.print(
        f"[bold]{mode}[/]：直写 {result['saved']} · 待确认 {result['queued']} · "
        f"重复 {result['duplicates']} · 拦截 {result['blocked']}"
    )
    if apply and result["queued"]:
        console.print("运行 syvrio pending 查看待确认条目。")
    if result["errors"]:
        console.print(
            f"部分导入失败：{result['failed_files']} 个文件、{result['failed_chunks']} 个文本块；已处理部分不会回滚。"
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
) -> None:
    """检索记忆。"""
    try:
        hits = _service(config).recall(
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
) -> None:
    """记录已发生的状态变化，保留历史；原先记错请使用 correct。"""
    try:
        context = MemoryContext(valid_from=effective_from) if effective_from else None
        draft = MemoryDraft(text=text, topic=topic, context=context)
        entry = _service(config).transition(entry_id, draft, previous_valid_to)
        console.print(f"已记录时间变化：{entry.id}")
    except (ValueError, KeyError) as exc:
        fail(str(exc), exc=exc)


@app.command("profile")
def profile(
    config: Path | None = CONFIG_OPTION,
) -> None:
    """查看最近一次 Dream 生成的画像。"""
    console.print(
        _service(config).call("profile") or "暂无画像，可运行 syvrio dream。", markup=False
    )


class DreamAction(StrEnum):
    run = "run"
    status = "status"


@app.command("dream")
def dream(
    action: Annotated[DreamAction, typer.Argument()] = DreamAction.run,
    config: Path | None = CONFIG_OPTION,
) -> None:
    """更新画像并生成待审推断，或查看 Dream 状态。"""
    result = _service(config).call("dream", action=action.value)
    if result["status"] != "ok":
        fail(result["message"])
    console.print(result["message"], markup=False)


@app.command("inferences")
def inferences(
    inference_id: str | None = typer.Argument(None),
    action: str | None = typer.Option(None, "--action", help="confirm / reject / correct"),
    text: str | None = typer.Option(None, "--text", help="更正后的假设内容"),
    config: Path | None = CONFIG_OPTION,
) -> None:
    """查看或审查推断；确认不会自动写入事实记忆。"""
    service = _service(config)
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
) -> None:
    """列出待确认候选。"""
    items = _service(config).pending()
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
) -> None:
    """确认一条候选并写入记忆库。"""
    try:
        entry = _service(config).approve(item_id)
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
) -> None:
    """拒绝一条待确认候选，保留处理记录。"""
    try:
        item = _service(config).reject(item_id)
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
) -> None:
    """写入更正后的新条目并将原记忆标为弃用。"""
    try:
        entry = _service(config).correct(entry_id, MemoryDraft(text=text, topic=topic))
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
) -> None:
    """弃用一条记忆，保留 ID、原文、来源和审计记录。"""
    try:
        entry = _service(config).deprecate(entry_id)
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
) -> None:
    """列出记忆主题。"""
    topics = _service(config).topics()
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
) -> None:
    """查看提交历史。"""
    commits = _service(config).history(limit)
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
) -> None:
    """生成总结并提交变更，供本人审阅。"""
    service = _service(config)
    try:
        result = service.commit(push=push)
    except ValueError as exc:
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
) -> None:
    """查看记忆库状态。"""
    info = _service(config).status()
    console.print(f"记忆库：{info['vault']}")
    console.print(
        f"主题 {info['topics']} · 条目 {info['entries']} · 待确认 {info['pending']} · 未提交 {info['dirty']}"
    )
    console.print(f"检索 {info['retrieval']}")


@app.command()
def serve(
    config: Path | None = CONFIG_OPTION,
) -> None:
    """以 stdio 启动 MCP server。"""
    service = _service(config)
    from .server import build_server

    build_server(client=service).run()


@app.command("backend")
def backend_command(
    action: str = typer.Argument("status", help="run / start / status / stop"),
    config: Path | None = CONFIG_OPTION,
) -> None:
    """查看、启动或关闭此 Vault 的常驻后端。"""
    from ..backend.endpoint import client_paths, connect, discover

    if action == "run":
        from ..backend.server import serve_resident

        serve_resident(config)
        return
    paths = client_paths(config)
    if action == "start":
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
) -> None:
    """启动私有 Web 客户端，或为本机/远程实例配置本地域名映射。"""
    from ..backend.hosts import ensure_hosts_mapping, normalize_domain, remove_hosts_mapping
    from .web.server import run_server

    if action == "share":
        from .web.share import start_share

        if host != "127.0.0.1" or map_only or remove_domain or domain_ip != "127.0.0.1":
            fail("web share 仅使用 127.0.0.1 网关，无需 hosts 映射。")
        try:
            start_share(port, domain, github_id, yes, config)
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
        run_server(host, port, config_path=config, domains=domains)
    except RuntimeError as exc:
        fail(str(exc), exc=exc)


def main() -> None:
    # Windows 管道默认使用本地代码页，可能无法输出帮助文本。
    # 保留原生控制台处理，JSON、MCP 和重定向的 CLI 输出使用 UTF-8。
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
        # 主动退出（fail()、--version 或在提示时按 Ctrl+C）属于正常控制流程。
        # 调用 fail() 时若传入 exc，原始错误已在退出前记入 cli.failed，重复记录会掩盖原因。
        raise
    except Exception as exc:
        record_event("cli.failed", exc, command=invocation.command)
        raise
    finally:
        stop_diagnostics()


if __name__ == "__main__":
    main()
