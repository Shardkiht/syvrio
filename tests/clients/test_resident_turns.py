"""常驻终端的输入、流式阅读、思考面板及 Git 确认交互。"""

from __future__ import annotations

import asyncio
import time

import pytest
from textual.geometry import Offset
from textual.widgets import Button, Input, Static

from syvrio.app.tui.input import BarInput
from syvrio.app.tui.resident import ResidentChatApp
from syvrio.app.tui.screens.git import GitConfirmScreen, GitScreen
from syvrio.app.tui.thinking import ThinkingPanel, ThinkingText

pytestmark = pytest.mark.ui


async def wait_for_ui(pilot, ready, timeout=30):
    deadline = time.monotonic() + timeout
    while not ready() and time.monotonic() < deadline:
        await pilot.pause(0.025)
    assert ready()


async def test_native_cursor_position_refreshed_on_resize(layout_app, monkeypatch):
    # 回归：布局变化（折叠侧栏/缩放）后 App 回放的是旧光标坐标，必须重算。
    app = layout_app
    writes: list[str] = []
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", BarInput)
        monkeypatch.setattr(app._driver, "write", writes.append)
        monkeypatch.setattr(BarInput, "_uses_native_cursor", property(lambda self: True))
        app.cursor_position = Offset(0, 0)
        widget._show_native_cursor()
        position = widget.cursor_screen_offset
        assert app.cursor_position == position
        assert writes[-2:] == [f"\x1b[{position.y + 1};{position.x + 1}H", "\x1b[5 q\x1b[?25h"]
        app.cursor_position = Offset(0, 0)  # 模拟布局变化后未刷新的旧坐标
        widget.on_resize()
        assert app.cursor_position == widget.cursor_screen_offset


@pytest.mark.parametrize(
    "value, position, cells",
    [
        ("", 0, 0),
        ("搜索下", 3, 6),
        ("搜索下 ", 4, 7),
        ("a你b", 3, 4),
        ("a你b", 1, 1),
        ("a你b", 2, 3),
    ],
)
async def test_input_caret_matches_insertion_cell(layout_app, value, position, cells):
    app = layout_app
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", BarInput)
        widget.value = value
        widget.cursor_position = position
        await pilot.pause()
        assert (
            widget.cursor_screen_offset.x
            == widget.content_region.x + cells - widget.scroll_offset.x
        )
        assert widget.cursor_screen_offset.y == widget.content_region.y
        assert app.cursor_position == widget.cursor_screen_offset
        assert widget.value == value


@pytest.mark.parametrize("chunk", ["ab cd", "中文混排a"])
async def test_native_input_caret_follows_horizontal_scroll(layout_app, monkeypatch, chunk):
    from rich.cells import cell_len

    monkeypatch.setattr(BarInput, "_uses_native_cursor", property(lambda self: True))
    app = layout_app
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", BarInput)
        value = chunk * 30 + "尾"

        def assert_caret():
            region = widget.content_region
            cells = cell_len(widget.value[: widget.cursor_position])
            assert app.cursor_position == Offset(
                region.x + cells - widget.scroll_offset.x, region.y
            )
            assert region.x <= app.cursor_position.x < region.right

        widget.insert_text_at_cursor(value)
        await pilot.pause()
        assert widget.value == value
        assert widget.scroll_offset.x > 0
        assert_caret()

        await pilot.press("home")
        assert widget.scroll_offset.x == 0
        assert_caret()

        await pilot.press("end", "left")
        assert widget.scroll_offset.x > 0
        assert_caret()

        await pilot.press("backspace")
        assert widget.value == value[:-2] + "尾"
        assert_caret()

        await pilot.press("end")
        await pilot.resize_terminal(62, 24)
        assert widget.scroll_offset.x > 0
        assert_caret()


