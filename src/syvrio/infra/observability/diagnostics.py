from __future__ import annotations

import atexit
import json
import logging
import os
import platform
import re
import sys
import traceback
from logging.handlers import QueueListener, RotatingFileHandler
from pathlib import Path
from queue import Full, Queue
from threading import RLock, current_thread

from ... import __version__
from ..time import serialize_timestamp, utc_now

_EVENTS = frozenset(
    {
        "app.start",
        "app.stop",
        "cli.failed",
        "update.failed",
        "tui.failed",
        "chat.failed",
        "chat.history_write_failed",
        "search.failed",
        "catchup.failed",
        "session.failed",
        "session.title_failed",
        "session.reload_failed",
        "queue.failed",
        "usage.failed",
        "reflection.failed",
        "reading.changed",
        "generation.started",
        "generation.finished",
        "turn.unbound",
        "tui.finalize_failed",
        "session.scan_failed",
        "session.delete_failed",
        "session.sources_unavailable",
        "web.startup_hook_failed",
        "backend.startup_hook_failed",
        "degraded.keyring",
        "degraded.tiktoken",
        "degraded.gitsync_state",
        "web.shutdown_distill_failed",
        "web.history_unreadable",
        "web.switch_failed",
        "web.worker_failed",
        "web.edit_admission_failed",
        "web.submission_receipt_failed",
        "web.turn_failed",
        "web.unhandled",
        "command.failed",
        "cursor.changed",
        "undo.failed",
        "retrieval.vector_dimension_failed",
        "git_sync.failed",
        "git_sync.scheduler_failed",
        "git_sync.status_failed",
        "reflection.schedule_failed",
        "reflection.state_failed",
        "reflection.summary_failed",
        "latency.turn.started",
        "latency.turn.finished",
        "latency.recall.started",
        "latency.recall.finished",
        "latency.context.started",
        "latency.context.finished",
        "latency.model.started",
        "latency.model.request",
        "latency.model.stream_open",
        "latency.model.first_chunk",
        "latency.model.first_reasoning",
        "latency.model.first_text",
        "latency.model.first_tool",
        "latency.model.finished",
        "latency.tools.started",
        "latency.tool.finished",
        "latency.tools.finished",
        "latency.completion.started",
        "latency.completion.finished",
    }
)
_FIELDS = frozenset(
    {
        "follow",
        "selecting",
        "selected",
        "grabbed",
        "scroll_y",
        "scroll_max",
        "streaming",
        "queued",
        "busy",
        "native",
        "suspended",
        "cursor_x",
        "cursor_y",
        "elapsed_ms",
        # 字符串状态用于记录触发 cli.failed 的 CLI 命令。
        "command",
    }
)
# 仅列出的状态键可以保存字符串，其他允许的键只接受数值或布尔值，
# 避免自由文本意外进入耗时诊断数据。
_STR_FIELDS = frozenset({"command"})
_LATENCY_FIELDS = frozenset(
    {
        "turn",
        "step",
        "call",
        "at_ms",
        "elapsed_ms",
        "ok",
        "cancelled",
        "estimated_tokens",
        "prompt_tokens",
        "completion_tokens",
        "compacted",
        "tool_count",
        "tool_index",
    }
)
_lock = RLock()
_listener: QueueListener | None = None
_handler: RotatingFileHandler | None = None
_queue: Queue[logging.LogRecord] | None = None
_path: Path | None = None
_RECORDED_ONCE: set[str] = set()


class _QuietFileHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # 记录日志不能打断聊天，也不能把消息内容写到 MCP 标准输出。
        pass


def start_diagnostics(path: Path) -> Path | None:
    """在 Runtime 内启动由后台处理且有队列上限的诊断记录。"""
    global _listener, _handler, _queue, _path
    with _lock:
        if _listener is not None:
            return _path
        handler = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = _QuietFileHandler(
                path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            latency_handler = _QuietFileHandler(
                path.with_name("latency.log"),
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        except (OSError, ValueError):
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
    """关闭文件前排空事件队列，可以重复调用。"""
    global _listener, _handler, _queue, _path
    with _lock:
        if _listener is None:
            return
        record_event("app.stop")
        # QueueListener.stop 使用 put_nowait 放入结束标记。先排空队列，避免满队列阻碍关闭。
        # 持锁期间，生产者无法向队列写入。
        _queue.join()
        _listener.stop()
        for handler in _listener.handlers:
            handler.close()
        _listener = _handler = _queue = _path = None


def _safe_traceback(error: BaseException | str) -> str:
    """保留异常链、调用栈和原因，排除局部变量与凭据。"""
    detail = (
        error
        if isinstance(error, str)
        else "".join(
            traceback.TracebackException.from_exception(error, capture_locals=False).format()
        )
    )
    for name, value in list(os.environ.items()):
        if re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD)$", name, re.I) and len(value) >= 4:
            detail = detail.replace(value, "[REDACTED]")
    detail = re.sub(r"(?i)(bearer\s+)[^\s\"',}]+", r"\1[REDACTED]", detail)
    detail = re.sub(
        r"(?i)((?:api[_ -]?key|access[_ -]?token|secret|password|passwd|密码|口令|验证码|私钥|密钥|秘钥|令牌|凭据)"
        r"[\"']?\s*(?:是|为|[:：=])\s*[\"']?)[^\s\"',;}]+",
        r"\1[REDACTED]",
        detail,
    )
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", detail)
    return detail[:32768]


def record_event(
    event: str, error: BaseException | str | None = None, **state: bool | int | float | str
) -> bool:
    """返回允许记录的事件是否已排队等待写入。"""
    if event not in _EVENTS:
        return False
    with _lock:
        if _queue is None:
            return False
        latency = event.startswith("latency.")
        fields = _LATENCY_FIELDS if latency else _FIELDS
        payload = {
            "time": serialize_timestamp(utc_now()),
            "event": event,
            "pid": os.getpid(),
            "version": __version__,
            "platform": platform.system(),
            "python": platform.python_version(),
            "thread": current_thread().name,
            "level": "ERROR" if error is not None else "INFO",
            "state": {
                key: value
                for key, value in state.items()
                if key in fields
                and (
                    (event == "cli.failed" and key in _STR_FIELDS and isinstance(value, str))
                    or (key not in _STR_FIELDS and type(value) in (bool, int, float))
                )
            },
        }
        if error is not None and not latency:
            payload["traceback"] = _safe_traceback(error)
        record = logging.LogRecord(
            "syvrio.latency" if latency else "syvrio.diagnostics",
            logging.INFO,
            "",
            0,
            json.dumps(payload, ensure_ascii=False),
            (),
            None,
        )
        try:
            _queue.put_nowait(record)
        except Full:
            return False  # 磁盘写入缓慢时仍须保持界面响应。
        return True


def record_once(event: str, error: BaseException | str) -> None:
    """同一降级事件在每个进程内最多记录一次。

    密钥环故障、tiktoken 下载失败等回退可能发生在频繁调用的路径上。
    需要去重以免持续故障刷满日志。
    """
    with _lock:
        if event not in _RECORDED_ONCE and record_event(event, error):
            _RECORDED_ONCE.add(event)


atexit.register(stop_diagnostics)
