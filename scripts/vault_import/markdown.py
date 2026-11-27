"""解析旧 Vault 的只读 Markdown 视图，迁移后的应用无需依赖旧格式。"""

import re
from urllib.parse import unquote

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

MARKDOWN = MarkdownIt("commonmark")


def field(item, lines):
    first, *rest = item.children
    m = re.fullmatch(r"`([a-z][a-z0-9_.]*)`:[ \t]*(.*)", first.children[0].content)
    if not m:
        raise ValueError(f"第 {item.map[0] + 1} 行字段无效或重复")
    key, raw = m.groups()
    if raw:
        tokens = MARKDOWN.parseInline(raw)[0].children
        if rest or len(tokens) != 1 or tokens[0].type != "code_inline":
            raise ValueError(f"字段 {key} 缺少行内代码定界符")
        value = tokens[0].content
        if value.startswith("~syvrio-escaped~"):
            value = unquote(value.removeprefix("~syvrio-escaped~"), errors="strict")
    elif len(rest) == 1 and rest[0].type == "fence" and key == "text":
        start, stop = rest[0].map
        pad = len(lines[start]) - len(lines[start].lstrip(" "))
        if not re.fullmatch(
            re.escape(rest[0].markup[0]) + "{%d,}" % len(rest[0].markup), lines[stop - 1].strip()
        ):
            raise ValueError("正文代码围栏未关闭")
        value = "".join(line.removeprefix(" " * pad) for line in lines[start + 1 : stop - 1])
        value = re.sub(r"(?:\r\n|\r|\n)\Z", "", value)
    elif rest:
        value = []
        for nested in rest:
            if nested.type != "bullet_list":
                raise ValueError(f"未知嵌套字段 {key}")
            for child in nested.children:
                name, member = field(child, lines)
                value.append({name: member})
        if all(set(v) == {"id"} for v in value):
            value = [v["id"] for v in value]
    else:
        raise ValueError(f"空字段 {key}")
    return key, value


def fields(nodes, lines):
    out = {}
    for node in nodes:
        if node.type != "bullet_list":
            raise ValueError("字段必须是 Markdown 列表")
        for item in node.children:
            key, value = field(item, lines)
            if key in out:
                raise ValueError(f"字段重复: {key}")
            out[key] = value
    return out


def document(raw):
    title = ""
    records = []
    lines = raw.splitlines(keepends=True)
    for node in SyntaxTreeNode(MARKDOWN.parse(raw)).children:
        if node.type == "heading":
            if not title and node.tag == "h1":
                title = node.children[0].content
            continue
        if node.type != "bullet_list":
            raise ValueError(f"第 {node.map[0] + 1} 行不是记录")
        for item in node.children:
            sections = {}
            children = None
            for child in item.children:
                if child.type == "paragraph":
                    heading = child.children[0].content
                    if (
                        heading not in ("**Core**", "**Context**", "**Extensions**")
                        or heading in sections
                    ):
                        raise ValueError(f"第 {child.map[0] + 1} 行存在未知或重复分组")
                    children = sections[heading] = []
                elif children is not None:
                    children.append(child)
                else:
                    raise ValueError("记录缺少 Core 分组")
            records.append({k.strip("*"): fields(v, lines) for k, v in sections.items()})
    return title, records
