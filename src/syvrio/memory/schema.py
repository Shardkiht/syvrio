"""Human-readable Vault records with byte-preserving edit spans."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, unquote

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..errors import SyvrioError
from ..time import parse_timestamp
from .models import Entry, FieldNode, MemoryContext

_MARKDOWN = MarkdownIt("commonmark")
_SECTIONS = ("Core", "Context", "Extensions")
_MARKERS = {f"**{name}**": name for name in _SECTIONS}
_KEY = r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*"
_FIELD = re.compile(rf"`(?P<key>{_KEY})`:[ \t]*(?P<value>[^\n]*)")
_ITEM = re.compile(r"^([ \t]*[-+*][ \t]+)")
_ESCAPED = "~syvrio-escaped~"
STATUSES = frozenset(("active", "deprecated"))


class SchemaError(SyvrioError, ValueError):
    pass


def validate_file_model(model, values: dict, filename: str, line: int):
    try:
        return model.model_validate(values)
    except ValidationError as exc:
        keys = ", ".join(
            ".".join(map(str, issue["loc"])) or "fields"
            for issue in exc.errors(include_input=False)
        )
        raise SchemaError(f"{filename}:{line}: invalid {model.__name__} ({keys}).") from None


class Core(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    status: Literal["active", "deprecated"]
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Memory text must contain readable content.")
        return value


def _span_code(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("A scalar must be nonempty and cannot contain NUL.")
    if any(c in value for c in "\r\n") or value.startswith(_ESCAPED):
        value = _ESCAPED + quote(value, safe="")
    delim = "`" * (1 + max((len(run) for run in re.findall(r"`+", value)), default=0))
    if value.startswith((" ", "`")) or value.endswith((" ", "`")):
        value = f" {value} "
    return f"{delim}{value}{delim}"


@dataclass
class Block:
    raw: str
    start: int
    end: int
    line: int
    end_line: int
    sections: dict[str, tuple[FieldNode, ...]]
    positions: dict[tuple[str, str], tuple[int, int]]
    headings: dict[str, int]

    def values(self, section: str) -> dict:
        values = {}
        for node in self.sections.get(section, ()):
            if node.key in values:
                raise SchemaError(f"Line {self.line}: duplicate {section} field {node.key}.")
            if node.value is not None:
                values[node.key] = node.value
            elif all(n.key == "id" and n.value is not None for n in node.children):
                values[node.key] = [n.value for n in node.children]
            else:
                raise SchemaError(f"Line {self.line}: invalid {section} field {node.key}.")
        return values

    def extensions(self, names: set[str]) -> dict[str, str]:
        selected = {}
        for node in self.sections.get("Extensions", ()):
            if node.key not in names:
                continue
            if node.key in selected or node.value is None:
                raise SchemaError(f"Line {self.line}: expected one value for {node.key}.")
            selected[node.key] = node.value
        return selected


def context_values(values: dict) -> dict:
    fields = dict(values)
    emotions = {
        key.removeprefix("emotion."): fields.pop(key)
        for key in values
        if key.startswith("emotion.")
    }
    if emotions:
        if "emotion" in fields:
            raise SchemaError("Conflicting emotion fields in Context.")
        fields["emotion"] = emotions
    return fields


def parse_blocks(raw: str, source_path: str) -> tuple[str, list[Block]]:
    lines = re.findall(r"[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+$", raw)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def fail(line: int, message: str):
        raise SchemaError(f"{source_path}:{line + 1}: {message}")

    def fields(nodes, section, positions, *, nested=False):
        result = []
        for node in nodes:
            if node.type != "bullet_list":
                fail(node.map[0], "expected a field list")
            for item in node.children:
                first, last = item.map
                children = item.children
                if not children or children[0].type != "paragraph":
                    fail(first, "invalid field")
                match = _FIELD.fullmatch(children[0].children[0].content)
                if not match:
                    fail(first, "invalid field syntax")
                key, value = match.group("key", "value")
                source = raw[offsets[first] : offsets[last]]
                if value:
                    tokens = _MARKDOWN.parseInline(value)[0].children or []
                    if (
                        len(children) != 1
                        or len(tokens) != 1
                        or tokens[0].type != "code_inline"
                        or not value.startswith("`")
                        or not value.endswith("`")
                    ):
                        fail(first, f"invalid scalar for {key}")
                    value = tokens[0].content
                    if value.startswith(_ESCAPED):
                        value = unquote(value.removeprefix(_ESCAPED), errors="strict")
                    marker = _ITEM.match(lines[first])
                    if marker is None or not value.strip() or "\x00" in value:
                        fail(first, f"invalid value for {key}")
                    if not nested:
                        base = offsets[first] + marker.end()
                        positions[section, key] = (
                            base + match.start("value"),
                            base + match.end("value"),
                        )
                    result.append(FieldNode(key=key, value=value, raw_markdown=source))
                elif len(children) == 2 and children[1].type == "fence":
                    fence = children[1]
                    start, end = fence.map
                    closing = lines[end - 1].strip()
                    if end <= start + 1 or not re.fullmatch(
                        re.escape(fence.markup[0]) + r"{" + str(len(fence.markup)) + r",}", closing
                    ):
                        fail(first, f"unclosed text block for {key}")
                    # Preserve author whitespace, CRLF and blank lines by reading
                    # physical source; the final separator belongs to the fence.
                    indent = len(lines[start]) - len(lines[start].lstrip(" "))
                    body = "".join(
                        line[indent:] if line.startswith(" " * indent) else line
                        for line in lines[start + 1 : end - 1]
                    )
                    value = re.sub(r"(?:\r\n|\r|\n)\Z", "", body)
                    if not value.strip() or "\x00" in value:
                        fail(first, f"empty text for {key}")
                    if not nested:
                        # Editing a scalar written as a fence replaces only its value.
                        left = offsets[first] + len(lines[first].rstrip("\r\n"))
                        right = offsets[end - 1] + len(lines[end - 1].rstrip("\r\n"))
                        positions[section, key] = (left, right)
                    result.append(FieldNode(key=key, value=value, raw_markdown=source))
                else:
                    if len(children) < 2:
                        fail(first, f"empty field {key}")
                    children_fields = fields(children[1:], section, positions, nested=True)
                    result.append(FieldNode(key=key, children=children_fields, raw_markdown=source))
        return tuple(result)

    blocks = []
    title = ""
    for node in SyntaxTreeNode(_MARKDOWN.parse(raw)).children:
        if node.type == "heading":
            if not title and node.tag == "h1":
                title = node.children[0].content.strip()
            continue
        if node.type != "bullet_list":
            fail(node.map[0], "expected a heading or a list of Core records")
        for item in node.children:
            first, last = item.map
            headings, sections, positions = {}, {}, {}
            current = None
            for child in item.children:
                marker = (
                    _MARKERS.get(child.children[0].content) if child.type == "paragraph" else None
                )
                if marker:
                    if current and _SECTIONS.index(marker) <= _SECTIONS.index(current):
                        fail(child.map[0], f"duplicate or out-of-order {marker} section")
                    if not current and marker != "Core":
                        fail(first, "expected Core")
                    headings[marker] = offsets[child.map[0]]
                    current = marker
                    sections[current] = []
                elif current:
                    sections[current].append(child)
                else:
                    fail(first, "expected a record beginning with Core")
            if "Core" not in sections:
                fail(first, "missing Core")
            parsed = {
                name: fields(children, name, positions) for name, children in sections.items()
            }
            if not all(parsed.values()):
                fail(first, "empty record section")
            start, end = offsets[first], offsets[last]
            blocks.append(
                Block(raw[start:end], start, end, first + 1, last, parsed, positions, headings)
            )
    return title, blocks


def _render_fields(items, indent: str, newline: str) -> str:
    result = ""
    for key, value in items:
        if not re.fullmatch(_KEY, key):
            raise ValueError(f"Invalid record key: {key}")
        prefix = f"{indent}* `{key}`:"
        if isinstance(value, list):
            if value:
                result += (
                    prefix
                    + newline
                    + _render_fields((("id", v) for v in value), indent + "  ", newline)
                )
        elif key == "text" and ("\n" in value or "\r" in value):
            fence = "~" * max(3, 1 + max((len(r) for r in re.findall("~+", value)), default=0))
            pad = indent + "  "
            # Preserve individual line endings independently of the document's style.
            body = re.sub(r"(\r\n|\r|\n)", r"\g<0>" + pad, value)
            result += (
                prefix
                + newline
                + pad
                + fence
                + newline
                + pad
                + body
                + newline
                + pad
                + fence
                + newline
            )
        else:
            result += f"{prefix} {_span_code(value)}{newline}"
    return result


def render_sections(core, context=(), extensions=(), newline: str = "\n") -> str:
    result = "* **Core**" + newline + _render_fields(core, "  ", newline)
    for name, items in (("Context", context), ("Extensions", extensions)):
        if items:
            result += newline + f"  **{name}**" + newline + _render_fields(items, "  ", newline)
    return result


def render_record(core: Core, context=(), extensions=(), newline: str = "\n") -> str:
    return render_sections(core.model_dump().items(), context, extensions, newline)


@dataclass
class Record:
    entry: Entry
    block: Block

    @property
    def start(self):
        return self.block.start

    @property
    def end(self):
        return self.block.end


@dataclass
class Document:
    raw_markdown: str
    records: tuple[Record, ...]


def parse_document(raw: str, source_path: str, topic: str) -> Document:
    title, blocks = parse_blocks(raw, source_path)
    records = []
    ids = set()
    for block in blocks:
        try:
            core = Core.model_validate(block.values("Core"))
            context = block.values("Context")
            MemoryContext.model_validate(context_values(context))
            extensions = block.extensions({"recorded_at", "session_id", "message_id", "supersedes"})
            stamp = extensions.pop("recorded_at")
            try:
                parse_timestamp(stamp)
            except ValueError as exc:
                raise ValueError("invalid recorded_at; a UTC offset is required") from exc
            if ("session_id" in extensions) != ("message_id" in extensions):
                raise ValueError("incomplete session source")
            if core.id in ids:
                raise ValueError("duplicate memory id")
            ids.add(core.id)
        except ValidationError as exc:
            bad = ", ".join(
                ".".join(map(str, e["loc"])) or "context" for e in exc.errors(include_input=False)
            )
            raise SchemaError(
                f"{source_path}:{block.line}: invalid memory record ({bad})."
            ) from None
        except (ValueError, KeyError) as exc:
            raise SchemaError(f"{source_path}:{block.line}: invalid memory record ({exc})") from exc
        supersedes = extensions.pop("supersedes", None)
        records.append(
            Record(
                Entry(
                    id=core.id,
                    topic=topic,
                    text=core.text,
                    timestamp=stamp,
                    status=core.status,
                    metadata=extensions,
                    supersedes=supersedes,
                    raw_markdown=block.raw,
                    source_path=source_path,
                    line=block.line,
                    end_line=block.end_line,
                    context_fields=block.sections.get("Context", ()),
                    extension_fields=block.sections.get("Extensions", ()),
                    topic_title=title,
                ),
                block,
            )
        )
    return Document(raw, tuple(records))


def append_record(raw: str, record: str, title: str, newline: str) -> str:
    if not raw:
        return f"# {' '.join(title.splitlines())}{newline}{newline}{record}"
    return raw + (newline if raw.endswith(("\r", "\n")) else newline * 2) + record


def replacements(raw: str, changes: list[tuple[int, int, str]]) -> str:
    for start, end, replacement in sorted(changes, reverse=True):
        raw = raw[:start] + replacement + raw[end:]
    return raw
