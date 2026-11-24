"""除回环主机外允许浏览器使用的自定义主机名。"""

from __future__ import annotations

import os


def web_hosts(extra: list[str] | None = None) -> list[str]:
    return [*(extra or ()), *os.environ.get("SYVRIO_WEB_ALLOWED_HOSTS", "").split(",")]
