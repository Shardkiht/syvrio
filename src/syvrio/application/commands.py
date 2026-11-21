"""Shared slash-command catalog and frontend-neutral dispatch.

No Textual, Rich, HTTP or web-framework imports. Both clients read the same
command names, usage strings and capability markers, but each owns its own
presentation. ``CommandUseCases`` reuses the existing Application use cases;
it never reimplements agent, memory or session logic.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..infra.config import MIN_CONTEXT_WINDOW
from ..infra.diagnostics import record_event
from ..infra.transaction import vault_transaction
from ..memory.turn_undo import TurnUndoError

if TYPE_CHECKING:
    from .service import ApplicationService

SLASH_COMMANDS = [
    ("new", "/new", "New session"),
    ("sessions", "/sessions", "Browse sessions"),
    ("compact", "/compact", "Compact context"),
    ("undo", "/undo", "Undo and edit last message"),
    ("summary", "/summary", "Show summary"),
    ("status", "/status", "Session status"),
    ("git", "/git", "Git status and synchronization"),
    ("model", "/model [refresh]", "Models & providers"),
    ("think", "/think", "Reasoning effort"),
    ("context", "/context", "Context window"),
    ("setup", "/setup", "Run setup in terminal"),
    ("rename", "/rename <title>", "Rename session"),
    ("import", "/import", "Choose local files to attach"),
    ("pending", "/pending", "Review pending"),
    ("inferences", "/inferences", "View and review inferences"),
    ("profile", "/profile", "View the generated profile"),
    ("dream", "/dream [n|cancel|resume|status]", "Run reflection, cancel or view status"),
    ("topics", "/topics [name]", "Browse topics"),
    ("help", "/help", "Show commands"),
    ("exit", "/exit", "Quit"),
    ("quit", "/quit", "Quit"),
]

SLASH_SUBCOMMANDS = {
    "model": (("refresh", "Refresh model list"),),
    "dream": (
        ("cancel", "Cancel reflection"),
        ("resume", "Resume automatic reflection"),
        ("status", "Show reflection status"),
    ),
}


def slash_matches(prefix: str) -> list[tuple[str, str, str]]:
    command, space, argument = prefix.lower().partition(" ")
    if not space:
        return [row for row in SLASH_COMMANDS if row[0].startswith(command)]
    argument = argument.lstrip()
    if " " in argument:
        return []
    return [
        (f"{command} {name}", f"/{command} {name}", description)
        for name, description in SLASH_SUBCOMMANDS.get(command, ())
        if name.startswith(argument)
    ]


def command_names() -> frozenset[str]:
    return frozenset(name for name, _, _ in SLASH_COMMANDS)


def parse_command(line: str) -> tuple[str, str]:
    """Return (name, argument). An unknown name is never treated as a message."""
    if not line.startswith("/"):
        return "", line.strip()
    head, _, argument = line.partition(" ")
    return head[1:].lower(), argument.strip()


# ---------- Web capability markers ----------
# support: "server" (Web backend executes it), "client" (browser-only UI),
# "unsupported" (explicit reason shown to the user; never silently ignored).
# confirm: the browser must ask first AND the backend must independently
# re-check the confirmed flag before any side effect.

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
    "dream": WebCapability(
        SERVER, "dream", note="Status, cancel or run: /dream [status|cancel|<n>]."
    ),
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
    """One stable catalog for completion, the palette and /help."""
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
MAX_ROWS = 20


@dataclass(frozen=True)
class CommandResult:
    """Stable result shape; clients decide how to render it."""

    status: str  # ok / client / unsupported / unknown / busy / confirm_required / error
    command: str
    intent: str = ""
    message: str = ""
    data: dict[str, Any] | None = None
    session_id: str | None = None
    refresh_sessions: bool = False
    refresh_history: bool = False


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
    """Dispatch slash commands through existing Application use cases only."""

    def __init__(self, application: ApplicationService) -> None:
        self.app = application
        self.conversation = application.conversation

    def catalog(self) -> tuple[WebCommandInfo, ...]:
        return web_command_catalog()

    # Reads, interrupts, and settings for subsequent turns remain available
    # while a session is generating. Mutating forms are checked individually.
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
    MODEL_REFRESH = frozenset({"refresh", "--refresh", "-r"})

    # Side-effecting command forms need a server-side confirmation even when
    # the browser dialog was already shown (the dialog is not an auth boundary).
    WRITE_ACTIONS = {
        "pending": frozenset({"approve", "reject", "edit"}),
        "inferences": frozenset({"confirm", "reject", "correct", "edit"}),
        "dream": frozenset({"resume"}),
        "git": frozenset({"sync", "commit", "push"}),
    }

    @classmethod
    def busy_safe(cls) -> frozenset[str]:
        return cls.BUSY_READ_ONLY | cls.TURN_SETTINGS

    @classmethod
    def changes_turn_settings(cls, name: str, argument: str) -> bool:
        return (
            bool(argument)
            and name in cls.TURN_SETTINGS
            and not (name == "model" and argument.lower() in cls.MODEL_REFRESH)
        )

    @staticmethod
    def _pending_edit(name: str, argument: str) -> tuple[str, str] | None:
        parts = argument.split(maxsplit=2)
        if name == "pending" and len(parts) >= 2 and parts[0].lower() == "edit":
            return parts[1], parts[2] if len(parts) == 3 else ""
        return None

    @classmethod
    def is_mutating(cls, name: str, argument: str) -> bool:
        """Whether a command *form* writes data or runs a long job.

        The busy gate and the Web command mutex key off the form, not the name:
        /dream status is read-only while /dream 3 runs a reflection job.
        """
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
        head = argument.split(maxsplit=1)[0].lower() if argument.strip() else ""
        if head in cls.WRITE_ACTIONS.get(name, ()):
            return True
        return name == "dream" and head.isdigit()

    def execute(
        self,
        line: str,
        *,
        busy: bool = False,
        confirmed: bool = False,
        expected_message_id: str | None = None,
    ) -> CommandResult:
        name, argument = parse_command(line)
        if not line.startswith("/") or name not in command_names():
            # Never forward an unrecognised slash command to the LLM.
            return CommandResult(
                "unknown",
                name,
                message=f"Unknown command: /{name}. Type /help for available commands.",
            )
        capability = WEB_CAPABILITIES[name]
        if name == "import" and argument:
            return CommandResult(
                "error", name, message="Use /import without a path to choose files on this device."
            )
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
            # The server re-checks confirmation independently of any browser dialog.
            if (capability.confirm or self._needs_confirmation(name, argument)) and not confirmed:
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
            # A failed review receipt must leave the decision available for retry.
            review_write = name in {"pending", "inferences"} and self.is_mutating(name, argument)
            with (
                vault_transaction(self.app.memory.service.paths) if review_write else nullcontext()
            ):
                return self._dispatch(name, argument, busy, expected_message_id)
        except TurnUndoError as exc:
            record_event("undo.failed", exc)
            return CommandResult("error", name, capability.intent, message=str(exc))
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the browser
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
            # Reuse the runtime's lazy start: the session file is written with the
            # first message, so an abandoned "new" leaves nothing on disk.
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
            rows = [
                f"Provider: {llm.provider}",
                f"Model: {llm.model}",
                f"Retrieval: {application.config.retrieval.method}",
                f"Context window: {application.settings.context_window}",
                f"Session: {conversation.current_id or '(not created yet)'}",
                f"Generating: {'yes' if busy else 'no'}",
                f"Pending review: {application.pending_count}",
            ]
            return CommandResult("ok", name, "status", "\n".join(rows))

        if name == "summary":
            if not conversation.has_session:
                return CommandResult("ok", name, "summary", "This new session has no summary yet.")
            text = conversation.summary_text() or "This session has no summary yet."
            return CommandResult("ok", name, "summary", text[:MAX_TEXT])

        if name == "topics":
            if argument:
                topic = application.memory.service.read_topic(argument)
                lines = [f"[{entry['status']}] {entry['text']}" for entry in topic["entries"]]
                return CommandResult(
                    "ok", name, "topics", f"{topic['topic']}\n\n" + "\n".join(lines), data=topic
                )
            rows = application.memory.topics()
            names = [str(row.get("topic") or row.get("title") or "") for row in rows]
            names = [item for item in names if item]
            return CommandResult(
                "ok",
                name,
                "topics",
                f"{len(rows)} topics. " + ", ".join(names) if names else "No topics yet.",
                data={"count": len(rows), "names": names},
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
            if head in ("approve", "reject", "edit"):
                if len(parts) < 2:
                    return CommandResult("error", name, "pending", f"Usage: /pending {head} <id>")
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
            return CommandResult(
                "error", name, "pending", "Usage: /pending [approve|reject|edit <id>]"
            )

        if name == "inferences":
            memory = application.memory
            head, _, rest = argument.partition(" ")
            head, rest = head.lower(), rest.strip()
            if not head:
                review = memory.load_inference_review()
                return CommandResult(
                    "ok",
                    name,
                    "inferences",
                    f"{len(review.items)} inference(s).",
                    data=_inference_data(review.items),
                )
            if head in ("confirm", "reject", "correct", "edit"):
                item_id, _, text = rest.partition(" ")
                if not item_id:
                    return CommandResult(
                        "error", name, "inferences", f"Usage: /inferences {head} <id>"
                    )
                action = "correct" if head == "edit" else head
                result = memory.review_inference(item_id, action, text.strip() or None)
                return CommandResult(
                    "ok",
                    name,
                    "inferences",
                    result.feedback or f"Inference {item_id}: {action} applied.",
                    data={**_inference_data(result.items), "updated_id": result.updated_id},
                )
            return CommandResult(
                "error", name, "inferences", "Usage: /inferences [confirm|reject|correct <id>]"
            )

        if name == "profile":
            if argument:
                return CommandResult(
                    "error", name, "profile", "Use /profile to view or /dream to regenerate."
                )
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
            reflection = application.reflection
            head = argument.strip().lower()
            if head == "cancel":
                reflection.cancel()
                return CommandResult("ok", name, "dream", "Dream cancelled.")
            if head == "resume" or head.isdigit():
                job = reflection.run("dream", int(head) if head.isdigit() else None)
                return CommandResult("ok", name, "dream", f"Dream submitted: {job}.")
            schedule = reflection.schedule()
            status = reflection.status()
            lines = [
                f"Automatic Dream: {'enabled' if schedule.enabled else 'disabled'}",
                f"State: {status['state']}",
                f"Next: {schedule.dream_next or '-'}",
            ]
            if schedule.dream_pending and status.get("phase"):
                lines.append(f"Phase: {status['phase']}")
            return CommandResult(
                "ok",
                name,
                "dream",
                "\n".join(lines),
                data={
                    "kind": "dream",
                    "rows": [
                        {"id": "cancel", "text": "Cancel automatic reflection"},
                        {"id": "1", "text": "Run reflection on 1 item"},
                    ],
                },
            )

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
            # Whitelist fields: never echo local paths or commit content.
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
            conversation.rename_current(argument[:200])
            return CommandResult(
                "ok", name, "rename", f"Renamed to: {argument[:200]}", refresh_sessions=True
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
        refresh = argument.lower() in self.MODEL_REFRESH

        if refresh:
            # The TUI refreshes the catalog from a background worker. The Web
            # client has no worker, so an explicit `/model refresh` does it.
            try:
                catalog = settings.model_catalog(refresh=True)
                options = settings.model_options(catalog, capabilities=False)
            except Exception:
                return CommandResult(
                    "error",
                    "model",
                    "model",
                    "Refreshing the model catalog failed. The current model is unchanged.",
                )
            found = sum(len(models) for models in catalog.values())
        else:
            try:
                options = settings.model_options(capabilities=False)
            except Exception:
                options = []

        payload = [
            {
                "provider": option.provider,
                "model": option.model,
                "label": option.label,
                "current": (option.provider, option.model) == (llm.provider, llm.model),
            }
            for option in options[:MAX_ROWS]
        ]
        # A cold process cache, or a provider without a /models endpoint (Spark
        # MaaS, local servers), must never produce an empty, meaningless picker.
        if not any(
            item["provider"] == llm.provider and item["model"] == llm.model for item in payload
        ):
            payload.insert(
                0,
                {
                    "provider": llm.provider,
                    "model": llm.model,
                    "label": f"{llm.provider}/{llm.model}",
                    "current": True,
                },
            )

        if argument and not refresh:
            # Applying a choice is not a list: omit `options` so the browser
            # does not reopen the picker immediately after the selection.
            provider, separator, model = argument.partition("/")
            if not separator or not provider or not model:
                return CommandResult(
                    "error",
                    "model",
                    "model",
                    "Usage: /model <provider>/<model>. Use /model to list options.",
                )
            known = any(option.provider == provider and option.model == model for option in options)
            # Providers with no /models list still allow switching by explicit
            # ID on the configured provider; unknown providers stay rejected.
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

        lines = [
            f"{'*' if item['current'] else ' '} {item['provider']}/{item['model']}"
            for item in payload
        ]
        if refresh:
            detail = (
                f"Refreshed the provider catalog: {found} model(s) found."
                if found
                else "The provider did not return a model list (no /models endpoint, "
                "or the request failed). The configured model stays available; "
                "you can still use /model <provider>/<model> to switch by ID."
            )
        elif len(payload) == 1:
            detail = (
                "No cached model list yet. Use /model refresh to fetch the provider's "
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
            data={"options": payload},
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
        # The empty option is rendered as "default" in the browser picker.
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
