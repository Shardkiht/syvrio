from __future__ import annotations

from ..agent.runtime import AgentRuntime
from ..agent.session import SessionStore
from ..llm import LiteLLMChatModel
from ..memory.service import MemoryService
from .chat_app import ChatApp
from .ui import console, fail


def run_chat(service: MemoryService, resume: str | None = None) -> None:
    if service.config.llm.requires_api_key() and not service.config.llm.api_key():
        fail(
            f"未配置 LLM API Key，请设置环境变量 {service.config.llm.api_key_env}，"
            "或先在终端运行 syvrio setup。"
        )
    if not (service.config.vault / ".git").exists():
        service.init_vault()
    # Web and TUI must not write the same session concurrently. The marker is a
    # whole-process file lock, released by the OS even after a crash.
    from ..infra.locking import LockTimeout, client_instance_lock

    client_lock = client_instance_lock(service.paths)
    try:
        client_lock.acquire(timeout=0)
    except (LockTimeout, OSError):
        fail("另一个 Syvrio 客户端（Web 或 TUI）正在使用此记忆库；请先关闭它。")
        return
    try:
        sessions = SessionStore(service.paths)
        runtime = AgentRuntime(service, LiteLLMChatModel(service.config.llm), sessions)
        if resume:
            try:
                runtime.resume(resume)
            except KeyError as exc:
                fail(str(exc))
                return
        else:
            runtime.start()
        app = ChatApp(runtime, service, sessions)
        try:
            app.run()
        finally:
            service.reflection.cancel()
            runtime.stop_catchup()
            # 界面退出后回到普通终端，再执行退出抽取并汇报结果。
            stats = runtime.close()
            _print_exit_stats(runtime, stats)
    finally:
        client_lock.release()


def _print_exit_stats(runtime: AgentRuntime, stats: dict) -> None:
    if runtime.has_session:
        console.print(f"[dim]会话已保存：{runtime.current.id}[/dim]")
    else:
        console.print("[dim]未发送消息，没有创建会话。[/dim]")
    if stats.get("error"):
        console.print(f"[dim]退出抽取失败：{stats['error']}[/dim]")
    elif any(stats.get(key) for key in ("admitted", "queued", "blocked")):
        console.print(
            f"[dim]退出时沉淀记忆：写入 {stats['admitted']} 条，"
            f"待确认 {stats['queued']} 条，拦截 {stats['blocked']} 条[/dim]"
        )
