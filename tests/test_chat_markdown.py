from __future__ import annotations

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.selection import SELECT_ALL
from textual.widgets import Static

from syvrio.agent.session import SessionStore
from syvrio.app.chat_theme import MARKDOWN_THEME
from syvrio.app.chat_widgets import ChatReply, _ReplyMarkdown

pytestmark = pytest.mark.ui

SAMPLE = """# 一级标题
这个项目是**"架构轻量但功能完整"**的设计思路，*italic* 和 `inline_code`。

## 二级标题
第一段中文 English，保留合理分段。

第二段用于检查段落间距。

### 三级标题
- 一级条目
  - 嵌套条目
  - 第二个嵌套条目
- 另一个条目

3. 第三项
4. 第四项

> 引用中文与 English 内容。

```python
def example():
    if True:
        return "value"\x20\x20
```

| 列名 | Value |
| --- | --- |
| 中文内容 | English value |

[Example](https://example.com)

---
"""


def render(source: str, width: int = 80, original: bool = False) -> Text:
    console = Console(width=width)
    with console.use_theme(MARKDOWN_THEME):
        return Text.assemble(
            *[
                (s.text, s.style)
                for s in console.render(Markdown(source) if original else _ReplyMarkdown(source))
                if not s.control
            ]
        )


@pytest.mark.parametrize(
    "source,target",
    [
        ('这个项目是**"架构轻量但功能完整"**的设计思路', '"架构轻量但功能完整"'),
        ("中文**“重点”**继续", "“重点”"),
        ("中文**（重点）**继续", "（重点）"),
        ("中文**bold**继续", "bold"),
        ("**普通加粗** and **bold**", "普通加粗"),
    ],
)
def test_strong_cjk_punctuation(source, target):
    text = render(source)
    assert "**" not in text.plain
    start = text.plain.index(target)
    assert any(span.start <= start < span.end and span.style.bold for span in text.spans)


def test_original_commonmark_limit_is_reproduced():
    source = '这个项目是**"架构轻量但功能完整"**的设计思路'
    assert "**" in render(source, original=True).plain
    assert _ReplyMarkdown(source).markup == source


@pytest.mark.parametrize(
    "source,literal",
    [
        ('`中文**"原样"**继续`', '中文**"原样"**继续'),
        ('```text\n中文**"原样"**继续\n```', '中文**"原样"**继续'),
        (r'中文\*\*"原样"\*\*继续', '中文**"原样"**继续'),
        ('中文**"未闭合', '中文**"未闭合'),
        ("`not finished", "`not finished"),
    ],
)
def test_code_escapes_and_unfinished_emphasis_preserved(source, literal):
    assert literal in render(source).plain


def test_inline_styles_and_links_survive_compatibility():
    text = render("中文**“重点”**继续 *italic* `code` ~~gone~~ [Example](https://example.com)")
    assert any(s.style.italic for s in text.spans)
    assert any(s.style.strike for s in text.spans)
    assert any(s.style.link == "https://example.com" for s in text.spans)
    assert "`" not in text.plain and "*" not in text.plain


def test_heading_style_and_compact_spacing():
    text = render("# Heading\n\nBody\n\n## Second\n\nMore")
    assert text.plain.splitlines()[0].startswith("Heading")
    start = text.plain.index("Heading")
    styles = [s.style for s in text.spans if s.start <= start < s.end]
    assert any(s.bold and s.color.get_truecolor() == (108, 182, 255) for s in styles)
    assert not any(s.underline for s in styles)
    assert text.plain.splitlines()[1].startswith("Body")
    assert len(render(SAMPLE).plain.splitlines()) < len(
        render(SAMPLE, original=True).plain.splitlines()
    )


@pytest.mark.parametrize("width", [80, 120, 160])
def test_all_blocks_and_long_lines_fit(width):
    source = SAMPLE + "\n```python\n" + "variable_name = " + repr("x" * 250) + "\n```"
    text = render(source, width)
    assert all(cell_len(line) <= width for line in text.plain.splitlines())
    for word in [
        "一级标题",
        "二级标题",
        "三级标题",
        "嵌套条目",
        "第三项",
        "第四项",
        "引用中文",
        "列名",
        "English value",
    ]:
        assert word in text.plain
    assert "3 " in text.plain and "4 " in text.plain
    assert "        return" in text.plain
    assert any(s.style.bgcolor is not None for s in text.spans)
    assert len({str(s.style.color) for s in text.spans if s.style.color}) > 3


