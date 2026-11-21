"""Read the published profile and submit manual Dream commands to the backend."""

from __future__ import annotations

from textual.widgets import Input

from .chat_widgets import ProfileScreen


class ChatReflectionMixin:
    def _reflection_command(self, mode: str, argument: str) -> None:
        if mode == "profile":
            if argument:
                self.add_card("Use /dream to update the profile.")
                return
            profile = self.application.memory.profile()
            self.push_screen(
                ProfileScreen(
                    profile.body,
                    profile.status,
                    profile.stale,
                    profile.generated_at,
                    profile.fact_count,
                    profile.unique_count,
                ),
                lambda _: self.query_one("#input", Input).focus(),
            )
            return
        if argument == "cancel":
            self.application.reflection.cancel()
            self.add_card("Dream cancelled.")
        elif argument == "status":
            status = self.application.reflection.status()
            self.add_card(f"Dream {status['state']} · {status.get('proposed', 0)} new hypotheses.")
        else:
            try:
                budget = None if argument in {"", "resume"} else int(argument)
                job = self.application.reflection.run("dream", budget)
            except ValueError:
                self.add_card("Usage: /dream [0–20|resume|cancel|status]")
                return
            self.add_card(f"Dream submitted ({job}).")
