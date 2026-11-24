"""共享的斜杠命令目录和独立于前端的命令分发。

此模块独立于 Textual、Rich、HTTP 和网页框架。两个客户端共用命令名称、用法与能力标记，
各自负责呈现。CommandUseCases 复用应用层已有用例，
智能体、记忆和会话逻辑由各自模块负责。
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..infra.config import MIN_CONTEXT_WINDOW
from ..infra.observability.diagnostics import record_event
from ..infra.session.errors import ConversationError
from ..infra.storage.transaction import vault_transaction
from ..memory.turn_undo import TurnUndoError
from .command_catalog import SLASH_COMMANDS, SLASH_SUBCOMMANDS, command_names, parse_command

if TYPE_CHECKING:
    from .service import ApplicationService

REVIEW_ACTIONS = {
    "pending": frozenset({"approve", "reject", "edit"}),
    "inferences": frozenset({"confirm", "reject", "correct", "edit"}),
}
MODEL_REFRESH = frozenset({"refresh", "--refresh", "-r"})


def is_command_form(name: str, argument: str) -> bool:
    """在确认、调度或触发客户端操作前校验固定命令格式。"""
    if name not in command_names():
        return False
    argument = argument.strip()
    if not argument or name in {"rename", "topics", "think", "context"}:
        return True
    if name == "model":
        provider, _, model = argument.partition("/")
        return argument.lower() in MODEL_REFRESH or bool(provider and model)
    if name == "dream":
        return argument.lower() in {"run", "status"}
    if name == "git":
        return argument.lower() in {"sync", "status"}
    if name in REVIEW_ACTIONS:
        parts = argument.split(maxsplit=2)
        action = parts[0].lower()
        return (
            action in REVIEW_ACTIONS[name]
            and len(parts) >= 2
            and (len(parts) == 2 or action in {"edit", "correct"})
        )
    return False


# 网页端能力标记
# support 为 server 时由后端执行，client 表示仅由浏览器处理，
# unsupported 必须向用户说明原因。
# 需要确认的操作先由浏览器询问用户，后端在产生任何副作用前再次独立核验确认标记。

SERVER = "server"
CLIENT = "client"
UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class WebCapability:
    support: str
    intent: str = ""
    confirm: bool = False
    note: str = ""
    shortcut: str = ""


WEB_CAPABILITIES: dict[str, WebCapability] = {
    "new": WebCapability(SERVER, "new_session"),
    "sessions": WebCapability(CLIENT, "open_sessions", shortcut="Ctrl+B / Alt+B"),
    "compact": WebCapability(SERVER, "compact"),
    "undo": WebCapability(
        SERVER, "undo", note="Stops generation and restores the last turn for editing."
    ),
    "summary": WebCapability(SERVER, "summary"),
    "status": WebCapability(SERVER, "status"),
    "git": WebCapability(
        SERVER, "git_status", note="Status by default; /git sync runs a sync after confirmation."
    ),
    "model": WebCapability(SERVER, "model", shortcut="Alt+M"),
    "think": WebCapability(SERVER, "think"),
    "context": WebCapability(SERVER, "context"),
    "setup": WebCapability(
        CLIENT,
        "open_settings",
        note="Opens the Web settings panel for providers, keys and endpoints.",
    ),
    "rename": WebCapability(SERVER, "rename"),
    "update": WebCapability(CLIENT, "open_update"),
    "import": WebCapability(CLIENT, "pick_attachments", note="Choose files on this device."),
    "pending": WebCapability(
        SERVER, "pending", note="List, approve or reject: /pending approve|reject|edit <id>."
    ),
    "inferences": WebCapability(
        SERVER,
        "inferences",
        note="List, confirm or reject: /inferences confirm|reject|correct <id>.",
    ),
    "profile": WebCapability(
        SERVER,
        "profile",
        note="View the latest generated profile. Use /dream to request regeneration.",
    ),
    "dream": WebCapability(SERVER, "dream", note="Run Dream or check its status."),
    "topics": WebCapability(SERVER, "topics"),
    "help": WebCapability(SERVER, "help", shortcut="Ctrl+Shift+P / Alt+P"),
    "exit": WebCapability(
        CLIENT, "leave", note="Returns to the Web entry view; never stops the background server."
    ),
    "quit": WebCapability(
        CLIENT, "leave", note="Returns to the Web entry view; never stops the background server."
    ),
}


@dataclass(frozen=True)
class WebCommandInfo:
    name: str
    usage: str
    description: str
    support: str
    intent: str
    confirm: bool
    note: str
    shortcut: str
    subcommands: tuple[tuple[str, str], ...]


def web_command_catalog() -> tuple[WebCommandInfo, ...]:
    """补全、命令面板与 /help 共用的命令目录。"""
    rows: list[WebCommandInfo] = []
    for name, usage, description in SLASH_COMMANDS:
        capability = WEB_CAPABILITIES[name]
        rows.append(
            WebCommandInfo(
                name=name,
                usage=usage,
                description=description,
                support=capability.support,
                intent=capability.intent,
                confirm=capability.confirm,
                note=capability.note,
                shortcut=capability.shortcut,
                subcommands=SLASH_SUBCOMMANDS.get(name, ()),
            )
        )
    return tuple(rows)


MAX_TEXT = 2000
MODEL_TEXT_ROWS = 20


@dataclass(frozen=True)
class CommandResult:
    """返回值结构固定，由客户端决定如何展示。"""

    status: str  # 结果状态可取 ok、client、unsupported、unknown、busy、confirm_required 或 error。
    command: str
    intent: str = ""
    message: str = ""
    data: dict[str, Any] | None = None
    session_id: str | None = None
    refresh_sessions: bool = False
    refresh_history: bool = False

    @classmethod
    def invalid(cls, name: str) -> CommandResult:
        return cls("unknown", name, message="Invalid command.")


def _inference_data(items) -> dict[str, Any]:
    return {
        "kind": "inferences",
        "count": len(items),
        "rows": [
            {"id": item.id, "text": item.text, "topic": item.kind, "status": item.status}
            for item in items
        ],
    }


def _pending_data(items) -> dict[str, Any]:
    return {
        "kind": "pending",
        "count": len(items),
        "rows": [
            {
                "id": item.id,
                "created_at": item.created_at,
                "text": item.text,
                "topic": item.topic,
                "question": item.question,
                "related_ids": item.related_ids,
                "session_id": item.session_id,
                "message_id": item.message_id,
            }
            for item in items
        ],
    }


class CommandUseCases:
    """使用应用层已有用例分发斜杠命令。"""

    def __init__(self, application: ApplicationService) -> None:
        self.app = application
        self.conversation = application.conversation

    def catalog(self) -> tuple[WebCommandInfo, ...]:
        return web_command_catalog()

    # 会话生成期间仍允许读取、打断和调整后续回合的设置，
    # 会修改状态的命令按具体用法判断。
    BUSY_READ_ONLY = frozenset(
        {
            "help",
            "status",
            "summary",
            "topics",
            "pending",
            "inferences",
            "profile",
            "dream",
            "git",
            "undo",
            "sessions",
            "exit",
            "quit",
        }
    )
    TURN_SETTINGS = frozenset({"model", "think", "context"})

    # 涉及写入等副作用时，后端必须核验确认标记。浏览器中的确认弹窗只负责交互。
    WRITE_ACTIONS = {
        **REVIEW_ACTIONS,
        "git": frozenset({"sync"}),
    }

    @classmethod
    def busy_safe(cls) -> frozenset[str]:
        return cls.BUSY_READ_ONLY | cls.TURN_SETTINGS

    @classmethod
    def changes_turn_settings(cls, name: str, argument: str) -> bool:
        return (
            bool(argument)
            and name in cls.TURN_SETTINGS
            and not (name == "model" and argument.lower() in MODEL_REFRESH)
        )

    @staticmethod
    def _pending_edit(name: str, argument: str) -> tuple[str, str] | None:
        parts = argument.split(maxsplit=2)
        if name == "pending" and len(parts) >= 2 and parts[0].lower() == "edit":
            return parts[1], parts[2] if len(parts) == 3 else ""
        return None

    @classmethod
    def is_mutating(cls, name: str, argument: str) -> bool:
        """判断当前命令用法是否写入数据或运行耗时任务。

        忙碌检查与网页命令互斥均按具体用法判断。
        例如 /dream status 仅读取状态，/dream run 会提交反思任务。
        """
        if not is_command_form(name, argument):
            return False
        edit = cls._pending_edit(name, argument)
        if edit is not None:
            return bool(edit[1])
        if name in ("new", "compact", "rename", "undo"):
            return True
        if name in cls.TURN_SETTINGS:
            return bool(argument.strip())
        return cls._needs_confirmation(name, argument)

    @classmethod
    def _needs_confirmation(cls, name: str, argument: str) -> bool:
        if name == "dream":
            return argument.strip().lower() in {"", "run"}
        head = argument.split(maxsplit=1)[0].lower() if argument.strip() else ""
        return head in cls.WRITE_ACTIONS.get(name, ())

    def execute(
        self,
        line: str,
        *,
        busy: bool = False,
        confirmed: bool = False,
        expected_message_id: str | None = None,
    ) -> CommandResult:
        name, argument = parse_command(line)
        if not is_command_form(name, argument):
            return CommandResult.invalid(name)
        capability = WEB_CAPABILITIES[name]
        if capability.support == CLIENT:
            return CommandResult(
                "client", name, capability.intent, message=capability.note or capability.intent
            )
        if capability.support == UNSUPPORTED:
            return CommandResult("unsupported", name, capability.intent, message=capability.note)
        if (
            busy
            and name != "undo"
            and self.is_mutating(name, argument)
            and not self.changes_turn_settings(name, argument)
        ):
            return CommandResult(
                "busy",
                name,
                capability.intent,
                message="A reply is being generated. Stop it first, "
                "or use a read-only command such as /help or /status.",
            )
        try:
            edit = self._pending_edit(name, argument)
            if edit is not None and (not edit[1] or not confirmed):
                item_id, proposed = edit
                items = self.app.memory.pending().items
                if not any(item.id == item_id for item in items):
                    return CommandResult("error", name, "pending", "Pending candidate not found.")
                return CommandResult(
                    "confirm_required" if proposed else "ok",
                    name,
                    "pending",
                    "Review the candidate before saving.",
                    data={
                        **_pending_data(items),
                        "edit_id": item_id,
                        "edit_text": proposed or None,
                    },
                )
            # 服务器独立核验确认标记。
            if (capability.confirm or self._needs_confirmation(name, argument)) and not confirmed:
                if name == "dream":
                    return CommandResult(
                        "confirm_required",
                        name,
                        capability.intent,
                        "Use your saved memories to update your profile and propose "
                        "hypotheses for your review?",
                    )
                description = (
                    " ".join(argument.split(maxsplit=2)[:2])
                    if name in {"pending", "inferences"}
                    else argument
                )
                return CommandResult(
                    "confirm_required",
                    name,
                    capability.intent,
                    message=f"Confirm this action: /{name} {description}".strip()
                    + ". This changes stored data and cannot be undone.",
                )
            # 审核回执失败时保留原决定，以便重试。
            review_write = name in {"pending", "inferences"} and self.is_mutating(name, argument)
            with (
                vault_transaction(self.app.memory.service.paths) if review_write else nullcontext()
            ):
                return self._dispatch(name, argument, busy, expected_message_id)
        except TurnUndoError as exc:
            record_event("undo.failed", exc)
            return CommandResult("error", name, capability.intent, message=str(exc))
        except Exception as exc:  # noqa: BLE001 - 避免向浏览器泄露异常调用栈
            record_event("command.failed", exc)
            return CommandResult(
                "error", name, capability.intent, message=f"Command failed ({type(exc).__name__})."
            )

    def _dispatch(
        self, name: str, argument: str, busy: bool, expected_message_id: str | None = None
    ) -> CommandResult:
        conversation = self.conversation
        application = self.app

        if name == "help":
            lines = [f"{info.usage:<30}{info.description}" for info in web_command_catalog()]
            lines.append("")
            lines.append("Type / to complete. Ctrl+Shift+P / Alt+P opens the command palette.")
            lines.append("Ctrl+P and Ctrl+Q stay with the browser; use /exit to leave the view.")
            return CommandResult("ok", name, "help", "\n".join(lines))

        if name == "new":
            # 复用运行时的延迟创建机制，发送首条消息时才写入会话文件，
            # 放弃新会话不会留下空文件。
            conversation.switch(None)
            return CommandResult(
                "ok",
                name,
                "new_session",
                "Started a new session. It is saved with your first message.",
                refresh_sessions=True,
                refresh_history=True,
            )

        if name == "status":
            llm = application.config.llm
            status = {
                "provider": llm.provider,
                "model": llm.model,
                "retrieval": application.config.retrieval.method,
                "context_window": application.settings.context_window,
                "session": conversation.current_id,
                "generating": busy,
                "pending_review": application.pending_count,
            }
            display = {
                **status,
                "session": conversation.current_id or "(not created yet)",
                "generating": "yes" if busy else "no",
            }
            message = "\n".join(
                f"{key.replace('_', ' ').capitalize()}: {value}" for key, value in display.items()
            )
            return CommandResult("ok", name, "status", message, data={"status": status})

        if name == "summary":
            has_session = conversation.has_session
            text = (
                conversation.summary_text() or "This session has no summary yet."
                if has_session
                else "This new session has no summary yet."
            )
            return CommandResult(
                "ok",
                name,
                "summary",
                text[:MAX_TEXT],
                data={"can_compact": has_session and not busy},
            )

        if name == "topics":
            if argument:
                topic = application.memory.service.read_topic(argument)
                lines = [f"[{entry['status']}] {entry['text']}" for entry in topic["entries"]]
                return CommandResult(
                    "ok", name, "topics", f"{topic['topic']}\n\n" + "\n".join(lines), data=topic
                )
            rows = application.memory.topic_directory()
            names = [row["topic"] for row in rows]
            return CommandResult(
                "ok",
                name,
                "topics",
                f"{len(rows)} topics. " + ", ".join(names) if names else "No topics yet.",
                data={"count": len(rows), "topics": rows},
            )

        if name == "pending":
            memory = application.memory
            parts = argument.split(maxsplit=2)
            head = parts[0].lower() if parts else ""
            if not head:
                items = memory.pending().items
                return CommandResult(
                    "ok",
                    name,
                    "pending",
                    f"{len(items)} pending item(s)." if items else "No pending items.",
                    data=_pending_data(items),
                )
            item_id = parts[1]
            override = parts[2] if len(parts) == 3 else None
            if head == "reject":
                memory.reject(item_id)
            else:
                memory.approve(item_id, override)
            return CommandResult(
                "ok",
                name,
                "pending",
                f"Pending item {item_id}: {head} applied.",
                data=_pending_data(memory.pending().items),
            )

        if name == "inferences":
            memory = application.memory
            parts = argument.split(maxsplit=2)
            if not parts:
                review = memory.load_inference_review(include_sources=False)
                return CommandResult(
                    "ok",
                    name,
                    "inferences",
                    f"{len(review.items)} inference(s).",
                    data=_inference_data(review.items),
                )
            head, item_id = parts[0].lower(), parts[1]
            text = parts[2] if len(parts) == 3 else None
            action = "correct" if head == "edit" else head
            result = memory.review_inference(item_id, action, text, include_sources=False)
            return CommandResult(
                "ok",
                name,
                "inferences",
                result.feedback or f"Inference {item_id}: {action} applied.",
                data={**_inference_data(result.items), "updated_id": result.updated_id},
            )

        if name == "profile":
            try:
                snapshot = application.memory.profile()
            except (OSError, UnicodeError) as exc:
                record_event("command.failed", exc)
                return CommandResult(
                    "error", name, "profile", "Could not read the published profile."
                )
            return CommandResult(
                "ok",
                name,
                "profile",
                snapshot.body,
                data={
                    "status": snapshot.status,
                    "generated_at": snapshot.generated_at,
                    "fact_count": snapshot.fact_count,
                    "unique_count": snapshot.unique_count,
                },
            )

        if name == "dream":
            return application.reflection.command(argument)

        if name == "git":
            head = argument.strip().lower()
            if head == "sync":
                result = application.git.sync("manual")
                if result.status == "failed":
                    return CommandResult(
                        "error",
                        name,
                        "git",
                        (result.error or "Git synchronization failed.")[:MAX_TEXT],
                    )
                message = (result.error or "Git sync finished.")[:MAX_TEXT]
                return CommandResult(
                    "ok",
                    name,
                    "git",
                    message,
                    data={
                        "kind": "git",
                        "status": result.status,
                        "committed": result.committed,
                        "pushed": result.pushed,
                        "push_skipped": result.push_skipped,
                        "commit_hash": result.commit_hash,
                    },
                )
            # 只返回明确允许的字段，避免回显本机路径或提交内容。
            state = application.git.status()
            keys = (
                "initialized",
                "branch",
                "remote",
                "modified",
                "unpushed",
                "behind",
                "last_commit_at",
                "last_push_at",
            )
            rows = [f"{key}: {state[key]}" for key in keys if state.get(key) is not None]
            timestamps = {key: state[key] for key in keys if key.endswith("_at") and state.get(key)}
            return CommandResult(
                "ok",
                name,
                "git_status",
                "\n".join(rows) + "\n\nUse /git sync to synchronise.",
                data={
                    "timestamps": timestamps,
                    "status": {key: state[key] for key in keys if key in state},
                },
            )

        if name == "model":
            return self._model(argument)

        if name == "think":
            return self._think(argument)

        if name == "context":
            return self._context(argument)

        if name == "rename":
            if not argument:
                return CommandResult("ok", name, "rename", "Provide a title: /rename <title>")
            if not conversation.has_session:
                return CommandResult(
                    "error", name, "rename", "Send a message first, then rename the session."
                )
            try:
                conversation.rename_current(argument)
            except ConversationError as exc:
                if exc.code != "invalid_title":
                    raise
                return CommandResult("error", name, "rename", str(exc))
            return CommandResult(
                "ok", name, "rename", f"Renamed to: {argument}", refresh_sessions=True
            )

        if name == "compact":
            phases = []
            events = conversation.force_compact(on_compaction=phases.append)
            return CommandResult(
                "ok",
                name,
                "compact",
                "Context compacted."
                if not events
                else "\n".join(str(event) for event in events)[:MAX_TEXT],
                data={"compacted": "done" in phases},
                refresh_history=True,
            )

        if name == "undo":
            if busy:
                submission = conversation.request_undo(expected_message_id)
                if submission.kind == "undo_busy":
                    return CommandResult(
                        "busy", name, "undo", submission.message or "Undo already in progress."
                    )
                if submission.kind == "undo":
                    return CommandResult(
                        "ok",
                        name,
                        "undo",
                        "Generation stopped; the last turn is being undone.",
                        data={"undo_pending": True},
                    )
            restored = conversation.undo_current(expected_message_id)
            return CommandResult(
                "ok",
                name,
                "undo",
                "Last turn undone." if restored is not None else "Nothing to undo.",
                data={"restored": restored},
                refresh_history=restored is not None,
                refresh_sessions=restored is not None,
            )

        return CommandResult(
            "unsupported", name, "", "This command is not available in the Web client yet."
        )

    def _model(self, argument: str) -> CommandResult:
        settings = self.app.settings
        llm = self.app.config.llm
        refresh = argument.lower() in MODEL_REFRESH

        if argument and not refresh:
            try:
                options = settings.model_options(capabilities=False)
            except Exception:
                options = []
            # 应用选择后省略 options，避免浏览器立即再次打开选择器。
            provider, _, model = argument.partition("/")
            known = any(option.provider == provider and option.model == model for option in options)
            # 已配置的供应商即使没有 /models 列表，也可通过明确的模型 ID 切换。
            # 未知供应商仍会被拒绝。
            if not known and provider != llm.provider:
                return CommandResult(
                    "error",
                    "model",
                    "model",
                    "Unknown model. Use /model to list available options.",
                )
            result = settings.apply_model(provider, model)
            status = "ok" if result.ok else "error"
            return CommandResult(
                status, "model", "model", result.message, data={"model_changed": result.ok}
            )

        try:
            catalog = settings.model_catalog(refresh=refresh)
        except Exception:
            if refresh:
                return CommandResult(
                    "error",
                    "model",
                    "model",
                    "Refreshing the model catalog failed. The current model is unchanged.",
                )
            catalog = {}
        options = settings.model_choices(catalog)
        found = sum(len(models) for models in catalog.values())
        payload = [
            {
                "provider": option.provider,
                "model": option.model,
                "label": option.label,
                "current": option.current,
            }
            for option in options
        ]
        # 只限制纯文字摘要，交给客户端选择的目录保留完整的供应商和模型标识。
        lines = [
            f"{'*' if item['current'] else ' '} {item['provider']}/{item['model']}"
            for item in payload[:MODEL_TEXT_ROWS]
        ]
        if len(payload) > MODEL_TEXT_ROWS:
            lines.append(f"… ({len(payload)} models in the picker)")
        if refresh:
            detail = (
                f"Refreshed the provider catalog: {found} model(s) found."
                if found
                else "The provider did not return a model list (no /models endpoint, "
                "or the request failed). The configured model stays available; "
                "you can still use /model <provider>/<model> to switch by ID."
            )
        elif not found:
            detail = (
                "No provider model list available. Use /model refresh to fetch the provider's "
                "list, or /model <provider>/<model> to switch by ID."
            )
        else:
            detail = "Select with /model <provider>/<model>."
        return CommandResult(
            "ok",
            "model",
            "model",
            f"Current model: {llm.provider}/{llm.model}\nModels:\n"
            + "\n".join(lines)
            + "\n\n"
            + detail,
            data={"options": payload, **({"refresh_message": detail} if refresh else {})},
        )

    def _think(self, argument: str) -> CommandResult:
        settings = self.app.settings
        label, current = settings.reasoning_selection()
        options = [value for value, _ in settings.reasoning_options()]
        if not argument:
            return CommandResult(
                "ok",
                "think",
                "think",
                f"{label}: {current or 'default'}\n"
                f"Options: {', '.join(value or 'default' for value in options) or '(not supported by this model)'}"
                "\n\nSet with /think <value>.",
                data={"current": current, "options": options},
            )
        value = argument.lower()
        # 浏览器选择器将空选项显示为“default”。
        if value == "default" and "" in options:
            value = ""
        if value not in options:
            return CommandResult(
                "error",
                "think",
                "think",
                f"Allowed values: {', '.join(item or 'default' for item in options) or '(none)'}.",
            )
        result = settings.apply_reasoning(value)
        return CommandResult(
            "ok" if result.ok else "error",
            "think",
            "think",
            result.message,
            data={"model_changed": result.ok},
        )

    def _context(self, argument: str) -> CommandResult:
        settings = self.app.settings
        current = settings.context_window
        options = settings.context_options()
        if not argument:
            return CommandResult(
                "ok",
                "context",
                "context",
                f"Context window: {current}\n"
                f"Options: {', '.join(str(item) for item in options)}"
                "\n\nSet with /context <size>.",
                data={"current": current, "options": options, "minimum": MIN_CONTEXT_WINDOW},
            )
        try:
            value = int(argument)
        except ValueError:
            return CommandResult(
                "error", "context", "context", "Context window must be a number of tokens."
            )
        result = settings.apply_context(value)
        return CommandResult(
            "ok" if result.ok else "error",
            "context",
            "context",
            result.message,
            data={"model_changed": result.ok},
        )
