"""在可用的本地端口运行内部常驻服务。"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..infra.observability import diagnostics
from .endpoint import client_paths
from .server import serve_resident


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    paths = client_paths(args.config)
    # 常驻后端独立启动，与客户端使用不同日志，避免多个进程轮转同一文件。
    diagnostics.start_diagnostics(paths.log_dir / "backend.log", latency_name="backend-latency.log")
    try:
        serve_resident(args.config)
    finally:
        diagnostics.stop_diagnostics()


if __name__ == "__main__":
    main()
