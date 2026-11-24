"""配置目录的内置默认设置、局部覆盖和可恢复写入。"""

from __future__ import annotations

import json
import re
import tomllib
from copy import deepcopy
from importlib.resources import files
from pathlib import Path

from ..errors import SyvrioError
from ..storage.files import atomic_write
from ..storage.locking import VaultLock

NAMES = ("models.toml", "chat.toml", "memory.toml", "config.toml")
MEMORY_SECTIONS = {
    "recall": (
        "auto_recall",
        "recall_rewrite",
        "recall_context_turns",
        "cue_limit",
        "rewrite_timeout",
    ),
    "distillation": ("extract_on_exit", "recall_limit"),
    "dream": ("auto_reflect", "dream_auto_interval_hours", "default_budget"),
}


class ConfigError(SyvrioError, ValueError):
    """配置文件无法读取或未通过校验。"""


def read_toml(path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: 配置文件 TOML 语法错误：{exc}") from exc
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"无法读取配置文件 {path}: {exc}") from exc


def package_defaults() -> dict:
    directory = files("syvrio.infra.config").joinpath("defaults")
    result = {}
    for name in NAMES:
        try:
            result.update(read_toml(directory.joinpath(name)))
        except ConfigError as exc:
            raise ConfigError(f"内置默认配置 {name} 缺失或损坏，请修复安装。{exc}") from exc
    return result


def merge(base: dict, overlay: dict) -> dict:
    merged = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = merge(base[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def file_for(key: str) -> str:
    if key == "llm":
        return "models.toml"
    if key == "chat":
        return "chat.toml"
    if key == "retrieval" or key in MEMORY_SECTIONS:
        return "memory.toml"
    return "config.toml"


def toml_literal(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).replace("\x7f", "\\u007f")


def _key(name: str) -> str:
    return name if re.fullmatch("[A-Za-z0-9_-]+", name) else toml_literal(name)


def encode(data: dict) -> bytes:
    lines = []

    def table(keys, values):
        if keys:
            lines.append("[" + ".".join(keys) + "]")
        for name, value in values.items():
            if not isinstance(value, dict):
                lines.append(f"{_key(name)} = {toml_literal(value)}")
        for name, value in values.items():
            if isinstance(value, dict):
                lines.append("")
                table([*keys, _key(name)], value)

    table([], data)
    return ("\n".join(lines).strip() + "\n").encode("utf-8")


class ConfigFiles:
    def __init__(self, entry: Path):
        if entry.name in NAMES[:-1]:
            raise ConfigError(f"{entry} 是分组配置，请指定同目录的入口 config.toml。")
        self.paths = {name: entry.with_name(name) for name in NAMES}
        self.paths["config.toml"] = entry
        self.lock = VaultLock(entry.with_name(".syvrio-config.lock"))
        self.journal = entry.with_name(".syvrio-config-transaction.json")

    def read(self) -> dict:
        self.recover()
        data = {}
        for name, path in self.paths.items():
            if path.exists():
                overrides = read_toml(path)
                for key in overrides:
                    if file_for(key) != name:
                        raise ConfigError(f"{path}: {key} 应位于 {file_for(key)}")
                data.update(overrides)
        return data

    def connection_locations(self, *, required: bool) -> tuple[str, str]:
        """只取连接需要的两个位置；未完成的事务按提交意图读取，留给后端恢复。"""
        root = self.paths["config.toml"]
        with self.lock:
            staged = self._pending_files().get("config.toml") if self.journal.exists() else None
            if staged is not None:
                data = tomllib.loads(staged)
            elif root.exists():
                data = read_toml(root)
            elif required:
                raise ConfigError(f"指定的入口配置文件不存在：{root}")
            else:
                data = {}
        vault, runtime = data.get("vault", ""), data.get("runtime", "")
        if not isinstance(vault, str) or not isinstance(runtime, str):
            raise ConfigError("配置中的 vault 和 runtime 必须是路径字符串。")
        return vault, runtime

    def _pending_files(self) -> dict[str, str]:
        record = json.loads(self.journal.read_bytes())
        if record["entry"] != self.paths["config.toml"].name:
            raise ConfigError(f"请先打开同一目录中的配置 {record['entry']} 以完成保存。")
        return record["files"]

    def recover(self) -> None:
        if self.journal.exists():
            for name, content in self._pending_files().items():
                atomic_write(self.paths[name], content.encode("utf-8"))
            self.journal.unlink()

    def write(self, data: dict, *, initialize: bool = False) -> None:
        updates = {}
        for name, path in self.paths.items():
            group = {k: v for k, v in data.items() if file_for(k) == name}
            if path.exists() and read_toml(path) == group:
                continue
            if not path.exists() and not group and not initialize and name != "config.toml":
                continue
            if group:
                updates[name] = encode(group).decode("utf-8")
            else:
                updates[name] = f"# {name} 的本地覆盖。未设置的字段使用内置默认值。\n"
        if updates:
            record = {"entry": self.paths["config.toml"].name, "files": updates}
            atomic_write(self.journal, json.dumps(record, ensure_ascii=False).encode("utf-8"))
            self.recover()
