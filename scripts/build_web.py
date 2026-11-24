"""构建 React 客户端，将产物放入 Python 包中。

    python scripts/build_web.py            # 用 pnpm 安装依赖、构建、复制并校验
    python scripts/build_web.py --check    # 只校验已放入包内的资源
    python scripts/build_web.py --wheel dist/*.whl   # 同时校验构建好的 wheel

构建失败或资源不完整时以非零状态退出，防止发布缺少网页资源的 wheel。
Git 按 .gitignore 忽略暂存的 static/ 目录，
hatchling 通过 artifacts 将其纳入 wheel。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
DIST = WEB / "dist"
STATIC = ROOT / "src" / "syvrio" / "app" / "web" / "static"


def run(command: str) -> None:
    print(f"> {command}", flush=True)
    # shell=True 使 Windows 能找到 corepack.cmd，Linux 能找到 corepack。
    subprocess.run(command, cwd=WEB, shell=True, check=True)


def verify(directory: Path) -> None:
    index = directory / "index.html"
    if not index.is_file():
        raise SystemExit(f"Missing Web bundle: {index}")
    html = index.read_text(encoding="utf-8")
    if "assets/" not in html or "<div id=" not in html:
        raise SystemExit(f"index.html does not reference the built bundle: {index}")
    if not list((directory / "assets").glob("*.js")):
        raise SystemExit(f"Missing JavaScript bundle in {directory / 'assets'}")
    if not list((directory / "assets").glob("*.css")):
        raise SystemExit(f"Missing stylesheet bundle in {directory / 'assets'}")


def verify_wheel(path: Path) -> None:
    """校验发布的 wheel 可在未安装 Node.js 时提供网页界面。"""
    if not path.is_file():
        raise SystemExit(f"Missing wheel: {path}")
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    prefix = "syvrio/app/web/static/"
    if prefix + "index.html" not in names:
        raise SystemExit(f"Wheel is missing Web index.html: {path}")
    if not any(n.startswith(prefix + "assets/") and n.endswith(".js") for n in names):
        raise SystemExit(f"Wheel is missing the JavaScript bundle: {path}")
    if not any(n.startswith(prefix + "assets/") and n.endswith(".css") for n in names):
        raise SystemExit(f"Wheel is missing the stylesheet bundle: {path}")
    print(f"Web assets OK in wheel: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="only verify staged assets")
    parser.add_argument(
        "--wheel",
        nargs="+",
        metavar="WHEEL",
        help="also verify that built wheel(s) contain the Web assets",
    )
    args = parser.parse_args()

    if args.wheel:
        verify(STATIC)
        for wheel in (Path(item) for item in args.wheel):
            verify_wheel(wheel)
        return 0
    if args.check:
        verify(STATIC)
        print(f"Web assets OK: {STATIC}")
        return 0
    if not (WEB / "package.json").is_file():
        raise SystemExit(f"Missing frontend project: {WEB}")
    run("corepack pnpm install --frozen-lockfile")
    run("corepack pnpm build")
    verify(DIST)
    if STATIC.exists():
        if not STATIC.resolve().is_relative_to(ROOT.resolve()):
            raise SystemExit("Refusing to clean a path outside the repository")
        shutil.rmtree(STATIC)
    shutil.copytree(DIST, STATIC)
    verify(STATIC)
    print(f"Staged Web assets into {STATIC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