class MarkdownApp(App):
    CSS = "ChatReply { height: auto; width: 1fr; }"

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield ChatReply(SAMPLE, id="reply")
            yield Static("用户中文 English", id="user", markup=False)


@pytest.mark.parametrize("width", [80, 120, 160])
async def test_stream_restore_resize_and_copy(width, tmp_path):
    app = MarkdownApp()
    async with app.run_test(size=(width, 50)) as pilot:
        reply = app.query_one(ChatReply)
        final = reply.get_selection(SELECT_ALL)[0]
        for partial in ['中文**"', "```python\n    x = 1", SAMPLE[:90], SAMPLE]:
            reply.update(partial)
            await pilot.pause()
            shown = reply.get_selection(SELECT_ALL)[0]
            assert all(cell_len(line) <= reply.content_size.width for line in shown.splitlines())
        assert reply.get_selection(SELECT_ALL)[0] == final
        visual = reply.visual
        reply.update(SAMPLE)
        assert reply.visual is visual  # identical final update skips reparsing
        store = SessionStore(tmp_path / "vault")
        session = store.create()
        store.append(session, {"type": "assistant", "content": SAMPLE})
        restored = ChatReply(store.load(session.id).records[-1]["content"])
        await app.query_one(VerticalScroll).mount(restored)
        await pilot.pause()
        final = reply.get_selection(SELECT_ALL)[0]
        assert restored.get_selection(SELECT_ALL)[0] == final
        assert store.load(session.id).records[-1]["content"] == SAMPLE
        reply.text_select_all()
        await pilot.press("ctrl+c")
        # Copy can flush the scrollbar layout after mounting the restored reply.
        # Compare with the current display width, not its pre-refresh snapshot.
        assert app.clipboard == reply.get_selection(SELECT_ALL)[0]
        await pilot.resize_terminal(80 if width != 80 else 120, 50)
        await pilot.pause()
        copied = reply.get_selection(SELECT_ALL)[0]
        assert "        return" in copied and "架构轻量但功能完整" in copied
        assert not reply.query("*")


async def test_mouse_selection_after_quoted_cjk_emphasis():
    app = MarkdownApp()
    async with app.run_test(size=(120, 50)) as pilot:
        reply = app.query_one(ChatReply)
        reply.update("中文**“重点”**继续")
        await pilot.pause()
        await pilot.mouse_down(reply, offset=(0, 0))
        await pilot.hover(reply, offset=(2, 0))
        await pilot.mouse_up(reply, offset=(2, 0))
        await pilot.pause()
        assert app.screen.get_selected_text() == "中文"
        await pilot.press("ctrl+c")
        assert app.clipboard == "中文"


def test_reference_link_context_is_preserved():
    text = render("中文**“重点”**继续 [Reference][ref]\n\n[ref]: https://example.com")
    assert any(s.style.link == "https://example.com" for s in text.spans)
    assert "[ref]" not in text.plain


@pytest.mark.parametrize("width", [47, 87, 127])
def test_wide_table_stays_inside_reply(width):
    columns = [f"Column{i}" for i in range(6)]
    table = "| " + " | ".join(columns) + " |\n"
    table += "| " + " | ".join(["---"] * 6) + " |\n"
    table += "| " + " | ".join(["long_value_" * 4] * 6) + " |"
    text = render(table, width)
    assert all(cell_len(line) <= width for line in text.plain.splitlines())
    assert "Column" in text.plain or "Col" in text.plain


async def test_long_reply_keeps_one_widget_and_all_text_on_resize():
    app = MarkdownApp()
    async with app.run_test(size=(120, 50)) as pilot:
        reply = app.query_one(ChatReply)
        source = SAMPLE * 4
        reply.update(source)
        for width in [80, 120, 160]:
            await pilot.resize_terminal(width, 50)
            await pilot.pause()
            text = reply.get_selection(SELECT_ALL)[0]
            assert text.count("架构轻量但功能完整") == 4
            assert text.count("        return") == 4
            assert not reply.query("*")
            assert reply.size.width <= width


def test_reply_markdown_has_readable_semantic_colors():
    text = render(
        "# Heading\n\nBody **important** and `code`.\n\n> Quoted text\n\n[Link](https://example.com)"
    )
    console = Console()

    def color(word):
        return text.get_style_at_offset(console, text.plain.index(word)).color.get_truecolor()

    assert color("Heading") == (108, 182, 255)
    assert color("Body") == (213, 213, 213)
    assert color("important") == (240, 240, 240)
    assert color("code") == (226, 192, 141)
    assert color("Quoted") == (154, 175, 196)
    assert color("Link") == (108, 182, 255)