@pytest.mark.parametrize("size", [(50, 24), (120, 40)])
async def test_home_has_full_project_wordmark(layout_app, size):
    from rich.cells import cell_len

    from syvrio.app.tui.widgets import HomePanel

    app = layout_app
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        logo = app.query_one(".welcome-wordmark", Static)
        for _ in range(30):
            if logo.region.height == (
                1 if app.query_one(HomePanel).size.width < HomePanel.wordmark_width() else 7
            ):
                break
            await pilot.pause(0.02)
        assert logo.region.height == (
            1 if app.query_one(HomePanel).size.width < HomePanel.wordmark_width() else 7
        )
        assert abs(logo.region.center[0] - app.query_one(HomePanel).region.center[0]) <= 1
        assert logo.region.bottom <= app.query_one("#input").region.y
        assert len(app.query_one(HomePanel).children) == 2
        assert not app.query("#home-overview, #home-recents, .home-prompt")
        wordmark = HomePanel.wordmark()
        lines = wordmark.plain.splitlines()
        assert len(lines) == 7
        assert all(cell_len(line) == HomePanel.wordmark_width() for line in lines)
        assert sum(line.count("·") for line in lines[4:]) >= 4
        assert "╲" in lines[4] and "╱" in lines[4]
        assert "╲╱" in lines[6]
        assert any(span.style == "#6CB6FF" for span in wordmark.spans)
        assert not app.query_one("#sidebar").display


async def test_mouse_drag_in_chat_keeps_input_caret_stable(layout_app):
    from textual.color import Color

    app = layout_app
    async with app.run_test(size=(100, 30)) as pilot:
        app.add_user_turn("Synthetic question")
        app.add_assistant_turn("Synthetic selectable reply", "12:00")
        await pilot.pause()
        await pilot.pause()
        widget = app.query_one("#input", BarInput)
        widget.value = "draft text"
        widget.cursor_position = 3
        assert app.query_one(".bubble").styles.border_left[1] == Color.parse("#6cb6ff")
        reply = app.query_one(".reply")
        await pilot.mouse_down(reply, offset=(1, 0))
        assert widget._cursor_suspended
        assert app.query_one("#chat").selecting
        await pilot.hover(reply, offset=(12, 0))
        await pilot.mouse_up(reply, offset=(12, 0))
        assert not app.query_one("#chat").selecting
        assert widget.value == "draft text" and widget.cursor_position == 3
        assert app.screen.selections
        await pilot.press("x")
        assert not widget._cursor_suspended


async def test_selected_stream_text_stays_copyable_until_input_clicked(layout_app):
    from textual.selection import SELECT_ALL

    app = layout_app
    async with app.run_test(size=(100, 24)) as pilot:
        app.add_user_turn("Synthetic question")
        app._show_event({"type": "content_delta", "text": "Original **selected** text"})
        await pilot.pause()
        await pilot.pause()

        reply = app.query_one(".reply")
        app.screen.selections = {reply: SELECT_ALL}
        before = reply.get_selection(SELECT_ALL)[0]
        for i in range(5):
            app._show_event({"type": "content_delta", "text": f" Changed reply {i}"})
            await pilot.pause()
            assert reply.get_selection(SELECT_ALL)[0] == before
        assert reply._pending_markdown
        await pilot.click("#input")
        await pilot.pause()
        assert not app.screen.selections
        assert reply._pending_markdown is None
        assert "Changed reply" in reply.get_selection(SELECT_ALL)[0]


async def test_dragging_scrollbar_freezes_stream_layout(layout_app):
    app = layout_app
    async with app.run_test(size=(100, 24)) as pilot:
        app.add_user_turn("Synthetic question")
        original = "\n\n".join(f"Paragraph {i}" for i in range(60))
        app.add_assistant_turn(original, "12:00")
        await pilot.pause()
        await pilot.pause()
        chat = app.query_one("#chat")
        reply = app.query_one(".reply")
        bar = chat.vertical_scrollbar
        await pilot.mouse_down(bar, offset=(0, bar.size.height - 2))
        try:
            assert chat.is_vertical_scrollbar_grabbed
            for i in range(5):
                reply.update(original + "\n\nNew output" * (i + 1))
                app._scroll_end()
                await pilot.pause()
                assert reply._markdown == original
        finally:
            await pilot.mouse_up(bar, offset=(0, bar.size.height - 2))
        assert not chat.is_vertical_scrollbar_grabbed


# ---------- Git 管理界面 ----------


async def test_git_confirmation_responsive_layout(layout_app):
    app = layout_app
    status = {
        "branch": "evolution[review]",
        "upstream": "origin/feature-memory-evolution",
        "modified": 3,
        "unpushed": 2,
    }
    async with app.run_test(size=(120, 36)) as pilot:
        app.push_screen(GitConfirmScreen(status, lambda value: None))
        await pilot.pause()
        assert "evolution[review]" in str(app.screen.query_one("#git-confirm").content)
        for width, height in ((120, 36), (100, 30), (80, 24), (60, 20)):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            card = app.screen.query_one("#git-card").region
            assert card.width <= width and card.height <= height
            assert app.screen.query_one("#git-confirm-actions").region.bottom <= card.bottom
            assert app.screen.query_one(".pick-hint").region.bottom <= card.bottom
            assert app.screen.query_one("#git-cancel", Button).has_focus


