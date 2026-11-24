"""按会话文件版本缓存有效附件，只解析与附件或撤回相关的记录。"""

from __future__ import annotations

import json
import mmap
import os
import re
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from ..storage.files import FileVersion, file_version
from ..storage.paths import SESSION_ID_RE


def _markers(data: mmap.mmap, *values: bytes) -> re.Pattern[bytes]:
    # 外部写入的合法 JSON 也可能把字段名或值编码为 Unicode 转义。
    if data.find(b"\\u") >= 0:
        values = (*values, b"\\u")
    return re.compile(b"|".join(re.escape(value) for value in values))


def _matching_rows(
    data: mmap.mmap, pattern: re.Pattern[bytes], start: int = 0
) -> Iterator[tuple[int, dict]]:
    while match := pattern.search(data, start):
        begin = max(start, data.rfind(b"\n", start, match.start()) + 1)
        end = data.find(b"\n", match.end())
        if end < 0:
            end = len(data)
        try:
            row = json.loads(data[begin:end])
        except ValueError:
            # 损坏的末行可能仍在追加；中间的损坏记录需有明确恢复标记。
            if end < len(data) - 1:
                number = data[:begin].count(b"\n") + 1
                if not any(
                    row.get("type") == "recovery" and row.get("ignored_line") == number
                    for _, row in _matching_rows(data, _markers(data, b'"recovery"'), end + 1)
                ):
                    raise
        else:
            if isinstance(row, dict):
                yield begin, row
        start = end + 1


class _VersionChanged(Exception):
    pass


@lru_cache(maxsize=32)
def _manifest(path: Path, version: FileVersion) -> dict:
    if not version[2]:
        return {}
    with path.open("rb") as source:
        if file_version(os.fstat(source.fileno())) != version:
            raise _VersionChanged
        with mmap.mmap(source.fileno(), version[2], access=mmap.ACCESS_READ) as data:
            first_end = data.find(b"\n")
            first = json.loads(data[:first_end] if first_end >= 0 else data[:])
            if first.get("type") != "meta" or first.get("id") != path.stem:
                raise ValueError("Session ID differs from archive filename.")
            return _scan(data)


def _scan(data: mmap.mmap) -> dict:
    # 撤回会截断目标之后的记录；目标本身也可能没有附件。
    undos = {
        offset: row["message_id"]
        for offset, row in _matching_rows(data, _markers(data, b'"undo"'))
        if row.get("type") == "undo"
        and isinstance(row.get("message_id"), str)
        and SESSION_ID_RE.fullmatch(row["message_id"])
    }
    keys = {b'"attachments"'}
    keys.update(b'"' + target.encode("ascii") + b'"' for target in undos.values())
    pattern = _markers(data, *keys)
    events = {offset: (target, None) for offset, target in undos.items()}
    for offset, row in _matching_rows(data, pattern):
        if row.get("type") == "message" and row.get("role") == "user":
            events[offset] = (row["id"], row.get("attachments", []))
    active = {}
    for offset in sorted(events):
        mid, files = events[offset]
        if files is not None:
            active[mid] = files
        elif mid in active:
            while active.popitem()[0] != mid:
                pass
    return {
        (mid, item["id"]): item
        for mid, files in active.items()
        for item in files
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def attachment_metadata(path: Path, message_id: str, attachment_id: str) -> dict | None:
    while True:
        version = file_version(path.stat())
        try:
            return _manifest(path, version).get((message_id, attachment_id))
        except _VersionChanged:
            continue
