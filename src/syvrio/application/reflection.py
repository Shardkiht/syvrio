"""提交 Dream 请求并展示常驻调度器的状态。"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from ..infra.time import format_local_timestamp
from ..memory.background import BackgroundMemory
from .commands import CommandResult, is_command_form


def _local_time(timestamp: float | None) -> str | None:
    return (
        format_local_timestamp(datetime.fromtimestamp(timestamp, UTC).isoformat())
        if timestamp
        else None
    )


class ReflectionUseCases:
    def __init__(self, background: BackgroundMemory | None):
        self.background = background

    def command(self, argument: str) -> CommandResult:
        if not isinstance(argument, str) or not is_command_form("dream", argument.strip()):
            return CommandResult.invalid("dream")
        action = argument.strip().lower() or "run"
        if self.background is None:
            return CommandResult(
                "error", "dream", "dream", "Open Syvrio with its resident service to use Dream."
            )

        # 后台线程可能立即完成，须在任务推进前取得回执。
        work = self.background.service.work
        with work.write() if action == "run" else work.read():
            before = self.background.dream_status()
            status = before
            if action == "run":
                self.background.request_dream(retry_now=True)
                status = self.background.dream_status()

        state = status["state"]
        next_at = status["next_at"] if status["enabled"] else None
        retry_at = status.get("retry_at", 0)
        waiting = state == "queued" and retry_at > time.time()
        error_type = status.get("error") if state in {"queued", "failed", "paused"} else None
        if error_type and error_type not in (
            "ValidationError",
            "TimeoutError",
            "DreamOutputTruncated",
        ):
            error_type = "DreamError"
        pause_reason = status["pause_reason"]
        run_hint = {
            "budget_disabled": "Dream is paused because the backend setting dream.default_budget is 0.",
            "budget_exhausted": "The run used its request budget. Run Dream to continue.",
            "output_limit": "The model reached its output limit. Running Dream again retries the current batch.",
        }.get(pause_reason)
        if not run_hint and not status["can_run"] and state not in {"running", "queued"}:
            run_hint = "Set dream.default_budget above 0 to run Dream."
        data = {
            "kind": "dream",
            "state": state,
            "pause_reason": pause_reason,
            "can_run": status["can_run"],
            "run_hint": run_hint,
            "enabled": status["enabled"],
            "next_at": next_at,
            "phase": status.get("phase"),
            "retry_at": retry_at if waiting else None,
            "error_type": error_type,
            "error_at": status.get("updated_at") if error_type else None,
            "failures": status.get("failures", 0) if error_type else 0,
        }
        if action == "status":
            data["rows"] = [{"id": "run", "text": "Run Dream"}] if status["can_run"] else []
            lines = [
                f"Automatic Dream: {'enabled' if status['enabled'] else 'disabled'}",
                f"State: {state}",
                f"Next: {_local_time(next_at) or '-'}",
            ]
            if status.get("phase") and state in {
                "queued",
                "running",
                "paused",
                "cancelled",
                "failed",
            }:
                lines.append(f"Phase: {status['phase']}")
            if run_hint:
                lines.append(run_hint)
            message = "\n".join(lines)
        elif state == "paused":
            message = run_hint or "Dream is paused."
        elif state == "running":
            message = "Dream is already running."
        elif before["state"] == "queued" and before.get("retry_at") and not retry_at:
            message = "The existing Dream run is queued to retry now."
        elif before["state"] == "queued":
            message = "Dream is already queued."
        else:
            message = "Dream submitted and queued."
        if error_type == "DreamOutputTruncated" and not run_hint:
            message += "\nThe model reached its output limit."
        elif error_type and error_type != "DreamOutputTruncated":
            message += f"\nLast error: {error_type}."
        if waiting:
            message += f"\nScheduled retry: {_local_time(retry_at)}."
        return CommandResult("ok", "dream", "dream", message, data=data)