async def test_automatic_follow_ignores_stale_scroll_animation_target(layout_app, monkeypatch):
    app = layout_app
    async with app.run_test(size=(80, 24)):
        chat = app._chat()
        chat.follow_output = True
        original = chat.scroll_end

        def scroll_with_stale_target(**kwargs):
            chat.watch_scroll_y(2, 1)
            original(**kwargs)

        monkeypatch.setattr(chat, "scroll_end", scroll_with_stale_target)
        chat.scroll_to_follow()
        assert chat.follow_output
        chat.watch_scroll_y(2, 1)
        assert not chat.follow_output


async def test_thinking_focus_preserves_transcript_follow(layout_app, monkeypatch):
    from syvrio.app.tui.thinking import ThinkingPanel, ThinkingText

    app = layout_app
    async with app.run_test(size=(80, 24)) as pilot:
        chat = app._chat()
        panel = ThinkingPanel("Synthetic reasoning", active=True)
        await chat.mount(panel)
        await pilot.pause()
        area = panel.query_one(ThinkingText)
        centered = []
        monkeypatch.setattr(app.screen, "can_view_entire", lambda widget: widget is not area)
        monkeypatch.setattr(
            app.screen, "scroll_to_center", lambda widget, **kwargs: centered.append(widget)
        )
        chat.follow_output = True

        area.focus()
        await pilot.pause()

        assert area.has_focus
        assert chat.follow_output
        assert area not in centered


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_live_thinking_modes_and_independent_scroll(layout_app, size):
    app = layout_app
    async with app.run_test(size=size) as pilot:
        app.add_user_turn("Synthetic question")
        first = "".join(f"Thought {i}: 合成 detail\n" for i in range(80))
        app._show_event({"type": "reasoning_delta", "text": first})
        panel = app.query_one(ThinkingPanel)
        await wait_for_ui(pilot, lambda: bool(panel.query(ThinkingText)))
        area = panel.query_one(ThinkingText)
        await wait_for_ui(pilot, lambda: area.size.height == 3 and area.is_vertical_scroll_end)
        await pilot.click(panel.query_one(Button))
        assert area.size.height == min(10, size[1] // 3)
        area.focus()
        await pilot.press("pageup")
        await pilot.wait_for_scheduled_animations()
        assert not area.is_vertical_scroll_end
        previous = area.scroll_y
        app._show_event({"type": "reasoning_delta", "text": "Further thought\n" * 40})
        await wait_for_ui(pilot, lambda: area.text.endswith("Further thought\n"))
        assert area.scroll_y == previous
        full = area.text
        app._show_event(
            {"type": "reasoning_end", "text": full, "turn_id": 1, "phase": 0, "seconds": 1.5}
        )
        app._show_event({"type": "reasoning_delta", "text": "Second stage"})
        next_panel = list(app.query(ThinkingPanel))[-1]
        assert next_panel is not panel
        await wait_for_ui(pilot, lambda: bool(next_panel.query(ThinkingText)))
        app._show_event(
            {
                "type": "reasoning_end",
                "text": "Second stage",
                "turn_id": 1,
                "phase": 1,
                "seconds": 2,
            }
        )
        await pilot.pause()
        assert area.display
        assert not next_panel.query_one(ThinkingText).display
        await pilot.click(next_panel.query_one(Button))
        assert next_panel.query_one(ThinkingText).text == "Second stage"


async def test_wheel_pauses_stream_layout_until_bottom(layout_app, monkeypatch):
    from textual.events import MouseScrollUp

    app = layout_app
    async with app.run_test(size=(100, 24)) as pilot:
        app.add_user_turn("Synthetic question")
        original = "\n\n".join(f"Paragraph {i}" for i in range(50))
        app._show_event({"type": "content_delta", "text": original})
        chat = app._chat()
        reply = app.query_one(".reply")
        await wait_for_ui(pilot, lambda: chat.max_scroll_y > 0 and chat.is_vertical_scroll_end)
        field = app.query_one(BarInput)
        monkeypatch.setattr(app._driver, "write", lambda *_: None)
        monkeypatch.setattr(BarInput, "_uses_native_cursor", property(lambda self: True))
        field._show_native_cursor()
        chat.post_message(
            MouseScrollUp(
                chat, x=5, y=5, delta_x=0, delta_y=0, button=0, shift=False, meta=False, ctrl=False
            )
        )
        await pilot.wait_for_scheduled_animations()
        await wait_for_ui(pilot, lambda: not chat.follow_output)
        assert field._cursor_suspended and not field._native_cursor_active
        y, height = chat.scroll_y, reply.virtual_size.height
        for i in range(3):
            app._show_event({"type": "content_delta", "text": f"\n\nNew paragraph {i}"})
            await pilot.pause()
            assert reply._markdown == original
            assert (chat.scroll_y, reply.virtual_size.height) == (y, height)
        chat.scroll_end(animate=False, immediate=True)
        await wait_for_ui(
            pilot,
            lambda: reply._markdown.endswith("New paragraph 2") and chat.is_vertical_scroll_end,
        )
        assert reply.virtual_size.height > height
        assert reply._pending_markdown is None


async def test_git_requires_confirmation_and_completes_through_resident(resident_backend, tmp_path):
    from tests.infra.test_git_sync import add_remote, remote_log

    owner, client = resident_backend
    service = owner._service
    remote = add_remote(service, tmp_path)
    before = remote_log(remote)
    service.note("合成的终端同步记录", topic="misc")
    app = ResidentChatApp(client)
    async with app.run_test(size=(100, 30)) as pilot:
        field = app.query_one("#input", Input)
        await pilot.press("ctrl+s")
        assert field.has_focus and len(app.screen_stack) == 1
        field.value = "/git"
        await pilot.press("enter")
        await wait_for_ui(
            pilot,
            lambda: isinstance(app.screen, GitScreen) and bool(app.screen.query("#git-content")),
        )
        assert "Initialized" in str(app.screen.query_one("#git-content", Static).content)
        for cancel in ("enter", "escape"):
            await pilot.press("ctrl+s")
            assert isinstance(app.screen, GitConfirmScreen)
            assert app.screen.query_one("#git-cancel", Button).has_focus
            await pilot.press(cancel)
            assert isinstance(app.screen, GitScreen)
            assert remote_log(remote) == before
        await pilot.press("ctrl+s")
        await pilot.click("#git-synchronize")
        await wait_for_ui(
            pilot,
            lambda: (
                "Synchronization Completed"
                in str(app.screen.query_one("#git-content", Static).content)
            ),
            timeout=45,
        )
        content = str(app.screen.query_one("#git-content", Static).content)
        assert "Synchronization Completed" in content
        assert len(remote_log(remote)) == len(before) + 1


async def test_conversation_caps_wide_prose_and_wraps_cjk_after_resize(layout_app):
    from rich.cells import cell_len
    from textual.selection import SELECT_ALL

    from syvrio.app.tui.widgets import ChatReply

    content = "这是一段用于检查中文长句换行的合成回复。" * 10
    app = layout_app
    async with app.run_test(size=(160, 40)) as pilot:
        app.add_user_turn("Synthetic question")
        app.add_assistant_turn(content, "12:00", "Synthetic metadata")
        await pilot.pause()
        reply = app.query_one(ChatReply)
        header = app.query_one(".who-ai")
        meta = app.query_one(".meta")
        assert reply.region.width == app.query_one(".bubble").region.width == 100
        assert reply.region.x == header.region.x == meta.region.x
        assert reply.region.y == header.region.bottom + 1
        assert meta.region.y == reply.region.bottom + 1

        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        chat = app.query_one("#chat")
        assert 0 < reply.region.width < 100
        assert reply.region.right <= chat.scrollable_content_region.right
        rendered = reply.get_selection(SELECT_ALL)[0].splitlines()
        assert all(cell_len(line) <= reply.content_size.width for line in rendered)
        assert "".join(line.strip() for line in rendered) == content


@pytest.mark.parametrize("switch_sessions", [False, True], ids=["same-session", "switched-session"])
async def test_undo_preserves_edited_draft_until_the_field_is_empty(
    resident_backend, monkeypatch, switch_sessions
):
    owner, client = resident_backend
    session = owner._sessions.create()
    other = owner._sessions.create() if switch_sessions else None
    owner._sessions.append(
        session, {"id": "latest", "type": "message", "role": "user", "content": "Original message"}
    )
    app = ResidentChatApp(client, session.id)
    async with app.run_test() as pilot:
        received, release = asyncio.Event(), asyncio.Event()
        original = app._request

        async def delay_response(method, path, **kwargs):
            result = await original(method, path, **kwargs)
            if path == "/api/commands" and kwargs["json"]["line"] == "/undo":
                assert result["data"]["restored"] == "Original message"
                received.set()
                await release.wait()
            return result

        monkeypatch.setattr(app, "_request", delay_response)
        field = app.query_one("#input", Input)
        await pilot.click("#undo-turn")
        try:
            await asyncio.wait_for(received.wait(), 15)
            if other:
                await app._select(other.id)
            field.value = "New draft"
        finally:
            release.set()
        await app.workers.wait_for_complete()
        assert field.value == "New draft"
        if other:
            await app._select(session.id)
            await wait_for_ui(pilot, lambda: field.value == "Original message")
            await app._select(other.id)
            assert field.value == "New draft"
            return
        field.value = ""
        await wait_for_ui(pilot, lambda: field.value == "Original message")


async def test_git_sync_without_remote_shows_reason_and_preserves_commit(resident_backend):
    owner, client = resident_backend
    owner._service.note("没有配置远程仓库", topic="misc")
    app = ResidentChatApp(client)
    async with app.run_test() as pilot:
        await app._command("/git")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.click("#git-synchronize")
        content = app.screen.query_one("#git-content", Static)
        await wait_for_ui(pilot, lambda: "Partially Completed" in str(content.content), timeout=45)
        assert "Remote is not configured" in str(content.content)
        assert "Local commit has been preserved" in str(content.content)
        assert not owner._service.git_sync.running


async def test_closing_git_screen_does_not_cancel_sync(resident_backend, tmp_path, monkeypatch):
    from threading import Event

    from tests.infra.test_git_sync import add_remote, remote_log

    owner, client = resident_backend
    service = owner._service
    remote = add_remote(service, tmp_path)
    before = remote_log(remote)
    service.note("关闭界面后继续推送", topic="misc")
    entered, release = Event(), Event()
    original_push = service.repo.push

    def gated_push():
        entered.set()
        assert release.wait(30)
        return original_push()

    monkeypatch.setattr(service.repo, "push", gated_push)
    app = ResidentChatApp(client)
    async with app.run_test() as pilot:
        await app._command("/git")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.click("#git-synchronize")
        try:
            assert await asyncio.to_thread(entered.wait, 15)
            assert "Git Synchronization" in str(
                app.screen.query_one("#git-content", Static).content
            )
            await pilot.press("escape")
            assert len(app.screen_stack) == 1
        finally:
            release.set()
        await wait_for_ui(pilot, lambda: not app._git_sync_running, timeout=45)
        assert len(remote_log(remote)) == len(before) + 1


async def test_completing_stream_preserves_selected_reply(resident_backend):
    from threading import Event

    from textual.selection import SELECT_ALL

    from syvrio.app.tui.widgets import ChatReply
    from syvrio.infra.llm import AssistantTurn

    owner, client = resident_backend
    release = Event()

    class StreamingModel:
        def chat(self, messages, tools, on_delta=None):
            on_delta("Selectable start")
            assert release.wait(30)
            on_delta(" and")
            return AssistantTurn(content="Selectable start and ending")

    owner._runtime.model = StreamingModel()
    app = ResidentChatApp(client)
    async with app.run_test() as pilot:
        field = app.query_one("#input", Input)
        field.value = "Synthetic question"
        await pilot.press("enter")
        try:
            await wait_for_ui(
                pilot, lambda: any(reply.content_size.width > 20 for reply in app.query(ChatReply))
            )
            reply = app.query_one(ChatReply)
            app.screen.selections = {reply: SELECT_ALL}
            before = reply.get_selection(SELECT_ALL)[0]
            assert before.strip() == "Selectable start"
        finally:
            release.set()
        await asyncio.wait_for(app.workers.wait_for_complete(), 30)
        await pilot.pause()
        assert app.query_one(ChatReply) is reply
        assert reply in app.screen.selections
        assert reply.get_selection(SELECT_ALL)[0] == before
        assert await pilot.click("#input")
        assert reply not in app.screen.selections

        def final_reply_visible():
            return any(
                widget.get_selection(SELECT_ALL)[0].strip() == "Selectable start and ending"
                for widget in app.query(ChatReply)
                if widget.content_size.width
            )

        await wait_for_ui(pilot, final_reply_visible)
        # 释放选区后，下次轮询会用持久化历史重建控件。
        await app._poll(force=True)
        await wait_for_ui(pilot, final_reply_visible)
