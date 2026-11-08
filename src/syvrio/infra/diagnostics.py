from __future__ import annotations

import atexit
import json
import logging
import os
import platform
import re
import sys
import traceback
from datetime import datetime, timezone
from logging.handlers import QueueListener, RotatingFileHandler
from pathlib import Path
from queue import Full, Queue
from threading import RLock, current_thread

from .. import __version__
from .config import ConfigError, load_config


_EVENTS = frozenset({
    "app.start", "app.stop", "cli.failed", "tui.failed", "chat.failed",
    "search.failed", "catchup.failed", "session.failed", "session.reload_failed",
    "queue.failed", "usage.failed", "reflection.failed", "reading.changed",
    "generation.started", "generation.finished", "turn.unbound", "tui.finalize_failed",
    "session.scan_failed", "session.delete_failed",
    "command.failed", "cursor.changed", "undo.failed", "retrieval.vector_dimension_failed",
    "git_sync.failed", "git_sync.scheduler_failed", "git_sync.status_failed",
    "reflection.schedule_failed", "reflection.state_failed", "reflection.summary_failed",
    "latency.turn.started", "latency.turn.finished",
    "latency.recall.started", "latency.recall.finished",
    "latency.context.started", "latency.context.finished",
    "latency.model.started", "latency.model.request", "latency.model.stream_open",
    "latency.model.first_chunk", "latency.model.first_reasoning", "latency.model.first_text",
    "latency.model.first_tool", "latency.model.finished",
    "latency.tools.started", "latency.tool.finished", "latency.tools.finished",
    "latency.completion.started", "latency.completion.finished",
})
_FIELDS = frozenset({
    "follow", "selecting", "selected", "grabbed", "scroll_y", "scroll_max",
    "streaming", "queued", "busy",
    "native", "suspended", "cursor_x", "cursor_y", "elapsed_ms",
})
_LATENCY_FIELDS = frozenset({
    "turn", "step", "call", "at_ms", "elapsed_ms", "ok", "cancelled", "estimated_tokens",
    "prompt_tokens", "completion_tokens", "compacted", "tool_count", "tool_index",
})
_lock = RLock()
_listener: QueueListener | None = None
_handler: RotatingFileHandler | None = None
_queue: Queue[logging.LogRecord] | None = None
_path: Path | None = None


class _QuietFileHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # Logging must neither interrupt chat nor dump payloads onto MCP stdout.
        pass


def start_diagnostics(config_file: Path | None = None) -> Path | None:
    """Start bounded, background-only diagnostics inside Runtime."""
    global _listener, _handler, _queue, _path
    with _lock:
        if _listener is not None:
            return _path
        handler = None
        try:
            paths = load_config(config_file).paths(config_file)
            paths.validate()
            path = paths.log_dir / "syvrio.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = _QuietFileHandler(path, maxBytes=2 * 1024 * 1024,
                                        backupCount=3, encoding="utf-8")
            latency_handler = _QuietFileHandler(path.with_name("latency.log"), maxBytes=2 * 1024 * 1024,
                                                backupCount=3, encoding="utf-8")
        except (OSError, ConfigError, ValueError):
            if handler is not None:
                handler.close()
            print("Local diagnostics unavailable: cannot open log file.", file=sys.stderr)
            return None
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        latency_handler.setFormatter(formatter)
        handler.addFilter(lambda record: record.name == "syvrio.diagnostics")
        latency_handler.addFilter(logging.Filter("syvrio.latency"))
        _queue = Queue(maxsize=1024)
        _handler = handler
        _listener = QueueListener(_queue, handler, latency_handler)
        _listener.start()
        _path = path
        record_event("app.start")
        return path


def stop_diagnostics() -> None:
    """Drain queued events before closing the file; safe to call repeatedly."""
    global _listener, _handler, _queue, _path
    with _lock:
        if _listener is None:
            return
        record_event("app.stop")
        # QueueListener.stop uses put_nowait for its sentinel: drain first so a
        # full queue cannot prevent shutdown. No producers enqueue under this lock.
        _queue.join()
        _listener.stop()
        for handler in _listener.handlers:
            handler.close()
        _listener = _handler = _queue = _path = None


def _safe_traceback(error: BaseException | str) -> str:
    """Retain chained tracebacks and reasons, without locals or credentials."""
    detail = (error if isinstance(error, str) else
              "".join(traceback.TracebackException.from_exception(error, capture_locals=False).format()))
    for name, value in list(os.environ.items()):
        if re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD)$", name, re.I) and len(value) >= 4:
            detail = detail.replace(value, "[REDACTED]")
    detail = re.sub(r"(?i)(bearer\s+)[^\s\"',}]+", r"\1[REDACTED]", detail)
    detail = re.sub(
        r"(?i)((?:api[_ -]?key|access[_ -]?token|secret|password|passwd|密码|口令|验证码|私钥|密钥|秘钥|令牌|凭据)"
        r"[\"']?\s*(?:是|为|[:：=])\s*[\"']?)[^\s\"',;}]+",
        r"\1[REDACTED]", detail)
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", detail)
    return detail[:32768]


def record_event(event: str, error: BaseException | str | None = None, **state: bool | int | float) -> None:
    """Write allowlisted events and state. Latency entries never include error text."""
    if event not in _EVENTS:
        return
    with _lock:
        if _queue is None:
            return
        latency = event.startswith("latency.")
        fields = _LATENCY_FIELDS if latency else _FIELDS
        payload = {
            "time": datetime.now(timezone.utc).isoformat(), "event": event,
            "pid": os.getpid(), "version": __version__, "platform": platform.system(),
            "python": platform.python_version(),
            "thread": current_thread().name, "level": "ERROR" if error is not None else "INFO",
            "state": {key: value for key, value in state.items()
                      if key in fields and type(value) in (bool, int, float)},
        }
        if error is not None and not latency:
            payload["traceback"] = _safe_traceback(error)
        record = logging.LogRecord("syvrio.latency" if latency else "syvrio.diagnostics", logging.INFO, "", 0,
                                   json.dumps(payload, ensure_ascii=False), (), None)
        try:
            _queue.put_nowait(record)
        except Full:
            pass  # Slow disks must not block the UI; diagnostics are best effort.


atexit.register(stop_diagnostics)
