"""Custom browser hostnames, in addition to the loopback hosts."""

from __future__ import annotations

import os


def web_hosts(extra: list[str] | None = None) -> list[str]:
    return [*(extra or ()), *os.environ.get("SYVRIO_WEB_ALLOWED_HOSTS", "").split(",")]
