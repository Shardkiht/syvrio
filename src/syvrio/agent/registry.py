"""Tool schemas, execution, and timeouts shared by the agent's integrations."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial
from threading import Event
from typing import Callable

# 进程级复用线程池：每轮 ask 新建池又不关闭会累积空闲线程。
_EXECUTOR = ThreadPoolExecutor(max_workers=4)
# Use the same limit for the network worker pool and the per-round batches.
PARALLEL_NETWORK_LIMIT = 8
_NETWORK_EXECUTOR = ThreadPoolExecutor(
    max_workers=PARALLEL_NETWORK_LIMIT,
    thread_name_prefix="syvrio-network",
)
PARALLEL_TOOLS = frozenset({"search", "fetch_web"})


@dataclass
class ToolOutcome:
    ok: bool
    content: str
    display: str
    duration_ms: int = 0
    data: dict | None = None


def cancelled_tool() -> ToolOutcome:
    return ToolOutcome(False, "Cancelled by user before execution", "Tool cancelled")


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], ToolOutcome]
    timeout: float = 60.0
    executor: str = "default"
    mutating: bool = False


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}
        self._executors = {"default": _EXECUTOR, "network": _NETWORK_EXECUTOR}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]

    def execute(self, name: str, arguments: dict) -> ToolOutcome:
        return self.start(name, arguments)()

    def start(
        self,
        name: str,
        arguments: dict,
        *,
        cancel: Event | None = None,
        shared_deadline: bool = False,
    ) -> Callable[[], ToolOutcome]:
        spec = self._tools.get(name)
        if spec is None:
            return lambda: ToolOutcome(
                ok=False,
                content=json.dumps({"error": f"未知工具：{name}"}, ensure_ascii=False),
                display=f"Unknown tool {name}",
            )
        started = time.monotonic()
        executor = self._executors.get(spec.executor, self._executors["default"])
        context = copy_context()
        finished: list[float] = []
        if shared_deadline or cancel is not None:

            def run() -> ToolOutcome:
                try:
                    if cancel is not None and cancel.is_set():
                        return cancelled_tool()
                    return spec.handler(arguments)
                finally:
                    finished.append(time.monotonic())

            future = executor.submit(partial(context.run, run))
        else:
            future = executor.submit(partial(context.run, spec.handler), arguments)
        return lambda: self._finish(spec, future, started, shared_deadline, finished)

    @staticmethod
    def _finish(
        spec, future, started: float, shared_deadline: bool, finished: list[float]
    ) -> ToolOutcome:
        name = spec.name
        timeout = spec.timeout
        if shared_deadline:
            timeout = max(0.0, started + timeout - time.monotonic())
        try:
            outcome = future.result(timeout=timeout)
            if shared_deadline and finished[0] > started + spec.timeout:
                raise FutureTimeout
        except FutureTimeout:
            cancelled = future.cancel()
            if spec.mutating and not cancelled:
                # 写入已开始后无法强杀；必须等到真实结果，不能先报失败再延迟落盘。
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome = ToolOutcome(
                        ok=False,
                        content=json.dumps({"error": str(exc)}, ensure_ascii=False),
                        display=f"{name} failed: {exc}",
                    )
            else:
                outcome = ToolOutcome(
                    ok=False,
                    content=json.dumps({"error": f"{name} 执行超时"}, ensure_ascii=False),
                    display=f"{name} timed out ({spec.timeout:.0f}s)",
                )
        except Exception as exc:
            outcome = ToolOutcome(
                ok=False,
                content=json.dumps({"error": str(exc)}, ensure_ascii=False),
                display=f"{name} failed: {exc}",
            )
        ended = finished[0] if finished else time.monotonic()
        if shared_deadline:
            ended = min(ended, started + spec.timeout)
        outcome.duration_ms = int((ended - started) * 1000)
        return outcome
