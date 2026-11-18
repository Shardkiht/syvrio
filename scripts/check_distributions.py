"""Verify the Syvrio wheel and source distribution produced by uv build."""

from __future__ import annotations

import configparser
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_PACKAGES = {"quiet_memo", "quietmemo"}


def verify_distributions(directory: Path, version: str) -> None:
    stem = f"syvrio-{version}"
    wheel = f"{stem}-py3-none-any.whl"
    sdist = f"{stem}.tar.gz"
    packages = {
        path.name
        for path in directory.iterdir()
        if path.is_file() and path.name.endswith((".whl", ".tar.gz"))
    }
    if packages != {wheel, sdist}:
        raise ValueError(f"Expected {wheel} and {sdist}, found {sorted(packages)}")

    with zipfile.ZipFile(directory / wheel) as archive:
        names = archive.namelist()
        if "syvrio/__init__.py" not in names or any(
            name.split("/", 1)[0] in LEGACY_PACKAGES for name in names
        ):
            raise ValueError("Wheel must ship syvrio without legacy packages")
        entry_point = f"{stem}.dist-info/entry_points.txt"
        if [name for name in names if name.endswith(".dist-info/entry_points.txt")] != [
            entry_point
        ]:
            raise ValueError("Wheel must ship one matching entry_points.txt")
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(archive.read(entry_point).decode("utf-8"))
        if not parser.has_section("console_scripts") or dict(parser.items("console_scripts")) != {
            "syvrio": "syvrio.app.cli:main"
        }:
            raise ValueError("Wheel must expose only the syvrio console script")

    with tarfile.open(directory / sdist, "r:gz") as archive:
        names = archive.getnames()
        source = f"{stem}/src/"
        if source + "syvrio/__init__.py" not in names or any(
            name[len(source) :].split("/", 1)[0] in LEGACY_PACKAGES
            for name in names
            if name.startswith(source)
        ):
            raise ValueError("Source distribution must ship syvrio without legacy packages")


if __name__ == "__main__":
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    try:
        verify_distributions(ROOT / "dist", project["version"])
    except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError, configparser.Error) as exc:
        raise SystemExit(f"Invalid distribution: {exc}") from exc
    print(f"Validated Syvrio {project['version']} wheel and source distribution")
