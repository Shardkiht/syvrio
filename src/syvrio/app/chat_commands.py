from __future__ import annotations

from functools import partial
from pathlib import Path

from rich.markup import escape
from textual.widgets import Input, Static

from ..infra.diagnostics import record_event
from ..infra.tokens import format_tokens
from ..memory.models import PendingItem
from .chat_widgets import ModelPickerScreen, PendingPickerScreen, PickScreen, SessionPickerScreen, TextInputScreen
from .command_catalog import SLASH_COMMANDS
from .inference_screen import InferenceScreen
from .session_ui import visible_sessions


class ChatCommandsMixin:
    """斜杠命令、Pending、主题、会话与设置选择器；依赖 ChatApp 的界面方法。"""

    # ---------- 斜杠命令 ----------

    def _command(self, line: str) -> None:
        command, _, argument = line.partition(" ")
        command = command.lower()
        argument = argument.strip()

        if command in ("/exit", "/quit"):
            self.exit()
        elif command == "/help":
            self.add_card("\n".join(f"{usage:<24}{description}" for _, usage, description in SLASH_COMMANDS), "Commands")
        elif command == "/model":
            self._open_models(argument.lower() in ("refresh", "--refresh", "-r"))
        elif command == "/think":
            self._pick_reasoning()
        elif command == "/context":
            self._pick_context()
        elif command == "/setup":
            self.add_card("Use Ctrl+P in /model to manage providers. Run syvrio setup in a terminal for full configuration.")
        elif command == "/new":
            self._new_session()
        elif command == "/sessions":
            self.open_session_picker()
        elif command == "/compact":
            self._run_compact()
        elif command == "/undo":
            self._undo_message()
        elif command == "/summary":
            if not self.application.conversation.has_session:
                self.add_card("This new session has no summary yet.")
            else:
                summary = self.application.conversation.summary_text()
                self.add_markdown_card(summary, "Session summary") if summary else self.add_card("This session has no summary yet.")
        elif command == "/status":
            self._show_status()
        elif command == "/git":
            self.open_git_screen()
        elif command == "/rename":
            if not self.application.conversation.has_session:
                self.add_card("The session is not created yet; send a message first, then rename.")
            elif argument:
                self.application.conversation.rename_current(argument)
                self.refresh_sessions()
                self.add_card(f"Renamed to: {argument}")
            else:
                self._open_rename_prompt()
        elif command == "/import":
            if not argument:
                self.push_screen(TextInputScreen("File or directory path (preview by default; add --apply to save)", on_result=lambda value: self._run_import(value) if value else None))
            else:
                self._run_import(argument)
        elif command == "/pending":
            self._open_pending()
        elif command == "/inferences":
            self._run_inference_review()
        elif command in ("/profile", "/dream"):
            self._reflection_command(command[1:], argument)
        elif command == "/topics":
            self._show_topics(argument.strip() or None)
        else:
            self.add_card(f"Unknown command: {command}. Type /help for help.")

    # ---------- Pending交互处理（issue #1） ----------

    def _run_inference_review(self, item_id: str | None = None,
                              action: str | None = None, text: str | None = None) -> None:
        self._begin_navigation()
        self.run_worker(partial(self._inference_work, item_id, action, text),
                        thread=True, group="inference", exit_on_error=False)

    def _inference_work(self, item_id: str | None, action: str | None, text: str | None) -> None:
        try:
            review = (
                self.application.memory.review_inference(item_id or "", action, text)
                if action is not None
                else self.application.memory.load_inference_review()
            )
        except Exception as exc:
            record_event("command.failed", exc)
            message = f"Inference action failed: {exc}" if action else f"Could not load inferences: {exc}"
            self.call_from_thread(self._inference_failed, message)
            return
        self.call_from_thread(
            self._show_inferences,
            review.items,
            review.sources,
            review.updated_id,
            review.feedback,
        )

    def _show_inferences(
        self, items: list, sources: dict, focus_id: str | None = None, feedback: str | None = None,
    ) -> None:
        self._end_navigation()
        if self._pending_new or self._pending_switch:
            self._drain_pending_intent()
            return
        if isinstance(self.screen, InferenceScreen):
            # 操作后在原界面内刷新，不再叠加选择器或向聊天区插入详情卡片。
            self.screen.set_items(items, sources, focus_id, feedback)
        else:
            self.push_screen(
                InferenceScreen(items, sources, self._run_inference_review),
                lambda _: self.query_one("#input", Input).focus(),
            )
        self._drain_pending_intent()

    def _inference_failed(self, message: str) -> None:
        self._end_navigation()
        if isinstance(self.screen, InferenceScreen):
            self.screen.show_error(message)
        else:
            self.add_card(message)
        self._drain_pending_intent()

    @staticmethod
    def _truncate(text: str, width: int) -> str:
        return text if len(text) <= width else text[:width] + "…"

    def _open_pending(self) -> None:
        self._run_pending_review()

    def _pending_picked(self, item: PendingItem | None) -> None:
        if item is None:
            self.query_one("#input", Input).focus()
            return
        options = [
            ("Approve", "approve"),
            ("Edit and approve", "edit"),
            ("Reject", "reject"),
        ]
        self.push_screen(PickScreen(escape(f"Pending [{item.topic}]"), options,
                                    partial(self._pending_action, item), detail=item.text))

    def _pending_action(self, item: PendingItem, action: str | None) -> None:
        if action is None:
            self.query_one("#input", Input).focus()
            return
        if action == "edit":
            self.push_screen(
                TextInputScreen("Full revised text", default=item.text,
                                on_result=partial(self._pending_edited, item.id))
            )
            return
        self._run_pending_review(action, item.id)

    def _pending_edited(self, item_id: str, text: str | None) -> None:
        if not text:
            self.query_one("#input", Input).focus()
            return
        self._run_pending_review("approve", item_id, text.strip())

    def _run_pending_review(self, action: str | None = None, item_id: str = "",
                            override: str | None = None) -> None:
        if self._pending_review_busy:
            self.notify("Pending review is already in progress.")
            return
        self._pending_review_busy = True
        session_id = self.application.conversation.current_id
        self.run_worker(partial(self._pending_work, session_id, action, item_id, override),
                        thread=True, group="pending", exit_on_error=False)

    def _pending_work(self, session_id: str | None, action: str | None, item_id: str,
                      override: str | None) -> None:
        finish = partial(self._finish_pending, session_id)
        message = None
        try:
            if action == "approve":
                entry = self.application.memory.approve(item_id, text_override=override)
                message = f"Saved [{entry.topic}] {entry.text}"
            elif action == "reject":
                item = self.application.memory.reject(item_id)
                message = f"Rejected {item.id} {self._truncate(item.text, 40)}"
        except Exception as exc:
            record_event("command.failed", exc)
            self.call_from_thread(finish, None, f"Pending decision failed: {exc}")
            return
        try:
            items = self.application.memory.pending().items
        except Exception as exc:
            record_event("command.failed", exc)
            error = f"Could not load pending items: {exc}"
            self.call_from_thread(finish, None, f"{message}\n{error}" if message else error)
            return
        self.call_from_thread(finish, items, message)

    def _finish_pending(self, session_id: str | None, items: list[PendingItem] | None,
                        message: str | None) -> None:
        self._pending_review_busy = False
        if items is not None:
            self.pending_count = len(items)
            self.update_status()
        if self.screen is not self.default_screen or self.application.conversation.current_id != session_id:
            if message:
                self.notify(message)
            return
        if message:
            self.add_card(message)
        if items is None:
            return
        widget = self.query_one("#input", Input)
        if not items:
            self.add_card("Pending queue is empty." if message else "No pending items.")
            widget.focus()
        elif not widget.value.strip() and not self._navigation_busy:
            options = [
                (f"{item.id}  [{item.topic}] {self._truncate(item.text, 36)}", item)
                for item in items
            ]
            self.push_screen(PendingPickerScreen(f"Pending items: {len(items)}", options, self._pending_picked))

    def _show_topics(self, name: str | None = None) -> None:
        topics = self.application.memory.topics()
        if not topics:
            self.add_card("The memory vault is empty.")
            return
        if name:
            needle = name.lower()
            matched = next(
                (t for t in topics if needle in t["topic"].lower() or needle in t["title"].lower()),
                None,
            )
            if matched is None:
                self.add_card(f"No matching topic: {name}")
                return
            entries = self.application.memory.topic_entries(matched["topic"])
            lines = [f"{matched['title']} ({matched['topic']}) · {len(entries)} entries", ""]
            for entry in entries:
                tag = "" if entry.status == "active" else f" ({entry.status})"
                lines.append(f"{entry.id[:26]}  {self._truncate(entry.text, 56)}{tag}")
            self.add_card("\n".join(lines), "Topic entries")
            return
        self.add_card(
            "\n".join(f"{t['topic']:<24} {t['title']} · {t['entries']} entries · updated {t['updated']}" for t in topics),
            "Memory topics",
        )

    def open_session_picker(self) -> None:
        rows = visible_sessions(self.application.conversation.list_sessions(100))
        if not rows:
            self.add_card("No sessions to choose from.")
            return
        current_id = self.application.conversation.current_id
        self.push_screen(
            SessionPickerScreen(rows, current_id, self._picker_selected)
        )

    def _picker_selected(self, session_id) -> None:
        if session_id is None:
            self.query_one("#input", Input).focus()
            return
        if self.application.conversation.current_id == session_id:
            self.notify("Already the current session.")
            self.query_one("#input", Input).focus()
            return
        if self._navigation_busy:
            self.application.conversation.request_switch(session_id)
            return
        self._switch_session(session_id)

    def _open_rename_prompt(self) -> None:
        self.push_screen(
            TextInputScreen(
                "Rename session",
                default=self.application.conversation.current.title(),
                on_result=self._rename_current_result,
            )
        )

    def _rename_current_result(self, title) -> None:
        if title:
            self.application.conversation.rename_current(title)
            self.refresh_sessions()
            self.notify(f"Renamed to: {title}")
        self.query_one("#input", Input).focus()

    def rename_session(self, row: dict) -> None:
        self.push_screen(
            TextInputScreen(
                "Rename session",
                default=row["title"],
                on_result=partial(self._rename_session_result, row),
            )
        )

    def _rename_session_result(self, row: dict, title) -> None:
        if title:
            if not self.application.conversation.session_exists(row["id"]):
                self.notify("Session no longer exists.")
            else:
                self.application.conversation.rename_session(row["id"], title)
                self.refresh_sessions()
                for screen in self.screen_stack:
                    if isinstance(screen, SessionPickerScreen):
                        screen.set_rows(visible_sessions(self.application.conversation.list_sessions(100)))
                self.notify("Session renamed.")
        # 重命名弹窗关闭后，焦点回到选择器而不是主输入框。
        if isinstance(self.screen, SessionPickerScreen):
            self.screen.query_one("#picker-input", Input).focus()
        else:
            self.query_one("#input", Input).focus()

    def _show_status(self) -> None:
        if not self.application.conversation.has_session:
            self.add_card("The session is not created yet; it appears after your first message.")
            return
        info = self.application.conversation.status()
        percent = info["estimated_tokens"] / max(info["context_window"], 1)
        self.add_card(
            f"Session: {info['session_id']} · {info['title']}\n"
            f"Model: {info['model']}\n"
            f"Retrieval: {info['retrieval']}\n"
            f"Context: {format_tokens(info['estimated_tokens'])}/{format_tokens(info['context_window'])} ({percent:.0%})\n"
            f"Summary: {info['summaries']} segments / {info['summary_chars']} chars · covering {info['covered_records']} records\n"
            f"Pending: {info['pending']}",
            "Status",
        )

    def _undo_message(self) -> None:
        if not self.application.conversation.has_session:
            self.add_card("No message to undo.")
            return
        text = self.application.conversation.undo_current()
        if text is None:
            self.add_card("No message to undo.")
            return
        self._clear_chat()
        self.build_transcript()
        self.refresh_sessions()
        self.update_status()
        widget = self.query_one("#input", Input)
        widget.value = text
        widget.cursor_position = len(text)
        widget.focus()
        self.notify("Message restored for editing.")

    def _run_compact(self) -> None:
        self._begin_navigation()
        self.run_worker(
            self._compact_work, group="ask", thread=True, exclusive=True, exit_on_error=False
        )

    def _compact_work(self) -> None:
        try:
            events = self.application.conversation.force_compact()
        except Exception as exc:
            record_event("command.failed", exc)
            self.call_from_thread(self._finish_navigation_error, f"Compaction failed: {exc}")
            return
        self.call_from_thread(self._after_compact, events)

    def _after_compact(self, events: list[str]) -> None:
        for event in events:
            self.add_event(event)
        self.update_status()
        self._end_navigation()
        self._drain_pending_intent()

    def _after_import(self, message: str) -> None:
        self._import_running = False
        self.add_card(message)
        self.update_status()

    def _run_import(self, argument: str) -> None:
        # 不用 POSIX shlex 拆路径，避免吃掉 Windows 反斜线。
        argument = argument.strip()
        apply = argument.endswith(" --apply")
        filename = argument[:-8].strip() if apply else argument
        if len(filename) >= 2 and filename[0] == filename[-1] and filename[0] in ("'", '"'):
            filename = filename[1:-1]
        if not filename:
            self.add_card("Usage: /import <file-or-directory> [--apply]")
            return
        path = Path(filename).expanduser()
        if apply:
            self.push_screen(PickScreen(
                "Confirm Memory Import",
                [("Cancel", False), ("Import and save", True)],
                partial(self._import_confirmed, path),
                detail=(
                    f"Source: {path}\n\n"
                    "This batch will write admitted memories directly to the Vault. "
                    "The configured model may be called and may incur charges.\n\n"
                    "If a later file or chunk fails, completed writes are retained. "
                    "Cancel starts neither document parsing nor model calls."
                ),
            ))
            return
        self._start_import(path, False)

    def _import_confirmed(self, path: Path, confirmed) -> None:
        if confirmed is not True:
            self.add_card("Import cancelled; no files were analyzed and no memories were written.")
            self.query_one("#input", Input).focus()
            return
        self._start_import(path, True)

    def _start_import(self, path: Path, apply: bool) -> None:
        if self._import_running:
            self.notify("An import is already running.")
            return
        self._import_running = True
        self.add_card("Starting import: parsing is local; extraction calls the configured model and may incur charges. " + ("Memories will be saved." if apply else "Preview only; memories will not be saved."))
        self.run_worker(partial(self._import_work, path, apply),
                        group="import", thread=True, exit_on_error=False)

    def _import_work(self, path: Path, apply: bool) -> None:
        try:
            result = self.application.memory.import_path(
                path, apply, on_progress=lambda item: self.call_from_thread(self._show_import_progress, item)
            )
        except Exception as exc:
            record_event("command.failed", exc)
            self.call_from_thread(self._after_import, f"Import failed: {exc}")
            return
        self.call_from_thread(self.refresh_pending)
        message = "Import complete." if apply else "Preview complete; use /import <path> --apply to analyze again and save."
        if result.errors:
            message += f" {len(result.errors)} file/chunk(s) failed; completed changes were retained."
        self.call_from_thread(self._after_import, message)

    def _show_import_progress(self, progress) -> None:
        if progress.chunk:
            self.add_card(escape(f"Analyzing {progress.file}: chunk {progress.chunk}/{progress.total_chunks}"))
        if progress.error:
            self.add_card(escape(f"{progress.file} import failed: {progress.error}"))
            return
        if not progress.chunk:
            return
        lines = [
            f"{'Processed' if progress.apply else 'Preview'}: saved {progress.saved} · "
            f"pending {progress.queued} · duplicates {progress.duplicates} · blocked {progress.blocked}"
        ]
        lines += [f"[{topic}] {text}" for topic, text in progress.admitted]
        lines += [f"Pending [{topic}] {text}" for topic, text in progress.pending]
        self.add_card(escape("\n".join(lines)))

    # ---------- 设置类选择器（逻辑在 application/settings.py） ----------

    def _open_models(self, refresh: bool = False) -> None:
        # keyring/供应商元数据查询可能很慢，不能在 UI 线程先算选项再开选择器。
        self.run_worker(
            partial(self._open_models_work, refresh),
            group="models",
            thread=True,
            exit_on_error=False,
        )

    def _open_models_work(self, refresh: bool) -> None:
        try:
            catalog = {} if refresh else self.application.settings.model_catalog(cached_only=True)
            entries = self._model_entries(catalog, capabilities=False)
        except Exception as exc:
            record_event("command.failed", exc)
            self.call_from_thread(self._background_error, f"Loading models failed: {exc}")
            return
        self.call_from_thread(self._show_model_picker, entries, True, refresh)

    def _model_entries(
        self, fetched: dict[str, list[str]] | list[str] | None = None, *, capabilities: bool = True
    ) -> list[tuple]:
        options = self.application.settings.model_options(fetched, capabilities)
        return [
            (f"Recent · {option.label}" if option.recent else option.label,
             (option.provider, option.model), option.current)
            for option in options
        ]

    def _show_model_picker(
        self,
        entries: list[tuple],
        background_refresh: bool = False,
        refresh: bool = False,
    ) -> PickScreen:
        self._drain_pending_intent()
        picker = ModelPickerScreen(
            f"Current model: {self.application.conversation.model_label}"
            + (" · Loading API model lists…" if background_refresh else ""),
            entries,
            self._model_picked,
            searchable=True,
        )
        self.push_screen(picker)
        if background_refresh:
            self._refresh_models(picker, refresh)
        return picker

    def _refresh_models(self, picker: PickScreen, refresh: bool = False) -> None:
        self.run_worker(
            partial(self._refresh_models_work, picker, refresh),
            group="models",
            thread=True,
            exit_on_error=False,
        )

    def _refresh_models_work(self, picker: PickScreen, refresh: bool = False) -> None:
        try:
            catalog = self.application.settings.model_catalog(refresh=refresh)
            entries = self._model_entries(catalog)
        except Exception as exc:
            record_event("command.failed", exc)
            self.call_from_thread(self._apply_fetched_models, picker, [], "Model discovery failed")
            return
        unavailable = [provider for provider, models in catalog.items() if not models]
        status = ("No configured providers" if not catalog else
                  f"No API models: {', '.join(unavailable)}" if unavailable else "")
        self.call_from_thread(self._apply_fetched_models, picker, entries, status)

    def _apply_fetched_models(self, picker: PickScreen, entries: list[tuple], status: str = "") -> None:
        # 异步刷新完成时用户可能已离开 /model；只更新仍然打开的这一屏。
        if self.screen is not picker:
            return
        picker.update_options(entries)
        title = f"Current model: {self.application.conversation.model_label}"
        picker.query_one(".pick-title", Static).update(f"{title} · {status}" if status else title)

    def _model_picked(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        provider, model = value
        if provider == "__providers__":
            self.push_screen(PickScreen(
                "Providers (Esc to return to models)",
                [("Add provider", "add"), ("Update provider key", "update"), ("Set provider Base URL", "endpoint"),
                 ("Remove provider", "remove")],
                self._provider_management_picked,
            ))
            return
        if provider == "__refresh__":
            self._open_models(True)
            return
        if provider == "__custom__":
            current = self.application.config.llm
            self.push_screen(
                TextInputScreen("Model name", default=current.model, on_result=self._model_custom)
            )
            return
        provider_action = {
            "__add_provider__": "add",
            "__update_provider__": "update",
            "__remove_provider__": "remove",
        }.get(provider)
        if provider_action:
            self._manage_providers(provider_action)
            return
        message = self.application.settings.apply_model(provider, model).message
        self.add_card(message)
        self.update_status()
        self.query_one("#input", Input).focus()

    def _provider_management_picked(self, action: str | None) -> None:
        if action is None:
            self._open_models()
        else:
            self._manage_providers(action)

    def _manage_providers(self, action: str) -> None:
        options = self.application.settings.provider_options()
        current = self.application.config.llm.provider
        if action == "add":
            candidates = [option for option in options if not option.configured]
            title = "Add provider"
            empty = "All built-in providers already have API keys."
        elif action == "update":
            candidates = [option for option in options if option.configured]
            title = "Update provider key"
            empty = "No built-in provider has a saved API key yet."
        elif action == "endpoint":
            candidates = options
            title = "Set provider Base URL"
            empty = "No built-in providers."
        else:
            candidates = [option for option in options if option.configured and option.key != current]
            title = "Remove provider"
            empty = "Switch to another model before removing the current provider."
        if not candidates:
            self.add_card(empty)
            self.query_one("#input", Input).focus()
            return
        rows = [
            (
                f"{option.label} · {'configured' if option.configured else 'add provider'}",
                (option.key, option.label, option.configured),
                option.current,
            )
            for option in candidates
        ]
        self.push_screen(PickScreen(title, rows, partial(self._provider_selected, action)))

    def _provider_selected(self, action: str, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        provider_key, label, _ = value
        if action == "endpoint":
            self.push_screen(TextInputScreen(
                f"{label} Base URL", default=self.application.settings.provider_endpoint(provider_key),
                on_result=partial(self._provider_endpoint_entered, provider_key),
            ))
            return
        if action == "remove":
            self.push_screen(
                PickScreen(
                    f"Remove {label} provider?\nSaved provider credentials and its API key may be removed.",
                    [("Cancel", False), ("Remove provider", True)],
                    partial(self._remove_provider_result, provider_key),
                )
            )
            return
        self.push_screen(
            TextInputScreen(
                f"{label} API key",
                password=True,
                on_result=partial(self._provider_key_entered, action, provider_key),
            )
        )

    def _provider_key_entered(self, action: str, provider_key: str, api_key) -> None:
        if not api_key:
            self.query_one("#input", Input).focus()
            return
        if action == "add":
            result = self.application.settings.configure_provider(provider_key, api_key)
        else:
            result = self.application.settings.update_provider_key(provider_key, api_key)
        self.add_card(result.message)
        if result.ok:
            self._open_models(refresh=True)
        else:
            self.query_one("#input", Input).focus()

    def _provider_endpoint_entered(self, provider: str, endpoint: str | None) -> None:
        if endpoint is None:
            self.query_one("#input", Input).focus()
            return
        result = self.application.settings.set_provider_endpoint(provider, endpoint)
        self.add_card(result.message)
        if result.ok:
            self._open_models(refresh=True)
        else:
            self.query_one("#input", Input).focus()

    def _remove_provider_result(self, provider_key: str, confirmed) -> None:
        if confirmed is not True:
            self.query_one("#input", Input).focus()
            return
        message = self.application.settings.remove_provider(provider_key).message
        self.add_card(message)
        self.update_status()
        self.query_one("#input", Input).focus()

    def _model_custom(self, name) -> None:
        if not name:
            self.query_one("#input", Input).focus()
            return
        current = self.application.config.llm
        message = self.application.settings.apply_model(current.provider, name).message
        self.add_card(message)
        self.update_status()
        self.query_one("#input", Input).focus()

    def _pick_reasoning(self) -> None:
        options = self.application.settings.reasoning_options()
        if not options:
            self.add_card(f"{self.application.conversation.model_label} does not support reasoning effort; use /model to switch to a reasoning-capable model.")
            return
        title, current = self.application.settings.reasoning_selection()
        entries = [(label, value, value == current) for value, label in options]
        self.push_screen(PickScreen(f"{title}: {current or 'default'}", entries, self._reasoning_picked))

    def _reasoning_picked(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        self.add_card(self.application.settings.apply_reasoning(value).message)
        self.query_one("#input", Input).focus()

    def _pick_context(self) -> None:
        if self._context_loading:
            return
        self._context_loading = True
        # 模型元数据首次读取会导入 LiteLLM，必须离开 UI 线程，避免整屏卡住。
        self.run_worker(self._pick_context_work, group="context", thread=True, exit_on_error=False)

    def _pick_context_work(self) -> None:
        try:
            options = self.application.settings.context_options()
        except Exception as exc:
            record_event("command.failed", exc)
            self.call_from_thread(self._context_failed, str(exc))
            return
        self.call_from_thread(self._show_context_picker, options)

    def _show_context_picker(self, options: list[int]) -> None:
        self._context_loading = False
        current = self.application.settings.context_window
        entries = [(format_tokens(size), size, size == current) for size in options]
        entries.append(("Custom…", 0, False))
        self.push_screen(PickScreen(f"Context window: {format_tokens(current)}", entries, self._context_picked))

    def _context_failed(self, message: str) -> None:
        self._context_loading = False
        self._background_error(f"Loading context options failed: {message}")

    def _context_picked(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        if value == 0:
            current = self.application.settings.context_window
            self.push_screen(
                TextInputScreen("Context window (tokens)", default=str(current), on_result=self._context_custom)
            )
            return
        self.add_card(self.application.settings.apply_context(value).message)
        self.update_status()
        self.query_one("#input", Input).focus()

    def _context_custom(self, value) -> None:
        try:
            # isdigit 会放行 "①" 这类 Unicode 数字而 int() 抛错，改用 int 校验。
            size = int(value) if value else 0
        except ValueError:
            size = 0
        if size <= 0:
            self.add_card("Context window must be a positive integer (tokens).")
            self.query_one("#input", Input).focus()
            return
        self.add_card(self.application.settings.apply_context(size).message)
        self.update_status()
        self.query_one("#input", Input).focus()
