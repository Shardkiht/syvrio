"""Parse v2 Markdown using CommonMark blocks; keep source offsets for writes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode
from pydantic import BaseModel, ConfigDict, Field

from .models import Entry, FieldNode

_MARKDOWN = MarkdownIt("commonmark")
_SECTIONS = {"**Core**": "core", "**Context**": "context", "**Extensions**": "extensions"}
_KEY = r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*"
_FIELD = re.compile(rf"`(?P<key>{_KEY})`:[ \t]*(?P<value>[^\n]*)")
_ITEM = re.compile(r"^([ \t]*[-+*][ \t]+)")
_ESCAPED = "~syvrio-escaped~"
CORE = frozenset(("id", "status", "source", "confidence"))
STATUSES = frozenset(("active", "superseded", "deleted"))


class SchemaError(ValueError):
    pass


class Core(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    status: str = Field(pattern=r"^(active|superseded|deleted)$")
    source: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


@dataclass(frozen=True)
class Record:
    entry: Entry
    start: int
    end: int
    status_start: int
    status_end: int
    core: Core
    context: tuple[FieldNode, ...]
    extensions: tuple[FieldNode, ...]
    migration_spans: dict[str, tuple[int, int]]


@dataclass(frozen=True)
class Document:
    raw_markdown: str
    records: tuple[Record, ...]


def _span_code(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("a Markdown metadata scalar must be nonempty and cannot contain NUL")
    if any(c in value for c in "\r\n") or value.startswith(_ESCAPED):
        value = _ESCAPED + quote(value, safe="")
    delim = "`" * (1 + max((len(run) for run in re.findall(r"`+", value)), default=0))
    if value.startswith((" ", "`")) or value.endswith((" ", "`")):
        value = f" {value} "
    return f"{delim}{value}{delim}"


def parse_document(raw: str, source_path: str, topic: str) -> Document:
    lines = re.findall(r"[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+$", raw)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def fail(line: int, message: str) -> None:
        raise SchemaError(f"{source_path}:{line + 1}: {message}")

    def span(node: SyntaxTreeNode) -> tuple[int, int]:
        assert node.map is not None
        return node.map

    def fields(nodes: list[SyntaxTreeNode]) -> tuple[tuple[FieldNode, int, int], ...]:
        result = []
        for node in nodes:
            start, end = span(node)
            if node.type != "bullet_list":
                fail(start, "expected a metadata field list")
            for item in node.children:
                first, last = span(item)
                children = item.children
                if not children or children[0].type != "paragraph":
                    fail(first, "invalid metadata field")
                inline = children[0].children[0]
                match = _FIELD.fullmatch(inline.content)
                if match is None:
                    fail(first, "invalid metadata field syntax")
                key, value = match.group("key", "value")
                lo = hi = offsets[first]
                if value.strip():
                    tokens = _MARKDOWN.parseInline(value)[0].children or []
                    if (
                        len(children) != 1
                        or len(tokens) != 1
                        or tokens[0].type != "code_inline"
                        or not tokens[0].content.strip()
                        or not value.startswith("`")
                        or not value.endswith("`")
                    ):
                        fail(first, f"invalid scalar for {key}")
                    value = tokens[0].content
                    if value.startswith(_ESCAPED):
                        value = unquote(value.removeprefix(_ESCAPED), errors="strict")
                    marker = _ITEM.match(lines[first])
                    if marker is None:
                        fail(first, "invalid metadata indentation")
                    lo = offsets[first] + marker.end() + match.start("value")
                    hi = offsets[first] + marker.end() + match.end("value")
                    result.append(
                        (
                            FieldNode(
                                key=key,
                                value=value,
                                raw_markdown=raw[offsets[first] : offsets[last]],
                            ),
                            lo,
                            hi,
                        )
                    )
                else:
                    if len(children) < 2:
                        fail(first, f"empty group for {key}")
                    result.append(
                        (
                            FieldNode(
                                key=key,
                                children=tuple(f for f, _, _ in fields(children[1:])),
                                raw_markdown=raw[offsets[first] : offsets[last]],
                            ),
                            lo,
                            hi,
                        )
                    )
        return tuple(result)

    records = []
    ids = set()
    blocks = SyntaxTreeNode(_MARKDOWN.parse(raw)).children
    title = next(
        (b.children[0].content.strip() for b in blocks if b.type == "heading" and b.tag == "h1"), ""
    )
    for block in blocks:
        if block.type != "bullet_list":
            continue
        for item in block.children:
            start, end = span(item)
            sections: dict[str, list[SyntaxTreeNode]] = {}
            current = None
            content_end = end
            for node in item.children:
                marker = (
                    _SECTIONS.get(node.children[0].content) if node.type == "paragraph" else None
                )
                if marker:
                    if marker in sections or list(_SECTIONS.values()).index(marker) <= (
                        list(_SECTIONS.values()).index(current) if current else -1
                    ):
                        fail(span(node)[0], f"duplicate or out of order section: {marker}")
                    if current is None:
                        content_end = span(node)[0]
                    current = marker
                    sections[current] = []
                elif current:
                    sections[current].append(node)
            if "core" not in sections:
                fail(start, "missing Core; legacy vaults require explicit v2 migration")
            core_fields = fields(sections["core"])
            core_data = {f.key: f.value for f, _, _ in core_fields}
            if core_data.keys() != CORE or len(core_fields) != len(CORE):
                fail(start, "Core must contain exactly id, status, source, and confidence")
            try:
                core = Core.model_validate(core_data)
            except ValueError as exc:
                # Avoid echoing the record itself in errors.
                fail(start, f"invalid Core ({', '.join(str(e['loc'][0]) for e in exc.errors())})")
            if core.id in ids:
                fail(start, "duplicate memory id")
            ids.add(core.id)
            _, status_start, status_end = next(row for row in core_fields if row[0].key == "status")
            context = tuple(f for f, _, _ in fields(sections.get("context", [])))
            extension_fields = fields(sections.get("extensions", []))
            extensions = tuple(f for f, _, _ in extension_fields)
            migration_spans = {}
            for node, lo, hi in extension_fields:
                if node.key not in {"syvrio.recorded_at", "migration.evidence_v2"}:
                    continue
                if node.value is None or node.key in migration_spans:
                    fail(start, f"expected one scalar for {node.key}")
                migration_spans[node.key] = (lo, hi)
            if any(
                name in sections and not values
                for name, values in (("context", context), ("extensions", extensions))
            ):
                fail(start, "omit empty optional sections")
            marker = _ITEM.match(lines[start])
            if marker is None:
                fail(start, "missing list item marker")
            indent = marker.end()
            text = (
                lines[start][indent:]
                + "".join(
                    line[indent:] if line.startswith(" " * indent) else line
                    for line in lines[start + 1 : content_end]
                )
            ).rstrip("\r\n")
            if not text.strip():
                fail(start, "empty Content")
            metadata = {"source": core.source, "confidence": core_data["confidence"]}
            for node in extensions:
                if node.key.startswith("syvrio.") and node.value is not None:
                    metadata.setdefault(node.key.removeprefix("syvrio."), node.value)
            record_raw = raw[offsets[start] : offsets[end]]
            entry = Entry(
                id=core.id,
                topic=topic,
                text=text,
                timestamp=metadata.pop("recorded_at", ""),
                status=core.status,
                supersedes=metadata.pop("supersedes", None),
                metadata=metadata,
                raw_markdown=record_raw,
                source_path=source_path,
                line=start + 1,
                end_line=end,
                context_fields=context,
                extension_fields=extensions,
                confidence=core.confidence,
                topic_title=title,
            )
            records.append(
                Record(
                    entry,
                    offsets[start],
                    offsets[end],
                    status_start,
                    status_end,
                    core,
                    context,
                    extensions,
                    migration_spans,
                )
            )
    return Document(raw, tuple(records))


def render_record(text: str, core: Core, context=(), extensions=(), newline: str = "\n") -> str:
    lines = text.splitlines()
    if not lines or not text.strip():
        raise ValueError("empty Content")
    result = [f"* {lines[0]}", *(f"  {line}" if line else "" for line in lines[1:])]
    for section, items in (
        (
            "Core",
            [
                ("id", core.id),
                ("status", core.status),
                ("source", core.source),
                (
                    "confidence",
                    f"{core.confidence:.2f}"
                    if round(core.confidence, 2) == core.confidence
                    else str(core.confidence),
                ),
            ],
        ),
        ("Context", context),
        ("Extensions", extensions),
    ):
        if items:
            result.extend(("", f"  **{section}**"))
            for key, value in items:
                if not re.fullmatch(_KEY, key):
                    raise ValueError(f"invalid metadata key: {key}")
                result.append(f"  * `{key}`: {_span_code(value)}")
    return newline.join(result) + newline
