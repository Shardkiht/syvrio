"""Shared slash-command catalog and frontend-neutral dispatch.

No Textual, Rich, HTTP or web-framework imports. Both clients read the same
command names, usage strings and capability markers, but each owns its own
presentation. ``CommandUseCases`` reuses the existing Application use cases;
it never reimplements agent, memory or session logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..infra.diagnostics import record_event

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
    ("import", "/import <path> [--apply]", "Import a file or directory (preview by default)"),
    ("pending", "/pending", "Review pending"),
    ("inferences", "/inferences", "View and review inferences"),
    ("profile", "/profile [refresh [n]]", "View or refresh profile"),
    ("dream", "/dream [n|cancel|resume|status]", "Run reflection, cancel or view status"),
    ("topics", "/topics [name]", "Browse topics"),
    ("help", "/help", "Show commands"),
    ("exit", "/exit", "Quit"),
    ("quit", "/quit", "Quit"),
]

SLASH_SUBCOMMANDS = {
    "model": (("refresh", "Refresh model list"),),
    "profile": (
        ("refresh", "Refresh profile"),
        ("cancel", "Cancel reflection"),
        ("resume", "Resume automatic reflection"),
        ("status", "Show reflection status"),
    ),
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
    "undo": WebCapability(SERVER, "undo", confirm=True,
                          note="Discards the last user turn and its reply."),
    "summary": WebCapability(SERVER, "summary"),
    "status": WebCapability(SERVER, "status"),
    "git": WebCapability(SERVER, "git_status",
                         note="Status by default; /git sync runs a sync after confirmation."),
    "model": WebCapability(SERVER, "model", shortcut="Alt+M"),
    "think": WebCapability(SERVER, "think"),
    "context": WebCapability(SERVER, "context"),
    "setup": WebCapability(CLIENT, "open_settings",
                           note="Opens the Web settings panel for providers, keys and endpoints."),
    "rename": WebCapability(SERVER, "rename"),
    "import": WebCapability(UNSUPPORTED, "",
                            note="A browser-supplied path must never be read by the server. Use the TUI."),
    "pending": WebCapability(SERVER, "pending",
                             note="List, approve or reject: /pending approve|reject|edit <id>."),
    "inferences": WebCapability(SERVER, "inferences",
                                note="List, confirm or reject: /inferences confirm|reject|correct <id>."),
    "profile": WebCapability(SERVER, "profile",
                             note="View profile; /profile refresh [n] refreshes it."),
    "dream": WebCapability(SERVER, "dream",
                           note="Status, cancel or run: /dream [status|cancel|<n>]."),
    "topics": WebCapability(SERVER, "topics"),
    "help": WebCapability(SERVER, "help", shortcut="Ctrl+Shift+P / Alt+P"),
    "exit": WebCapability(CLIENT, "leave",
                          note="Returns to the Web entry view; never stops the background server."),
    "quit": WebCapability(CLIENT, "leave",
                          note="Returns to the Web entry view; never stops the background server."),
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
        rows.append(WebCommandInfo(
            name=name,
            usage=usage,
            description=description,
            support=capability.support,
            intent=capability.intent,
            confirm=capability.confirm,
            note=capability.note,
            shortcut=capability.shortcut,
            subcommands=SLASH_SUBCOMMANDS.get(name, ()),
        ))
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


def _item_rows(items, limit: int = MAX_ROWS) -> list[dict[str, str]]:
    """Frontend-neutral review rows: id plus a short, safe text preview."""
    rows: list[dict[str, str]] = []
    for item in items[:limit]:
        text = ""
        for field in ("text", "content", "body", "statement", "summary", "title"):
            value = getattr(item, field, None)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        rows.append({"id": str(getattr(item, "id", "") or ""), "text": text[:160]})
    return rows


class CommandUseCases:
    """Dispatch slash commands through existing Application use cases only."""

    def __init__(self, application: ApplicationService) -> None:
        self.app = application
        self.conversation = application.conversation
        # Mirror the TUI's per-client reflection pause (UI state, not a Vault write).
        self.reflection_paused = False

    def catalog(self) -> tuple[WebCommandInfo, ...]:
        return web_command_catalog()

    # The Web client runs a single turn at a time, so only read-only commands
    # (plus /undo, which cancels that turn) may run while a reply streams.
    # This is deliberately stricter than the TUI's SAFE_COMMANDS; a test
    # asserts this stays a subset of it so the two clients never disagree.
    BUSY_READ_ONLY = frozenset({
        "help", "status", "summary", "topics", "pending", "inferences",
        "profile", "dream", "git", "undo", "sessions", "exit", "quit",
    })
    BUSY_NO_ARGUMENT = frozenset({"model", "think", "context"})

    # Side-effecting command forms need a server-side confirmation even when
    # the browser dialog was already shown (the dialog is not an auth boundary).
    WRITE_ACTIONS = {
        "pending": frozenset({"approve", "reject", "edit"}),
        "inferences": frozenset({"confirm", "reject", "correct", "edit"}),
        "profile": frozenset({"refresh"}),
        "dream": frozenset({"run"}),
        "git": frozenset({"sync", "commit", "push"}),
    }

    @classmethod
    def busy_safe(cls) -> frozenset[str]:
        return cls.BUSY_READ_ONLY | cls.BUSY_NO_ARGUMENT

    @classmethod
    def is_mutating(cls, name: str, argument: str) -> bool:
        """Whether a command *form* writes data or runs a long job.

        The busy gate and the Web command mutex key off the form, not the name:
        /dream status is read-only while /dream 3 runs a reflection job.
        """
        head = argument.strip().lower().split(" ", 1)[0] if argument.strip() else ""
        if name in ("new", "compact", "rename"):
            return True
        if name in ("model", "think", "context"):
            return bool(argument.strip())
        if head in cls.WRITE_ACTIONS.get(name, ()):
            return True
        return name == "dream" and head.isdigit()

    @classmethod
    def _needs_confirmation(cls, name: str, argument: str) -> bool:
        head = argument.strip().lower().split(" ", 1)[0] if argument.strip() else ""
        if head in cls.WRITE_ACTIONS.get(name, ()):
            return True
        return name == "dream" and head.isdigit()

    def execute(
        self,
        line: str,
        *,
        busy: bool = False,
        confirmed: bool = False,
    ) -> CommandResult:
        name, argument = parse_command(line)
        if not line.startswith("/") or name not in command_names():
            # Never forward an unrecognised slash command to the LLM.
            return CommandResult(
                "unknown", name,
                message=f"Unknown command: /{name}. Type /help for available commands.",
            )
        capability = WEB_CAPABILITIES[name]
        if capability.support == CLIENT:
            return CommandResult("client", name, capability.intent,
                                 message=capability.note or capability.intent)
        if capability.support == UNSUPPORTED:
            return CommandResult("unsupported", name, capability.intent,
                                 message=capability.note)
        if busy and name != "undo" and self.is_mutating(name, argument):
            return CommandResult("busy", name, capability.intent,
                                 message="A reply is being generated. Stop it first, "
                                         "or use a read-only command such as /help or /status.")
        # The server re-checks confirmation independently of any browser dialog.
        if (capability.confirm or self._needs_confirmation(name, argument)) and not confirmed:
            return CommandResult(
                "confirm_required", name, capability.intent,
                message=f"Confirm this action: /{name} {argument}".strip()
                + ". This changes stored data and cannot be undone.",
            )
        try:
            return self._dispatch(name, argument, busy)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the browser
            record_event("command.failed", exc)
            return CommandResult("error", name, capability.intent,
                                 message=f"Command failed ({type(exc).__name__}).")

    def _dispatch(self, name: str, argument: str, busy: bool) -> CommandResult:
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
            conversation.runtime.close(distill=True)
            conversation.switch(None)
            return CommandResult("ok", name, "new_session",
                                 "Started a new session. It is saved with your first message.",
                                 refresh_sessions=True, refresh_history=True)

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
                return CommandResult("ok", name, "summary",
                                     "This new session has no summary yet.")
            text = conversation.summary_text() or "This session has no summary yet."
            return CommandResult("ok", name, "summary", text[:MAX_TEXT])

        if name == "topics":
            rows = application.memory.topics()
            names = [str(row.get("name") or row.get("title") or "")
                     for row in rows[:MAX_ROWS]]
            names = [item for item in names if item]
            return CommandResult("ok", name, "topics",
                                 f"{len(rows)} topics. " + ", ".join(names)
                                 if names else "No topics yet.",
                                 data={"count": len(rows), "names": names})

        if name == "pending":
            memory = application.memory
            head, _, rest = argument.partition(" ")
            head, rest = head.lower(), rest.strip()
            if not head:
                items = memory.pending().items
                return CommandResult(
                    "ok", name, "pending",
                    f"{len(items)} pending item(s)." if items else "No pending items.",
                    data={"kind": "pending", "rows": _item_rows(items), "count": len(items)},
                )
            if head in ("approve", "reject", "edit"):
                item_id, _, override = rest.partition(" ")
                if not item_id:
                    return CommandResult("error", name, "pending",
                                         f"Usage: /pending {head} <id>")
                if head == "reject":
                    memory.reject(item_id)
                else:
                    memory.approve(item_id, override.strip() or None)
                return CommandResult("ok", name, "pending",
                                     f"Pending item {item_id}: {head} applied.",
                                     refresh_sessions=True)
            return CommandResult("error", name, "pending",
                                 "Usage: /pending [approve|reject|edit <id>]")

        if name == "inferences":
            memory = application.memory
            head, _, rest = argument.partition(" ")
            head, rest = head.lower(), rest.strip()
            if not head:
                review = memory.load_inference_review()
                return CommandResult(
                    "ok", name, "inferences", f"{len(review.items)} inference(s).",
                    data={"kind": "inferences", "rows": _item_rows(review.items),
                          "count": len(review.items)},
                )
            if head in ("confirm", "reject", "correct", "edit"):
                item_id, _, text = rest.partition(" ")
                if not item_id:
                    return CommandResult("error", name, "inferences",
                                         f"Usage: /inferences {head} <id>")
                action = "correct" if head == "edit" else head
                result = memory.review_inference(item_id, action, text.strip() or None)
                return CommandResult("ok", name, "inferences",
                                     result.feedback or f"Inference {item_id}: {action} applied.")
            return CommandResult("error", name, "inferences",
                                 "Usage: /inferences [confirm|reject|correct <id>]")

        if name == "profile":
            head, _, rest = argument.partition(" ")
            if head.lower() == "cancel":
                self.reflection_paused = True
                application.reflection.cancel()
                return CommandResult("ok", name, "profile", "Reflection cancelled.")
            if head.lower() == "refresh":
                budget = int(rest.strip()) if rest.strip().isdigit() else 1
                application.reflection.run("profile", budget)
                snapshot = application.memory.profile()
                return CommandResult("ok", name, "profile",
                                     f"Profile refreshed.\n\n{snapshot.status}\n\n{snapshot.body}"[:MAX_TEXT])
            snapshot = application.memory.profile()
            return CommandResult("ok", name, "profile",
                                 f"{snapshot.status}\n\n{snapshot.body}"[:MAX_TEXT])

        if name == "dream":
            reflection = application.reflection
            head = argument.strip().lower()
            if head == "resume":
                self.reflection_paused = False
                return CommandResult("ok", name, "dream", "Automatic reflection resumed.")
            if head == "cancel":
                self.reflection_paused = True
                reflection.cancel()
                return CommandResult("ok", name, "dream", "Automatic reflection cancelled.")
            if head.isdigit():
                reflection.run("dream", int(head))
                return CommandResult("ok", name, "dream",
                                     f"Reflection run for {head} item(s) completed.")
            schedule = reflection.schedule()
            lines = [
                f"Automatic reflection: "
                f"{'paused' if self.reflection_paused else 'enabled' if schedule.enabled else 'disabled'}",
                f"Running: {'yes' if schedule.running else 'no'}",
                f"Profile next: {schedule.profile_next or '-'}",
                f"Dream next: {schedule.dream_next or '-'}",
            ]
            return CommandResult("ok", name, "dream", "\n".join(lines),
                                 data={"kind": "dream", "rows": [
                                     {"id": "cancel", "text": "Cancel automatic reflection"},
                                     {"id": "1", "text": "Run reflection on 1 item"},
                                 ]})

        if name == "git":
            head = argument.strip().lower()
            if head == "sync":
                result = application.git.sync("manual")
                message = str(getattr(result, "message", None) or "Git sync finished.")[:MAX_TEXT]
                return CommandResult("ok", name, "git", message, data={"kind": "git"})
            # Whitelist fields: never echo local paths or commit content.
            state = application.git.status()
            keys = ("initialized", "branch", "remote", "modified", "unpushed",
                    "behind", "last_commit_at", "last_push_at")
            rows = [f"{key}: {state[key]}" for key in keys if state.get(key) is not None]
            return CommandResult("ok", name, "git_status",
                                 "\n".join(rows) + "\n\nUse /git sync to synchronise.")

        if name == "model":
            return self._model(argument)

        if name == "think":
            return self._think(argument)

        if name == "context":
            return self._context(argument)

        if name == "rename":
            if not argument:
                return CommandResult("ok", name, "rename",
                                     "Provide a title: /rename <title>")
            if not conversation.has_session:
                return CommandResult("error", name, "rename",
                                     "Send a message first, then rename the session.")
            conversation.rename_current(argument[:200])
            return CommandResult("ok", name, "rename", f"Renamed to: {argument[:200]}",
                                 refresh_sessions=True)

        if name == "compact":
            events = conversation.force_compact()
            return CommandResult("ok", name, "compact",
                                 "Context compacted." if not events
                                 else "\n".join(str(event) for event in events)[:MAX_TEXT],
                                 refresh_history=True)

        if name == "undo":
            if busy:
                submission = conversation.request_undo()
                if submission.kind == "undo_busy":
                    return CommandResult("busy", name, "undo",
                                         submission.message or "Undo already in progress.")
                return CommandResult("ok", name, "undo",
                                     "Generation stopped; the last turn is being undone.",
                                     refresh_history=True, refresh_sessions=True)
            restored = conversation.undo_current()
            return CommandResult("ok", name, "undo",
                                 f"Restored the last message: {restored}" if restored
                                 else "Nothing to undo.",
                                 refresh_history=True, refresh_sessions=True)

        return CommandResult("unsupported", name, "",
                             "This command is not available in the Web client yet.")

    def _model(self, argument: str) -> CommandResult:
        settings = self.app.settings
        llm = self.app.config.llm
        refresh = argument.lower() in ("refresh", "--refresh", "-r")

        if refresh:
            # The TUI refreshes the catalog from a background worker. The Web
            # client has no worker, so an explicit `/model refresh` does it.
            try:
                catalog = settings.model_catalog(refresh=True)
                options = settings.model_options(catalog, capabilities=False)
            except Exception:
                return CommandResult("error", "model", "model",
                                     "Refreshing the model catalog failed. "
                                     "The current model is unchanged.")
            found = sum(len(models) for models in catalog.values())
        else:
            try:
                options = settings.model_options()
            except Exception:
                options = []

        payload = [
            {"provider": option.provider, "model": option.model,
             "label": option.label,
             "current": (option.provider, option.model) == (llm.provider, llm.model)}
            for option in options[:MAX_ROWS]
        ]
        # A cold process cache, or a provider without a /models endpoint (Spark
        # MaaS, local servers), must never produce an empty, meaningless picker.
        if not any(item["provider"] == llm.provider and item["model"] == llm.model
                   for item in payload):
            payload.insert(0, {"provider": llm.provider, "model": llm.model,
                               "label": f"{llm.provider}/{llm.model}", "current": True})

        if argument and not refresh:
            # Applying a choice is not a list: omit `options` so the browser
            # does not reopen the picker immediately after the selection.
            provider, separator, model = argument.partition("/")
            if not separator or not provider or not model:
                return CommandResult("error", "model", "model",
                                     "Usage: /model <provider>/<model>. Use /model to list options.")
            known = any(item["provider"] == provider and item["model"] == model
                        for item in payload)
            # Providers with no /models list still allow switching by explicit
            # ID on the configured provider; unknown providers stay rejected.
            if not known and provider != llm.provider:
                return CommandResult("error", "model", "model",
                                     "Unknown model. Use /model to list available options.")
            result = settings.apply_model(provider, model)
            status = "ok" if result.ok else "error"
            return CommandResult(status, "model", "model", result.message,
                                 data={"model_changed": result.ok})

        lines = [f"{'*' if item['current'] else ' '} {item['provider']}/{item['model']}"
                 for item in payload]
        if refresh:
            detail = (f"Refreshed the provider catalog: {found} model(s) found."
                      if found else
                      "The provider did not return a model list (no /models endpoint, "
                      "or the request failed). The configured model stays available; "
                      "you can still use /model <provider>/<model> to switch by ID.")
        elif len(payload) == 1:
            detail = ("No cached model list yet. Use /model refresh to fetch the provider's "
                      "list, or /model <provider>/<model> to switch by ID.")
        else:
            detail = "Select with /model <provider>/<model>."
        return CommandResult(
            "ok", "model", "model",
            f"Current model: {llm.provider}/{llm.model}\nModels:\n" + "\n".join(lines)
            + "\n\n" + detail,
            data={"options": payload},
        )

    def _think(self, argument: str) -> CommandResult:
        settings = self.app.settings
        label, current = settings.reasoning_selection()
        options = [value for value, _ in settings.reasoning_options()]
        if not argument:
            return CommandResult("ok", "think", "think",
                                 f"{label}: {current or 'default'}\n"
                                 f"Options: {', '.join(value or 'default' for value in options) or '(not supported by this model)'}"
                                 "\n\nSet with /think <value>.",
                                 data={"current": current, "options": options})
        value = argument.lower()
        # The empty option is rendered as "default" in the browser picker.
        if value == "default" and "" in options:
            value = ""
        if value not in options:
            return CommandResult("error", "think", "think",
                                 f"Allowed values: {', '.join(item or 'default' for item in options) or '(none)'}.")
        result = settings.apply_reasoning(value)
        return CommandResult("ok" if result.ok else "error", "think", "think",
                             result.message, data={"model_changed": result.ok})

    def _context(self, argument: str) -> CommandResult:
        settings = self.app.settings
        current = settings.context_window
        options = settings.context_options()
        if not argument:
            return CommandResult("ok", "context", "context",
                                 f"Context window: {current}\n"
                                 f"Options: {', '.join(str(item) for item in options)}"
                                 "\n\nSet with /context <size>.",
                                 data={"current": current, "options": options})
        try:
            value = int(argument)
        except ValueError:
            return CommandResult("error", "context", "context",
                                 "Context window must be a number of tokens.")
        result = settings.apply_context(value)
        return CommandResult("ok" if result.ok else "error", "context", "context",
                             result.message, data={"model_changed": result.ok})
