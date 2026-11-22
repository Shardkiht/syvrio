"""Verify the Syvrio wheel and source distribution produced by uv build."""

from __future__ import annotations

import configparser
import tarfile
import zipfile
from email.parser import BytesHeaderParser
from pathlib import Path
from runpy import run_path

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
        metadata = archive.read(f"{stem}.dist-info/METADATA")
        if BytesHeaderParser().parsebytes(metadata)["Version"] != version:
            raise ValueError("Wheel metadata version must match the source version")
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
        with archive.extractfile(f"{stem}/PKG-INFO") as metadata_file:
            if BytesHeaderParser().parse(metadata_file)["Version"] != version:
                raise ValueError("Source package metadata version must match the source version")
        names = archive.getnames()
        source = f"{stem}/src/"
        if source + "syvrio/__init__.py" not in names or any(
            name[len(source) :].split("/", 1)[0] in LEGACY_PACKAGES
            for name in names
            if name.startswith(source)
        ):
            raise ValueError("Source distribution must ship syvrio without legacy packages")


if __name__ == "__main__":
    version = run_path(str(ROOT / "src/syvrio/__init__.py"))["__version__"]
    try:
        verify_distributions(ROOT / "dist", version)
    except (
        OSError,
        ValueError,
        KeyError,
        zipfile.BadZipFile,
        tarfile.TarError,
        configparser.Error,
    ) as exc:
        raise SystemExit(f"Invalid distribution: {exc}") from exc
    print(f"Validated Syvrio {version} wheel and source distribution")
