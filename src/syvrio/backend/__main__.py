"""在可用的本地端口运行内部常驻服务。"""

import argparse
from pathlib import Path

from .server import serve_resident


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--vault", type=Path, required=True)
    args = parser.parse_args()
    serve_resident(args.config, args.vault)


if __name__ == "__main__":
    main()
