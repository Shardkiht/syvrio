"""按本次命令使用的后端配置选择本地日志目录。"""

from __future__ import annotations

import sys
from pathlib import Path

from ..infra.config import ConfigError
from ..infra.observability import diagnostics
from .endpoint import client_paths


def start_diagnostics(config_file: Path | None = None) -> Path | None:
    try:
        paths = client_paths(config_file, None)
        paths.validate()
    except (OSError, ConfigError, ValueError):
        print("Local diagnostics unavailable: cannot open log file.", file=sys.stderr)
        return None
    return diagnostics.start_diagnostics(paths.log_dir / "syvrio.log")
