from __future__ import annotations

import asyncio
import json
import threading
from datetime import date

import pytest
from textual.widgets import Button, Input, Static

from syvrio.app.tui.input import BarInput
from syvrio.app.tui.resident import ResidentChatApp
from syvrio.app.tui.widgets import ModelPickerScreen, SidebarToggle
from syvrio.backend.client_contracts import SLASH_COMMANDS
from syvrio.infra.config import (
    GitConfig,
    SearchConfig,
)
from syvrio.infra.llm import AssistantTurn
from syvrio.infra.session import SessionStore, new_message_id
from syvrio.infra.vault.files import initialize_vault

pytestmark = pytest.mark.ui


class FakeChatModel:
    def __init__(self, turns):
        self.turns = list(turns)

    def chat(self, messages, tools, on_delta=None):
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if on_delta is not None and turn.content:
            half = max(1, len(turn.content) // 2)
            on_delta(turn.content[:half])
            on_delta(turn.content[half:])
        return turn


async def _finish_inference_worker(app, pilot):
    import asyncio

    await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
    await pilot.pause()


async def _open_resident_inferences(app, pilot):
    from syvrio.app.tui.screens.inferences import InferenceScreen

    app.query_one("#input", Input).value = "/inferences"
    await pilot.press("enter")
    await _finish_inference_worker(app, pilot)
    assert isinstance(app.screen, InferenceScreen)
    return app.screen


async def test_inference_screen_empty_state(resident_backend):
    from syvrio.app.tui.resident import ResidentChatApp

    _, client = resident_backend
    app = ResidentChatApp(client)
    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _open_resident_inferences(app, pilot)
        assert screen.query_one("#inference-empty").display
        assert not screen.query_one("#inference-body").display
        assert not screen.query_one("#inference-actions").display
        assert not screen.query_one("#inference-filters").display
        assert "No inferences yet" in str(screen.query_one("#inference-empty").render())
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, type(screen))


async def test_inference_screen_single_pending_confirm_refreshes_in_place(resident_backend):

    owner, client = resident_backend
    app = ResidentChatApp(client)
    source = owner._service.note("合成事实：喜欢安静活动")
    item = owner._service.propose_inference("可能喜欢安静活动", [source.id], kind="profile")
    async with app.run_test(size=(120, 36)) as pilot:
        screen = await _open_resident_inferences(app, pilot)
        assert screen._filter == "hypothesis"
        assert "Pending" in str(screen.query_one("#inference-detail-status").render())
        assert "confirmation never writes facts" in str(
            screen.query_one("#inference-detail-sources").render()
        )
        assert str(screen.query_one("#inference-detail-text").render()) == item.text
        assert "合成事实：喜欢安静活动" in str(
            screen.query_one("#inference-detail-sources").render()
        )
        assert not screen.query_one("#inference-confirm", Button).disabled

        await pilot.press("c")
        await _finish_inference_worker(app, pilot)
        assert app.screen is screen  # 原界面内刷新，不叠加弹窗
        assert not app.query(".card")  # 不再向聊天区插入详情卡片
        assert owner._service.list_inferences()[0].status == "confirmed"
        assert len(owner._service.store.entries()) == 1  # 确认不写入事实
        assert not app._submitting.locked()
        assert screen._filter == "hypothesis"
        assert screen._current() is None
        assert "Pending 0" in str(screen.query_one("#inference-filters").render())
        assert "Confirmed 1" in str(screen.query_one("#inference-filters").render())
        assert "Confirmed. 0 pending remaining." in str(
            screen.query_one("#inference-message").render()
        )


async def test_inference_screen_edit_labels_and_shortcut_letters_in_editor(resident_backend):
    owner, client = resident_backend
    app = ResidentChatApp(client)
    source = owner._service.note("合成事实")
    owner._service.propose_inference("可能喜欢安静活动", [source.id], kind="profile")
    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _open_resident_inferences(app, pilot)
        assert str(screen.query_one("#inference-correct", Button).label) == "Edit"
        assert (
            str(screen.query_one("#inference-hint").render())
            == "C Confirm · R Reject · E Edit · Esc Close"
        )
        await pilot.press("e")
        await pilot.pause()
        editor = screen.query_one("#inference-editor")
        editor.load_text("")
        await pilot.press("c", "r", "e")
        assert editor.text == "cre"
        assert screen._editing
        assert str(screen.query_one("#inference-hint").render()) == "Ctrl+S Save · Esc Cancel"


async def test_inference_sources_use_one_fact_scan(service, resident_backend, monkeypatch):
    from syvrio.app.tui.resident import ResidentChatApp

    _, client = resident_backend
    app = ResidentChatApp(client)
    sources = [service.note(f"合成来源 {index}") for index in range(3)]
    for index in range(6):
        service.propose_inference(f"合成推断 {index}", [sources[index % 3].id], kind="profile")
    calls = []
    entries = service.store.entries

    def counted_entries(*args, **kwargs):
        calls.append(kwargs)
        return entries(*args, **kwargs)

    monkeypatch.setattr(service.store, "entries", counted_entries)
    monkeypatch.setattr(service.store, "find", lambda *_: pytest.fail("来源不得逐个 find 全库扫描"))
    async with app.run_test(size=(100, 30)) as pilot:
        await app.workers.wait_for_complete()
        before = len(calls)
        screen = await _open_resident_inferences(app, pilot)
        assert len(calls) - before == 1
        assert calls[-1] == {"include_inactive": True}
        source_text = {entry.id: entry.text for entry in sources}
        selected = screen._current()
        assert source_text[selected.source_ids[0]] in str(
            screen.query_one("#inference-detail-sources").render()
        )


async def test_inference_edit_failure_preserves_draft_and_retries(
    service, resident_backend, monkeypatch
):
    from syvrio.app.tui.resident import ResidentChatApp

    _, client = resident_backend
    app = ResidentChatApp(client)
    source = service.note("合成事实")
    item = service.propose_inference("原始假设", [source.id], kind="profile")
    request = client.request

    def fail_save(method, path, **kwargs):
        if method == "POST" and path == "/api/commands":
            raise OSError("Synthetic save failure")
        return request(method, path, **kwargs)

    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _open_resident_inferences(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        revised = "修改后的完整草稿\nSecond line with C R E letters."
        editor = screen.query_one("#inference-editor")
        editor.load_text(revised)
        monkeypatch.setattr(client, "request", fail_save)
        await pilot.press("ctrl+s")
        await _finish_inference_worker(app, pilot)
        assert screen._editing and editor.display
        assert editor.text == revised
        assert editor.has_focus
        assert "Synthetic save failure" in str(screen.query_one("#inference-message").render())
        assert not screen.query_one("#inference-save", Button).disabled
        assert service.list_inferences() == [item]

        monkeypatch.setattr(client, "request", request)
        await pilot.press("ctrl+s")
        await _finish_inference_worker(app, pilot)
        assert not screen._editing
        assert screen._current().id != item.id
        assert screen._current().text == revised
        assert screen._filter == "confirmed"
        assert "Edit saved as a confirmed inference." in str(
            screen.query_one("#inference-message").render()
        )


async def test_inference_screen_filters_and_readonly_states(resident_backend):
    from syvrio.memory.models import MemoryDraft

    owner, client = resident_backend
    app = ResidentChatApp(client)
    source = owner._service.note("合成事实：偏好简洁工具")
    pending = owner._service.propose_inference("待审查假设", [source.id], kind="profile")
    confirmed = owner._service.propose_inference("已确认假设", [source.id], kind="profile")
    owner._service.review_inference(confirmed.id, "confirm")
    rejected = owner._service.propose_inference("已拒绝假设", [source.id], kind="profile")
    owner._service.review_inference(rejected.id, "reject")
    superseded_source = owner._service.note("将被更正的事实")
    retained = owner._service.propose_inference(
        "来源会被更正的假设", [superseded_source.id], kind="profile"
    )
    owner._service.correct(
        superseded_source.id, MemoryDraft(text="更正后的事实", topic="uncategorized")
    )

    async with app.run_test(size=(120, 36)) as pilot:
        screen = await _open_resident_inferences(app, pilot)
        filters = str(screen.query_one("#inference-filters").render())
        for text in ("All 4", "Pending 2", "Confirmed 1", "Deprecated 1"):
            assert text in filters
        from textual.widgets import OptionList

        listing = screen.query_one("#inference-list", OptionList)
        assert {option.id for option in listing.options} == {pending.id, retained.id}

        await pilot.press("right")  # 从待确认切换到已确认。
        await pilot.pause()
        assert screen._filter == "confirmed"
        assert screen._current().id == confirmed.id
        assert screen.query_one("#inference-confirm", Button).disabled
        assert not screen.query_one("#inference-reject", Button).disabled

        await pilot.press("right")  # 从已确认切换到已弃用。
        await pilot.pause()
        assert screen._current().id == rejected.id
        assert all(button.disabled for button in screen.query("#inference-actions Button"))
        assert "Read-only record" in str(screen.query_one("#inference-detail-sources").render())

        await pilot.press("right")  # 从已弃用切换到全部。
        await pilot.pause()
        assert screen._filter == "all"
        assert len(listing.options) == 4
        assert pending.id in {option.id for option in listing.options}


async def test_inference_screen_long_text_and_long_source_ids(resident_backend):
    from syvrio.app.tui.screens.inferences import InferenceScreen
    from syvrio.memory.models import Inference

    owner, client = resident_backend
    app = ResidentChatApp(client)
    long_fact = "合成事实 [/x]：" + "长期项目偏好先验证最小闭环。" * 20
    source = owner._service.note(long_fact)
    long_text = (
        "[/x] 可能在做长期项目时偏好先搭建可验证的最小闭环，再逐步扩展功能；面对不确定需求时倾向先确认边界。"
        * 4
    )
    owner._service.propose_inference(long_text, [source.id], kind="profile")
    async with app.run_test(size=(120, 36)) as pilot:
        screen = await _open_resident_inferences(app, pilot)
        assert str(screen.query_one("#inference-detail-text").render()) == long_text
        sources = str(screen.query_one("#inference-detail-sources").render())
        assert "Sources (1)" in sources
        assert "[/x]" in sources
        assert len(sources) < len(long_fact) + 40  # 来源只给摘录，不整段搬运
        assert (
            screen.query_one("#inference-detail-body").region.height
            <= screen.query_one("#inference-detail").region.height
        )
        assert (
            screen.query_one("#inference-actions").region.bottom
            <= screen.query_one("#inference-card").region.bottom
        )

        # 长来源 ID 由组件级数据构造：业务接口不允许伪造来源，这里只验证展示规则。
        stamp = "2026-11-05T18:00:00+08:00"
        item = Inference(
            id="inf-synthetic",
            text="可能偏好简洁界面",
            kind="profile",
            source_ids=["mem-20990101-000000-abcdef123456"],
            created_at=stamp,
            updated_at=stamp,
        )
        app.push_screen(InferenceScreen([item], {}, lambda *args: None))
        await pilot.pause()
        detail = app.screen
        assert isinstance(detail, InferenceScreen)
        sources = str(detail.query_one("#inference-detail-sources").render())
        assert "mem-20990101-000000-abcdef123456" not in sources
        assert "mem-209901…23456" in sources
        assert "Source unavailable" in sources


async def test_inference_screen_reject_and_correct_full_text(service, resident_backend):
    from syvrio.app.tui.resident import ResidentChatApp

    _, client = resident_backend
    app = ResidentChatApp(client)
    source = service.note("合成事实：先确认边界")
    item = service.propose_inference("可能偏好先确认需求边界", [source.id], kind="profile")
    async with app.run_test(size=(120, 36)) as pilot:
        screen = await _open_resident_inferences(app, pilot)
        await pilot.press("r")
        await _finish_inference_worker(app, pilot)
        assert service.list_inferences()[0].status == "deprecated"

        screen._set_filter("all")
        await pilot.pause()
        from textual.widgets import OptionList

        listing = screen.query_one("#inference-list", OptionList)
        listing.highlighted = next(
            index for index, option in enumerate(listing.options) if option.id == item.id
        )
        await pilot.pause()
        # 已拒绝不可再操作。
        assert all(button.disabled for button in screen.query("#inference-actions Button"))

        editable = service.propose_inference("可能偏好分阶段交付", [source.id], kind="profile")
        await pilot.press("escape")
        screen = await _open_resident_inferences(app, pilot)
        assert screen._current().id == editable.id
        await pilot.press("e")
        await pilot.pause()
        editor = screen.query_one("#inference-editor")
        assert editor.display and editor.text == editable.text
        revised = (
            editable.text + "；先给最小验证版本，再补充细节。\n第二行保留完整原文，不静默截断。"
        )
        editor.load_text(revised)
        await pilot.press("ctrl+s")
        await _finish_inference_worker(app, pilot)
        records = {record.id: record for record in service.list_inferences()}
        assert records[editable.id].status == "deprecated"
        assert records[editable.id].text == editable.text
        current = screen._current()
        assert current is not None and current.id != editable.id
        assert current.status == "confirmed"
        assert current.source_ids == editable.source_ids
        assert current.text == revised  # 完整多行原文保留
        assert str(screen.query_one("#inference-detail-text").render()) == revised
        assert not screen._editing


async def test_inference_screen_correct_escape_cancels_then_closes(resident_backend):
    from syvrio.app.tui.screens.inferences import InferenceScreen

    owner, client = resident_backend
    app = ResidentChatApp(client)
    source = owner._service.note("合成事实")
    item = owner._service.propose_inference("可能喜欢安静活动", [source.id], kind="profile")
    async with app.run_test(size=(120, 36)) as pilot:
        screen = await _open_resident_inferences(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        assert screen._editing
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, InferenceScreen)  # 第一次 Esc 只退出编辑
        assert not screen._editing
        assert not screen.query_one("#inference-editor").display
        assert owner._service.list_inferences()[0].text == item.text
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, InferenceScreen)
        assert app.query_one("#input", Input).has_focus


async def test_inference_screen_action_failure_keeps_screen_usable(resident_backend, monkeypatch):
    from syvrio.app.tui.screens.inferences import InferenceScreen

    owner, client = resident_backend
    app = ResidentChatApp(client)
    source = owner._service.note("合成事实")
    owner._service.propose_inference("可能喜欢安静活动", [source.id], kind="profile")

    def fail(*args, **kwargs):
        raise ValueError("合成推断写入失败")

    monkeypatch.setattr(owner._service, "review_inference", fail)
    async with app.run_test(size=(120, 36)) as pilot:
        screen = await _open_resident_inferences(app, pilot)
        await pilot.press("c")
        await _finish_inference_worker(app, pilot)
        assert isinstance(app.screen, InferenceScreen)
        assert not app._submitting.locked() and not screen._busy
        assert "Command failed" in str(screen.query_one("#inference-message").render())
        assert screen.query_one("#inference-message").has_class("error")
        assert not screen.query_one("#inference-confirm", Button).disabled
        assert not app.query_one("#input", Input).disabled


async def test_inference_screen_list_and_detail_scroll(resident_backend):
    owner, client = resident_backend
    app = ResidentChatApp(client)
    source = owner._service.note("合成事实")
    for index in range(12):
        owner._service.propose_inference(
            f"第{index}条待审查推断。" + "补充说明" * 80, [source.id], kind="profile"
        )
    async with app.run_test(size=(100, 26)) as pilot:
        from textual.widgets import OptionList

        screen = await _open_resident_inferences(app, pilot)
        listing = screen.query_one("#inference-list", OptionList)
        assert listing.option_count == 12
        listing.highlighted = 11
        await pilot.pause()
        listing.scroll_end(animate=False)
        await pilot.pause()
        assert screen._current().id == listing.options[11].id
        assert listing.scroll_y > 0  # 列表内部滚动，不撑破卡片
        detail = screen.query_one("#inference-detail-body")
        detail.scroll_end(animate=False)
        await pilot.pause()
        assert detail.scroll_y > 0  # 长详情独立滚动


@pytest.mark.parametrize(
    ("size", "layout"),
    [
        ((120, 36), "horizontal"),
        ((100, 30), "horizontal"),
        ((80, 24), "vertical"),
        ((60, 20), "vertical"),
    ],
)
async def test_inference_screen_reopen_and_responsive_layout(resident_backend, size, layout):
    from syvrio.app.tui.screens.inferences import InferenceScreen

    owner, client = resident_backend
    app = ResidentChatApp(client)
    source = owner._service.note("合成事实")
    owner._service.propose_inference("可能喜欢安静活动", [source.id], kind="profile")
    async with app.run_test(size=size) as pilot:
        screen = await _open_resident_inferences(app, pilot)
        assert screen.query_one("#inference-body").styles.layout.name == layout
        card = screen.query_one("#inference-card").region
        assert screen.query_one("#inference-actions").region.bottom <= card.bottom
        assert screen.query_one("#inference-hint").region.bottom <= card.bottom
        listing = screen.query_one("#inference-list").region
        detail = screen.query_one("#inference-detail").region
        if layout == "horizontal":
            assert listing.right <= detail.x  # 两列不重叠
        else:
            assert listing.bottom <= detail.y  # 上下布局不重叠
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, InferenceScreen)
        # 连续再次打开仍可用。
        screen = await _open_resident_inferences(app, pilot)
        assert isinstance(app.screen, InferenceScreen)
        await pilot.press("escape")
        await pilot.pause()


async def test_inference_sync_updates_options_in_place(resident_backend):
    from textual.widgets import OptionList

    owner, client = resident_backend
    app = ResidentChatApp(client)
    source = owner._service.note("合成事实")
    for index in range(5):
        owner._service.propose_inference(f"合成假设 {index}", [source.id], kind="profile")
    entries = owner._service.list_inferences()
    sources = {source.id: source.text}
    async with app.run_test(size=(120, 36)) as pilot:
        screen = await _open_resident_inferences(app, pilot)
        screen._set_filter("all")
        await pilot.pause()
        listing = screen.query_one("#inference-list", OptionList)
        before = list(listing.options)
        assert [option.id for option in before] == [item.id for item in entries]

        # 状态变化只替换对应提示文本，其余选项对象原样复用，不整表重建。
        changed = [entries[0].model_copy(update={"status": "confirmed"}), *entries[1:]]
        screen.set_items(changed, sources)
        assert "Confirmed" in listing.options[0].prompt.text.plain
        assert all(listing.options[index] is before[index] for index in range(1, 5))

        # 移除一条时只删除该选项，其他选项对象和选中高亮保持稳定。
        remaining = [entries[0], *entries[2:]]
        screen.set_items(remaining, sources, focus_id=entries[3].id)
        assert [option.id for option in listing.options] == [item.id for item in remaining]
        assert listing.options[1] is before[2]
        assert listing.highlighted_option.id == entries[3].id


async def test_dream_background_continues_after_switching_sessions(
    resident_backend, service, monkeypatch
):
    from syvrio.memory.background import dream

    owner, client = resident_backend
    service.config.chat.default_budget = 1
    service.note("Synthetic source", topic="preferences")
    other = owner._sessions.create()
    started, release = threading.Event(), threading.Event()

    def generate(_, prompt, data, schema):
        started.set()
        assert release.wait(8)
        return schema(summary="Synthetic summary")

    monkeypatch.setattr(dream, "ask", generate)
    app = ResidentChatApp(client)
    async with app.run_test() as pilot:
        try:
            await app._command("/dream run", confirmed=True)
            assert await asyncio.to_thread(started.wait, 15)
            assert not app.query_one("#input", Input).disabled
            await app._select(other.id)
            assert app.sid == other.id
            assert owner._background.dream_status()["state"] == "running"
        finally:
            release.set()
        async with asyncio.timeout(8):
            while owner._background.dream_status()["state"] != "paused":
                await pilot.pause(0.05)
    assert service.profile.read_text().strip() == "Synthetic summary"
    assert not service.list_inferences()


async def test_profile_view_does_not_invoke_model(resident_backend, service, monkeypatch):
    from syvrio.app.tui.widgets import ProfileScreen
    from syvrio.memory.background import dream

    owner, client = resident_backend
    service.note("Synthetic preference", topic="preferences")
    monkeypatch.setattr(
        dream, "ask", lambda _, prompt, data, schema: schema(summary="Published profile")
    )
    # 在画像发布后、开始生成推断前停止。
    service.config.chat.default_budget = 1
    owner._background.request_dream()
    async with asyncio.timeout(8):
        while owner._background.dream_status()["state"] != "paused":
            await asyncio.sleep(0.02)
    monkeypatch.setattr(dream, "ask", lambda *_: pytest.fail("Viewing a profile called a model"))
    monkeypatch.setattr(
        service, "current_facts", lambda: pytest.fail("Viewing a profile scanned facts")
    )
    app = ResidentChatApp(client)
    async with app.run_test() as pilot:
        await app._command("/profile")
        await pilot.pause()
        assert isinstance(app.screen, ProfileScreen)
        assert str(app.screen.query_one("#profile-text", Static).content) == "Published profile"
        assert str(app.screen.query_one("#profile-title").render()) == "User Profile"
        assert str(app.screen.query_one("#profile-subtitle").render()) == "Personal memory overview"
        assert "Last generated" in str(app.screen.query_one("#profile-status").render())
        metadata = str(app.screen.query_one("#profile-meta").render())
        assert "Generated " in metadata and "Facts 1" in metadata and "Unique 1" in metadata
        hint = str(app.screen.query_one("#profile-hint").render())
        assert hint == "↑↓ Scroll · PgUp/PgDn Page · Esc Close"
        await pilot.press("escape")
        assert not isinstance(app.screen, ProfileScreen)
        assert app.query_one("#input", Input).has_focus


async def test_profile_view_shows_empty_state_without_cache(resident_backend):
    from syvrio.app.tui.widgets import ProfileScreen

    _, client = resident_backend
    app = ResidentChatApp(client)
    async with app.run_test() as pilot:
        await app._command("/profile")
        await pilot.pause()
        assert isinstance(app.screen, ProfileScreen)
        assert "Run /dream" in str(app.screen.query_one("#profile-text", Static).content)
        assert str(app.screen.query_one("#profile-status").render()) == "Not generated"
        assert not app.screen.query_one("#profile-meta").display
        await pilot.press("escape")
        assert app.query_one("#input", Input).has_focus


async def test_profile_view_keeps_published_summary_until_dream_runs(resident_backend, service):
    from syvrio.app.tui.widgets import ProfileScreen

    _, client = resident_backend
    service.profile.path.write_text("Previous profile", encoding="utf-8")
    service.note("Subsequent fact", topic="preferences")
    app = ResidentChatApp(client)
    async with app.run_test() as pilot:
        await app._command("/profile")
        await pilot.pause()
        assert isinstance(app.screen, ProfileScreen)
        assert str(app.screen.query_one("#profile-text", Static).content) == "Previous profile"
        assert str(app.screen.query_one("#profile-status", Static).content) == "Generated profile"
        assert not app.screen.query_one("#profile-meta").display


async def test_profile_view_reports_read_failure(resident_backend, service, monkeypatch):
    _, client = resident_backend
    service.profile.path.write_bytes(b"\xff")
    app = ResidentChatApp(client)
    notifications = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append(message))
    async with app.run_test() as pilot:
        await app._command("/profile")
        await pilot.pause()
        assert notifications == ["Could not read the published profile."]
        assert app.query_one("#input", Input).has_focus
        assert service.profile.path.read_bytes() == b"\xff"


async def test_profile_long_mixed_text_scrolls_and_resizes(resident_backend, service):
    from syvrio.app.tui.widgets import ProfileScreen

    _, client = resident_backend
    summary_text = "\n\n".join(
        f"Section {n} · 长段落用于验证滚动后正文完整、标题和底部提示保持可见。" * 3
        for n in range(40)
    )
    assert len(summary_text) > 2000
    service.profile.path.write_text(summary_text, encoding="utf-8")
    app = ResidentChatApp(client)
    async with app.run_test(size=(120, 36)) as pilot:
        await app._command("/profile")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ProfileScreen)
        assert str(screen.query_one("#profile-text").content) == summary_text
        body = screen.query_one("#profile-body")
        for width, height in ((120, 36), (100, 30), (80, 24), (60, 20)):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            card = screen.query_one("#profile-card").region
            assert card.width <= width and card.height <= height
            assert card.x >= 0 and card.y >= 0 and card.bottom <= height
            assert screen.query_one("#profile-title").region.bottom <= body.region.y
            hint = screen.query_one("#profile-hint").region
            assert body.region.bottom <= hint.y and hint.bottom <= card.bottom
            body.scroll_end(animate=False)
            await pilot.pause()
            assert body.scroll_y > 0
            assert screen.query_one("#profile-hint").region == hint
            await pilot.press("pageup")
        await pilot.press("escape")
        assert app.query_one("#input", Input).has_focus
        await app._command("/profile")
        await pilot.pause()
        assert str(app.screen.query_one("#profile-text").content) == summary_text


async def test_dream_publishes_profile_in_background(resident_backend, service, monkeypatch):
    from syvrio.memory.background import dream

    owner, client = resident_backend
    service.config.chat.default_budget = 1
    service.note("Synthetic preference", topic="preferences")
    monkeypatch.setattr(
        dream, "ask", lambda _, prompt, data, schema: schema(summary="Updated profile")
    )
    app = ResidentChatApp(client)
    async with app.run_test() as pilot:
        await app._command("/dream", confirmed=True)
        async with asyncio.timeout(8):
            while not service.profile.path.exists():
                await pilot.pause(0.05)
        assert not app.query_one("#input", Input).disabled
        assert service.profile.read_text().strip() == "Updated profile"
        cards = [str(card.content) for card in app.query(".card")]
        assert any("Dream submitted" in text for text in cards)
        assert owner._background.dream_status()["phase"] == "hypotheses"


async def test_profile_remains_readable_while_dream_runs(resident_backend, service, monkeypatch):
    from syvrio.memory.background import dream

    _, client = resident_backend
    service.config.chat.default_budget = 1
    service.note("Synthetic preference", topic="preferences")
    service.profile.path.write_text("Current profile", encoding="utf-8")
    started, release = threading.Event(), threading.Event()

    def slow_summary(_, prompt, data, schema):
        started.set()
        assert release.wait(8)
        return schema(summary="Next profile")

    monkeypatch.setattr(dream, "ask", slow_summary)
    app = ResidentChatApp(client)
    async with app.run_test() as pilot:
        try:
            await app._command("/dream run", confirmed=True)
            assert await asyncio.to_thread(started.wait, 5)
            assert not app.query_one("#input", Input).disabled
            await app._command("/profile")
            await pilot.pause()
            assert str(app.screen.query_one("#profile-text", Static).content) == "Current profile"
            await pilot.press("escape")
            assert app.query_one("#input", Input).has_focus
        finally:
            release.set()


async def test_resume_session_does_not_show_success_notification(resident_backend, monkeypatch):
    from textual.selection import SELECT_ALL

    from syvrio.app.tui.resident import ResidentChatApp

    owner, client = resident_backend
    session = owner._sessions.create()
    owner._sessions.append(session, {"type": "assistant", "content": "A previous reply"})
    app = ResidentChatApp(client)
    async with app.run_test() as pilot:
        notices = []
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))
        await app._select(session.id)
        await pilot.pause()
        assert app.sid == session.id
        assert "A previous reply" in app.query_one(".reply").get_selection(SELECT_ALL)[0]
        assert not notices and not app.query_one("#input", Input).disabled


async def test_command_palette_filter_navigate_execute(resident_backend):
    from syvrio.app.tui.widgets import CommandPalette

    owner, client = resident_backend
    app = ResidentChatApp(client)
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/th"  # 触发 Changed -> 面板打开
        await pilot.pause()

        palette = app.query_one("#cmd-palette", CommandPalette)
        assert palette.display
        assert [m[0] for m in palette.matches] == ["think"]
        assert palette.current is None
        await pilot.press("down")
        assert palette.current == "think"

        await pilot.press("enter")
        assert widget.value == "/think"
        await pilot.press("enter")
        await pilot.pause()
        assert not palette.display
        # /think 要么弹出选择器，要么提示模型不支持；两者都说明命令已执行。
        assert app.query(".card") or app.screen.__class__.__name__ in {
            "PickScreen",
            "TextInputScreen",
        }
        assert widget.value == ""


async def test_command_palette_escape_closes(layout_app):
    from syvrio.app.tui.widgets import CommandPalette

    app = layout_app
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/"
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        assert palette.display and len(palette.matches) == len(SLASH_COMMANDS)

        await pilot.press("escape")
        await pilot.pause()
        assert not palette.display


async def test_resident_enter_preserves_unselected_input_and_extra_slashes(
    resident_backend, monkeypatch
):
    from syvrio.app.tui.widgets import CommandPalette, PickScreen

    _, client = resident_backend
    app = ResidentChatApp(client)
    calls = []
    notices = []
    request = client.request
    notify = app.notify

    def trace_request(method, path, **values):
        if method == "POST" and path == "/api/commands":
            calls.append(values["json"]["line"])
        return request(method, path, **values)

    def trace_notice(message, **kwargs):
        notices.append(message)
        return notify(message, **kwargs)

    monkeypatch.setattr(client, "request", trace_request)
    monkeypatch.setattr(app, "notify", trace_notice)
    async with app.run_test() as pilot:
        widget = app.query_one("#input", Input)
        palette = app.query_one(CommandPalette)
        screen = app.screen
        for line in ("/", "/exi", "/dream r"):
            widget.value = line
            await pilot.pause()
            assert palette.current is None
            await pilot.press("enter")
            assert not calls
            assert notices[-1] == "Complete the command before sending."
            assert widget.value == line and app.screen is screen
        await pilot.press("escape", "enter")
        assert not calls and widget.value == "/dream r"
        assert notices[-1] == "Complete the command before sending."

        for line in ("//rename", "//inferences", "/import extra", "/exit extra"):
            widget.value = line
            await pilot.pause()
            assert palette.current is None
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert calls[-1] == line
            assert notices[-1] == "Invalid command."
            assert app.screen is screen

        widget.value = "/dream st"
        await pilot.pause()
        before = len(calls)
        await pilot.press("down", "enter")
        assert len(calls) == before and widget.value == "/dream status"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert calls[-1] == "/dream status"
        assert isinstance(app.screen, PickScreen)
        await pilot.press("escape")

        widget.value = "/dream "
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert calls[-1] == "/dream"
        assert isinstance(app.screen, PickScreen)
        await pilot.press("escape")

        widget.value = "/sta"
        widget.cursor_position = len(widget.value)
        await pilot.pause()
        before = len(calls)
        await pilot.press("tab")
        assert widget.value == "/status" and len(calls) == before
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert calls[-1] == "/status"


async def test_tab_completes_highlighted_command_without_executing(layout_app, monkeypatch):
    app = layout_app
    commands = []
    monkeypatch.setattr(app, "_submit_command", commands.append)
    async with app.run_test() as pilot:
        widget = app.query_one("#input", Input)
        await pilot.press("slash", "down", "down", "tab")
        assert widget.value == "/sessions"
        assert widget.cursor_position == len(widget.value)
        assert not commands
        await pilot.press("enter")
        assert commands == ["/sessions"]


@pytest.mark.parametrize(
    ("partial", "completed"),
    [
        ("/dream r", "/dream run"),
        ("/model re", "/model refresh"),
        ("/dream st", "/dream status"),
        ("/rename my notes", "/rename my notes"),
        ("ordinary text", "ordinary text"),
    ],
)
async def test_tab_completes_subcommands_and_preserves_arguments(layout_app, partial, completed):
    app = layout_app
    async with app.run_test() as pilot:
        widget = app.query_one("#input", Input)
        widget.value = partial
        widget.cursor_position = len(partial)
        await pilot.press("tab")
        assert widget.value == completed
        assert widget.has_focus


async def test_tab_completes_command_then_subcommand(layout_app, monkeypatch):
    app = layout_app
    commands = []
    monkeypatch.setattr(app, "_submit_command", commands.append)
    async with app.run_test() as pilot:
        widget = app.query_one("#input", Input)
        await pilot.press("slash", "m", "o", "d", "tab")
        assert widget.value == "/model "
        await pilot.press("tab")
        assert widget.value == "/model refresh"
        assert not commands
        await pilot.press("enter")
        assert commands == ["/model refresh"]


@pytest.mark.parametrize("prefix", ["/", "/rename"])
async def test_command_palette_sits_above_input(layout_app, prefix):
    from syvrio.app.tui.widgets import CommandPalette

    app = layout_app
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = prefix
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        assert palette.display
        assert palette.region.bottom <= widget.region.y, (
            f"palette={palette.region} input={widget.region}"
        )
        assert palette.region.x == widget.region.x, (
            f"palette={palette.region} input={widget.region}"
        )


async def test_command_palette_columns_align(layout_app):
    from syvrio.app.tui.widgets import CommandPalette, CommandRow

    app = layout_app
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#input", Input).value = "/"
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        rows = list(palette.query(CommandRow))
        assert len(rows) == len(SLASH_COMMANDS)
        assert not any(row.has_class("cmd-active") for row in rows)

        starts = set()
        for row, (_, usage, description) in zip(rows, SLASH_COMMANDS, strict=True):
            plain = row.render().plain
            # [refresh] 等方括号必须原样可见，不能被当成标记吞掉
            assert usage in plain
            starts.add(plain.index(description))
        assert len(starts) == 1


async def test_command_palette_scrolls_highlighted_command_into_view(layout_app):
    from syvrio.app.tui.widgets import CommandPalette

    app = layout_app
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#input", Input).value = "/"
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        assert palette.listing.max_scroll_y > 0

        for _ in range(len(SLASH_COMMANDS)):
            await pilot.press("down")
        await pilot.pause()
        assert palette.index == len(SLASH_COMMANDS) - 1
        last = palette.rows()[-1]
        assert last.region.y >= palette.listing.region.y
        assert last.region.bottom <= palette.listing.region.bottom

        await pilot.press("up")
        await pilot.pause()
        assert palette.index == len(SLASH_COMMANDS) - 2
        previous = palette.rows()[palette.index]
        assert previous.region.y >= palette.listing.region.y
        assert previous.region.bottom <= palette.listing.region.bottom


async def test_command_palette_mouse_wheel_scrolls(layout_app):
    from textual.events import MouseScrollDown

    from syvrio.app.tui.widgets import CommandPalette

    app = layout_app
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#input", Input).value = "/"
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        before = palette.listing.scroll_y
        palette.listing.post_message(
            MouseScrollDown(palette.listing, 0, 0, 0, 1, 0, False, False, False)
        )
        await pilot.pause()
        assert palette.listing.scroll_y > before


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [("/pro", ["profile"]), ("/dre", ["dream"]), ("/inf", ["inferences"])],
)
async def test_command_palette_prefix_filters(layout_app, prefix, expected):
    from syvrio.app.tui.widgets import CommandPalette

    app = layout_app
    async with app.run_test() as pilot:
        app.query_one("#input", Input).value = prefix
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        assert palette.display
        assert [match[0] for match in palette.matches] == expected


async def test_command_palette_click_fills_visible_command_before_running(resident_backend):
    import asyncio

    from syvrio.app.tui.widgets import CommandPalette

    owner, client = resident_backend
    app = ResidentChatApp(client)
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/hel"
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        await pilot.click(palette.rows()[0])
        assert widget.value == "/help"
        assert len(app.screen_stack) == 1
        await pilot.press("enter")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert not palette.display
        assert str(app.screen.query_one(".pick-title").render()) == "Commands"


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_command_palette_fits_small_viewport_and_input_stays_visible(layout_app, size):
    from syvrio.app.tui.widgets import CommandPalette

    app = layout_app
    async with app.run_test(size=size) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/"
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        assert palette.display
        assert palette.region.height <= 16
        assert palette.region.bottom <= widget.region.y
        assert widget.region.height > 0

        for _ in range(len(SLASH_COMMANDS)):
            await pilot.press("down")
        await pilot.pause()
        last = palette.rows()[-1]
        assert last.region.y >= palette.listing.region.y
        assert last.region.bottom <= palette.listing.region.bottom


async def test_rename_command_prompts_for_title(resident_backend):
    from syvrio.app.tui.widgets import TextInputScreen

    owner, client = resident_backend
    session = owner._sessions.create()
    owner._sessions.rename(session, "旧标题")
    app = ResidentChatApp(client, session.id)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", Input)
        widget.value = "/rename"
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, TextInputScreen):
                break
        assert isinstance(app.screen, TextInputScreen)
        title_input = app.screen.query_one("#pick-input", Input)
        assert title_input.value == "旧标题"

        title_input.value = "新标题"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if owner._runtime.current.title() == "新标题":
                break
        assert owner._runtime.current.title() == "新标题"
        assert owner._sessions.load(session.id).title() == "新标题"


@pytest.mark.parametrize("shortcut,sentinel", [("ctrl+r", "__refresh__"), ("ctrl+n", "__custom__")])
async def test_model_picker_shortcuts_work_from_search(layout_app, monkeypatch, shortcut, sentinel):
    selected = []
    app = layout_app
    monkeypatch.setattr(app, "_model_choice", selected.append)
    async with app.run_test(size=(120, 40)) as pilot:
        app.push_screen(ModelPickerScreen("Model", [], app._model_choice, searchable=True))
        await pilot.pause()
        assert app.screen.query_one("#pick-search", Input).has_focus
        await pilot.press(shortcut)
        await pilot.pause()
        assert selected == [(sentinel, "")]


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_model_picker_keeps_shortcut_hint_visible(layout_app, size):
    app = layout_app
    entries = [(f"model-{i}", ("deepseek", f"model-{i}"), i == 0) for i in range(50)]
    async with app.run_test(size=size) as pilot:
        app.push_screen(ModelPickerScreen("Model", entries, app._model_choice, searchable=True))
        await pilot.pause()
        hint = app.screen.query_one(".pick-hint", Static)
        card = app.screen.query_one("#pick-card")
        assert hint.region.bottom <= card.content_region.bottom
        assert hint.region.height > 0
        assert "Ctrl+P" in str(hint.render())
        assert card.region.bottom <= app.screen.region.bottom


async def test_ctrl_p_outside_model_picker_keeps_command_palette(layout_app):
    app = layout_app
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "PickScreen"


async def test_model_picker_filters_by_search(layout_app):
    import asyncio

    from textual.widgets import ListView

    from syvrio.app.tui.widgets import PickScreen

    app = layout_app
    entries = [
        ("DeepSeek · DeepSeek Flash", ("deepseek", "deepseek-flash"), True),
        ("SiliconFlow · DeepSeek V3", ("siliconflow", "deepseek-ai/DeepSeek-V3"), False),
        ("Spark · Spark X2.6", ("spark", "spark-x2.6"), False),
    ]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(ModelPickerScreen("Model", entries, app._model_choice, searchable=True))
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, PickScreen)
        search = picker.query_one("#pick-search", Input)
        assert search.has_focus

        search.value = "silicon"
        # ListView 的移除与挂载异步完成，不能假设一帧后已经得到最终列表。
        async with asyncio.timeout(5):
            while [item.label for item in picker.query("#pick-list OptionItem")] != [
                "SiliconFlow · DeepSeek V3"
            ]:
                await pilot.pause()

        items = list(picker.query("#pick-list OptionItem"))
        assert [item.label for item in items] == ["SiliconFlow · DeepSeek V3"]
        listing = picker.query_one("#pick-list", ListView)
        assert listing.index == 0

        await pilot.press("down")
        await pilot.pause()
        # 方向键在搜索框内移动高亮，不把焦点抢到列表。
        assert search.has_focus
        assert listing.index == 0


async def test_model_picker_arrow_keys_keep_search_focus(layout_app):
    from textual.widgets import ListView

    selected = []
    app = layout_app
    entries = [(f"model-{i}", ("deepseek", f"model-{i}"), i == 0) for i in range(6)]
    async with app.run_test(size=(120, 40)) as pilot:
        original = app._model_choice
        app._model_choice = selected.append
        try:
            await pilot.pause()
            app.push_screen(ModelPickerScreen("Model", entries, app._model_choice, searchable=True))
            await pilot.pause()
            picker = app.screen
            search = picker.query_one("#pick-search", Input)
            listing = picker.query_one("#pick-list", ListView)
            assert search.has_focus

            await pilot.press("down", "down", "up")
            await pilot.pause()
            assert search.has_focus
            assert listing.index == 1

            # 方向键之后继续输入仍然过滤，不需要重新点搜索框。
            search.value = "model-4"
            await pilot.pause()
            assert search.has_focus
            assert [item.label for item in picker.query("#pick-list OptionItem")] == ["model-4"]

            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause()
                if selected:
                    break
            assert selected == [("deepseek", "model-4")]
        finally:
            app._model_choice = original


async def test_ctrl_b_toggles_sidebar(layout_app):
    from textual.containers import Vertical

    app = layout_app
    async with app.run_test(size=(120, 40)) as pilot:
        sidebar = app.query_one("#sidebar", Vertical)
        expand = app.query_one("#show-sidebar", SidebarToggle)
        assert not sidebar.display and expand.display
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert sidebar.display and not expand.display
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert not sidebar.display and expand.display
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert sidebar.display and not expand.display

        # 点侧栏右上角 « 隐藏；对话区左上角 » 恢复
        await pilot.click("#hide-sidebar")
        await pilot.pause()
        assert not sidebar.display and expand.display
        await pilot.click("#show-sidebar")
        await pilot.pause()
        assert sidebar.display and not expand.display


async def test_main_focus_stays_on_chat_input(layout_app):
    from textual.containers import Vertical
    from textual.widgets import ListView

    app = layout_app
    async with app.run_test(size=(100, 30)) as pilot:
        app.show_sidebar()
        await pilot.pause()
        await pilot.pause()
        widget = app.query_one("#input", Input)
        sessions = app.query_one("#sessions", ListView)
        assert sessions.can_focus is False
        assert widget.has_focus

        await pilot.press("tab")
        await pilot.pause()
        assert widget.has_focus

        await pilot.press("shift+tab")
        await pilot.pause()
        assert widget.has_focus

        app._set_sidebar_visible(False)
        app.show_sidebar()
        await pilot.pause()
        assert widget.has_focus
        assert app.query_one("#sidebar", Vertical).display


async def test_clicking_chat_keeps_input_focus(layout_app):
    from textual.containers import VerticalScroll

    app = layout_app
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.add_assistant_turn("点击正文不应该抢走输入焦点", "12:00")
        await pilot.pause()
        widget = app.query_one("#input", Input)
        chat = app.query_one("#chat", VerticalScroll)
        assert chat.can_focus is False
        assert widget.has_focus

        await pilot.click(chat)
        await pilot.pause()
        assert widget.has_focus

        await pilot.click(".reply")
        await pilot.pause()
        assert widget.has_focus


async def test_sidebar_rows_align_with_new_session(resident_backend):
    # 回归：New session 与会话列表必须同宽同起点；列表滚动时也不能缩窄。
    owner, client = resident_backend
    app = ResidentChatApp(client)
    store = owner._sessions
    for index in range(12):
        session = store.create()
        store.rename(session, f"会话 {index}")
        store.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": f"内容 {index}"},
        )
    async with app.run_test(size=(100, 30)) as pilot:
        app.show_sidebar()
        await pilot.pause()
        for _ in range(60):
            await pilot.pause()
            if app.query("#sessions > ListItem"):
                break
        new_session = app.query_one("#new-session").region
        rows = [item.region for item in app.query("#sessions > ListItem") if hasattr(item, "row")]
        assert rows
        assert all(row.x == new_session.x and row.width == new_session.width for row in rows)


async def test_sidebar_items_are_compact_and_clickable(resident_backend):
    from syvrio.app.tui.widgets import SessionItem
    from syvrio.infra.session import new_message_id

    owner, client = resident_backend
    app = ResidentChatApp(client)
    ids = []
    for title in ("会话甲", "会话乙"):
        session = owner._sessions.create()
        owner._sessions.rename(session, title)
        owner._sessions.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": title},
        )
        ids.append(session.id)
    async with app.run_test(size=(100, 30)) as pilot:
        app.show_sidebar()
        await pilot.pause()
        await app._poll(force=True)
        await app.workers.wait_for_complete()
        await pilot.pause()
        items = list(app.query(SessionItem))
        assert len(items) >= 2
        # 回归：ListItem 内的 Vertical 默认 1fr 会把条目撑满列表导致命中错乱
        assert all(item.region.height <= 3 for item in items)

        second = items[1]
        await pilot.click(second)
        for _ in range(100):
            await pilot.pause()
            if not app._submitting.locked() and owner._runtime.has_session:
                break
        assert owner._runtime.current.id == second.row["id"]
        assert app.query_one("#input", Input).has_focus


async def test_session_switch_failure_releases_busy(resident_backend, monkeypatch):
    from textual.selection import SELECT_ALL

    from syvrio.app.tui.resident import ResidentChatApp

    owner, client = resident_backend
    original = owner._sessions.create()
    owner._runtime.model = FakeChatModel([AssistantTurn(content="still available")])
    app = ResidentChatApp(client, original.id)
    async with app.run_test() as pilot:
        notices = []
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))
        app._run_request(app._select("s-20260101-000000-dead"))
        await app.workers.wait_for_complete()
        assert any("Session not found" in message for message in notices)
        assert app.sid == original.id and not app.query_one("#input", Input).disabled
        app.query_one("#input", Input).value = "after failed switch"
        await pilot.press("enter")
        for _ in range(80):
            await pilot.pause(0.05)
            if any(
                "still available" in widget.get_selection(SELECT_ALL)[0]
                for widget in app.query(".reply")
            ):
                break
        assert owner._sessions.load(original.id).user_texts() == ["after failed switch"]
        assert "still available" in app.query_one(".reply").get_selection(SELECT_ALL)[0]


def test_slash_commands_cover_aliases():
    # 回归：面板必须列出 _command 实际支持的别名，/qu、/mo 前缀过滤要有提示。
    commands = {command for command, _, _ in SLASH_COMMANDS}
    assert "quit" in commands and "model" in commands


def _cell_style(strip, x):
    offset = 0
    for segment in strip._segments:
        if offset <= x < offset + segment.cell_length:
            return segment.style
        offset += segment.cell_length
    return None


def _session_rail_cells(picker) -> dict[str, dict]:
    """读取选择器最终渲染的字符网格：竖线字符、列、行高与标题列。"""
    strips = picker._compositor.render_strips()
    cells = {}
    for item in picker.query("#picker-list SessionItem"):
        if not item.visible:
            continue
        region = item.region
        line = strips[region.y].text
        cells[item.row["id"]] = {
            "x": region.x,
            "char": line[region.x],
            "height": item.size.height,
            "title_x": item.query_one(".session-title").region.x,
            "inner": line[region.x + 1 : region.x + 8],
        }
    return cells


def _session_visible_rails(picker) -> set[str]:
    """竖线字符始终占位；前景与背景相同即这一行没有可见竖线。"""
    strips = picker._compositor.render_strips()
    visible = set()
    for item in picker.query("#picker-list SessionItem"):
        if not item.visible:
            continue
        style = _cell_style(strips[item.region.y], item.region.x)
        if style.color != style.bgcolor:
            visible.add(item.row["id"])
    return visible


async def test_session_picker_focus_bar_follows_selection(resident_backend):
    # 回归：竖线只标记选中行；普通/当前行保留同宽占位但不显示，切换时位置和行高不变。
    from textual.color import Color
    from textual.widgets import ListView

    from syvrio.app.tui.widgets import SessionItem, SessionPickerScreen

    owner, client = resident_backend
    app = ResidentChatApp(client)
    store = owner._sessions
    ids = []
    for title in ("当前会话", "第二条会话", "第三条会话"):
        session = store.create()
        store.rename(session, title)
        store.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": title},
        )
        ids.append(session.id)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app._select(ids[0])
        await app._command("/sessions")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, SessionPickerScreen)
        rows = {row.row["id"]: row for row in picker.query("#picker-list SessionItem")}
        assert len(rows) == 3

        listing = picker.query_one("#picker-list", ListView)
        current = rows[ids[0]]
        assert current.has_class("current")
        assert "← current" in str(current.query_one(".session-meta").content)

        for selection in range(3):
            listing.index = selection
            await pilot.pause()
            selected = listing.highlighted_child
            assert isinstance(selected, SessionItem)
            selected_id = selected.row["id"]

            cells = _session_rail_cells(picker)
            assert len(cells) == 3
            # 三行始终共用同一列、同一字符、同一行高和同一标题列。
            assert len({cell["x"] for cell in cells.values()}) == 1
            assert {cell["char"] for cell in cells.values()} == {"┃"}
            assert len({cell["height"] for cell in cells.values()}) == 1
            assert len({cell["title_x"] for cell in cells.values()}) == 1
            assert all("┃" not in cell["inner"] for cell in cells.values())  # 没有第二根线
            # 普通行与当前行不显示竖线，只有选中行出现蓝色竖线。
            assert _session_visible_rails(picker) == {selected_id}

            for session_id, row in rows.items():
                assert row.styles.border_left[0] == "heavy"
                if session_id == selected_id:
                    assert row.styles.border_left[1] == Color.parse("#6cb6ff")
                    assert row.styles.background == Color.parse("#303030")
                else:
                    assert row.styles.background != Color.parse("#303030")
                    if row.has_class("current"):
                        assert row.styles.background == Color.parse("#272727")
                        assert row.styles.border_left[1] == Color.parse("#272727")
                    else:
                        assert row.styles.border_left[1].a == 0


@pytest.mark.parametrize("size", [(60, 24), (100, 30), (160, 40)])
async def test_session_picker_rail_stays_aligned_for_long_titles_and_widths(resident_backend, size):
    # 长标题只截断不换行；窄终端和宽终端下竖线列都不抖动、不溢出。
    from textual.widgets import ListView

    from syvrio.app.tui.widgets import SessionPickerScreen

    owner, client = resident_backend
    app = ResidentChatApp(client)
    store = owner._sessions
    titles = (
        ("超长会话标题" * 12 + " https://github.com/example/repository")[:80],
        "短标题",
        "第三条会话",
    )
    ids = []
    for title in titles:
        session = store.create()
        store.rename(session, title)
        store.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": title},
        )
        ids.append(session.id)

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await app._select(ids[0])
        await app._command("/sessions")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, SessionPickerScreen)
        listing = picker.query_one("#picker-list", ListView)
        footer = picker.query_one(".pick-hint", Static)

        for selection in range(3):
            listing.index = selection
            await pilot.pause()
            cells = _session_rail_cells(picker)
            assert len(cells) == 3
            assert len({cell["x"] for cell in cells.values()}) == 1
            assert {cell["char"] for cell in cells.values()} == {"┃"}
            assert len({cell["title_x"] for cell in cells.values()}) == 1
            # 长标题保持单行截断，行高一致。
            assert {cell["height"] for cell in cells.values()} == {2}
            assert all(
                row.query_one(".session-title").size.height == 1
                for row in picker.query("#picker-list SessionItem")
            )
            # 列表不横向溢出，底部提示仍完整可见。
            assert all(
                item.region.right <= listing.region.right
                for item in picker.query("#picker-list SessionItem")
            )
            assert footer.region.bottom <= picker.query_one("#pick-card").region.bottom


async def test_session_picker_escape_and_arrow_navigation_with_rail(resident_backend):
    # 竖线改动不影响 ↓ 进入列表、继续 ↓ 切换选中和 ESC 取消。
    from textual.widgets import ListView

    from syvrio.app.tui.widgets import SessionItem, SessionPickerScreen

    owner, client = resident_backend
    app = ResidentChatApp(client)
    store = owner._sessions
    for title in ("甲会话", "乙会话", "丙会话"):
        session = store.create()
        store.rename(session, title)
        store.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": title},
        )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app._command("/sessions")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, SessionPickerScreen)
        listing = picker.query_one("#picker-list", ListView)
        first = listing.highlighted_child
        assert isinstance(first, SessionItem)
        for _ in range(50):
            # 启动预热线程会拉长首帧；等列表真正布局后再取网格基线。
            aligned = _session_rail_cells(picker)
            if aligned and all(cell["height"] for cell in aligned.values()):
                break
            await pilot.pause()

        await pilot.press("down")
        await pilot.pause()
        assert listing.has_focus
        await pilot.press("down")
        await pilot.pause()
        second = listing.highlighted_child
        assert isinstance(second, SessionItem) and second is not first
        assert _session_rail_cells(picker) == aligned  # 只有颜色和底色变化，网格不变
        assert _session_visible_rails(picker) == {second.row["id"]}

        await pilot.press("escape")
        for _ in range(50):
            await pilot.pause()
            if not isinstance(app.screen, SessionPickerScreen):
                break
        assert not isinstance(app.screen, SessionPickerScreen)
        assert app.query_one("#input", Input).has_focus


def test_startup_commits_dirty_vault_without_crashed_sessions(service):
    from syvrio.backend.api.channel import APIChannel as WebBackend

    service.config.git.auto_sync = False
    service.config.search.auto_start = False
    owner = WebBackend(service=service)
    initialize_vault(owner.git, service.initialize_files)
    service.note("Uncommitted synthetic fact", topic="misc")
    assert owner.git.repo.has_changes()
    owner.startup()
    try:
        assert not owner.health().busy
        assert not owner.git.repo.has_changes()
    finally:
        owner.shutdown()


async def test_chat_scrollbar_is_thin(layout_app):
    from textual.containers import Vertical, VerticalScroll

    app = layout_app
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        chat = app.query_one("#chat", VerticalScroll)
        assert chat.styles.scrollbar_size_vertical == 1
        # 滚动条要贴着窗口右缘，而不是停在对话区右侧留白里。
        assert chat.region.right == app.query_one("#main", Vertical).region.right
        assert chat.styles.padding.right == 0


async def test_input_hint_uses_border_not_text(layout_app):
    # 回归：提示词留在文字区会和中文输入法的组字内容重叠。
    app = layout_app
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", BarInput)
        assert widget.placeholder == ""
        assert widget.border_title == "Message or /command"
        assert "Message or /command" not in widget.render_line(0).text

        widget.value = "你好"
        await pilot.pause()
        assert widget.border_title is None
        assert "Message or /command" not in widget.render_line(0).text

        widget.value = ""
        await pilot.pause()
        assert widget.border_title == "Message or /command"


async def test_input_cursor_is_vertical_bar(layout_app):
    app = layout_app
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", Input)
        assert isinstance(widget, BarInput)
        # 直接检验可见阶段，避免依赖闪烁定时器。
        widget._cursor_visible = True
        assert "│" in widget.render_line(0).text
        assert not widget.get_component_rich_style("input--cursor").underline

        widget.value = "测试"
        await pilot.pause()
        widget._cursor_visible = True
        rendered = widget.render_line(0).text
        assert "│" in rendered
        assert "测试" in rendered
        # 光标只在渲染层插入；写进 _value 会让 Textual 把输入背景刷到左侧。
        assert "│" not in widget._value.plain

        widget.blur()
        await pilot.pause()
        assert "│" not in widget.render_line(0).text


def test_auto_start_warms_search_in_background(service, monkeypatch):
    from syvrio.backend.api.channel import APIChannel as WebBackend

    started, release = threading.Event(), threading.Event()
    calls = []

    def warm(base_url):
        calls.append(base_url)
        started.set()
        assert release.wait(8)
        return "running"

    service.config.search = SearchConfig(enabled=True, auto_start=True)
    service.config.git.auto_sync = False
    monkeypatch.setattr("syvrio.infra.search.daemon.ensure_daemon", warm)
    owner = WebBackend(service=service)
    owner.startup()
    try:
        assert started.wait(5)
        assert not owner.health().busy
        other = owner._sessions.create()
        assert owner.switch_session(other.id).session_id == other.id
        assert calls == [service.config.search.base_url]
    finally:
        release.set()
        owner.shutdown()


def test_search_warmup_thread_unavailable_keeps_backend_usable(service, monkeypatch):
    from syvrio.backend.api.channel import APIChannel as WebBackend

    original_start = threading.Thread.start
    errors = []

    def start(thread):
        if thread.name == "syvrio-search-warmup":
            raise RuntimeError("unavailable")
        original_start(thread)

    service.config.git.auto_sync = False
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(
        "syvrio.backend.channel.record_event", lambda event, exc: errors.append(event)
    )
    owner = WebBackend(service=service)
    owner.startup()
    try:
        assert not owner.health().busy
        assert errors == ["backend.startup_hook_failed"]
    finally:
        owner.shutdown()
    # 新启动的实例可以获取同一 Vault 和 Runtime 的所有权。
    successor = WebBackend(service=service)
    successor.startup()
    successor.shutdown()


async def test_startup_catchup_advances_crashed_session_cursor(service, monkeypatch):
    from syvrio.backend.api.channel import APIChannel as WebBackend
    from syvrio.memory.background import distillation

    service.config.git.auto_sync = False
    service.config.search.auto_start = False
    service.initialize_files()
    sessions = SessionStore(service.paths)
    crashed = sessions.create()
    message_id = new_message_id()
    sessions.append(
        crashed, {"type": "message", "id": message_id, "role": "user", "content": "Synthetic fact"}
    )
    calls = []

    def extract(*_):
        calls.append(message_id)
        return distillation.Result(memories=[])

    monkeypatch.setattr(
        "syvrio.infra.llm.client.model_metadata", lambda _: {"max_input_tokens": 32_000}
    )
    monkeypatch.setattr(distillation, "ask", extract)
    owner = WebBackend(service=service)
    owner.startup()
    try:
        async with asyncio.timeout(8):
            while sessions.load(crashed.id).undistilled_messages():
                await asyncio.sleep(0.02)
        assert not owner.health().busy
        assert not sessions.unconsolidated(limit=10)
        assert calls == [message_id]
    finally:
        owner.shutdown()


async def _open_resident_pending(app, pilot, line="/pending"):
    from syvrio.app.tui.screens.pending import PendingScreen

    app.query_one("#input", Input).value = line
    await pilot.press("enter")
    await app.workers.wait_for_complete()
    await pilot.pause()
    assert isinstance(app.screen, PendingScreen)
    return app.screen


async def test_pending_direct_edit_opens_full_text_and_confirms_unchanged(
    service, resident_backend
):
    from textual.widgets import TextArea

    from syvrio.app.tui.widgets import PickScreen
    from syvrio.memory.models import MemoryDraft

    app = ResidentChatApp(resident_backend[1])
    service.create_pending(MemoryDraft(text="Another candidate", topic="review"), "Keep it?", [])
    item = service.create_pending(
        MemoryDraft(text="Original text\nSecond line", topic="review"), "Keep it?", []
    )
    async with app.run_test() as pilot:
        screen = await _open_resident_pending(app, pilot, f"/pending edit {item.id}")
        assert screen.query_one(TextArea).text == item.text
        await pilot.press("ctrl+s")
        assert isinstance(app.screen, PickScreen)
        assert app.screen.query_one("#pick-detail-text", Static).content == item.text
        await pilot.press("escape")
        assert app.screen is screen and service.queue.get(item.id) == item
        await pilot.press("ctrl+s", "down", "enter")
        await app.workers.wait_for_complete()
        assert service.queue.get(item.id) is None
        assert [entry.text for entry in service.current_facts()] == [item.text]


@pytest.mark.parametrize("accepted_first", [False, True])
async def test_resident_redelivery_preserves_next_draft_and_sends_once(
    resident_backend, monkeypatch, accepted_first
):
    import httpx

    _, client = resident_backend
    original = client._submit
    entered, release = threading.Event(), threading.Event()
    attempts = []

    def disconnect(message, sid, files, delivery):
        attempts.append(delivery)
        if len(attempts) == 1:
            if accepted_first:
                original(message, sid, files, delivery)
            entered.set()
            assert release.wait(5)
        if len(attempts) <= 2:
            raise httpx.ReadError("Synthetic connection loss")
        return original(message, sid, files, delivery)

    monkeypatch.setattr(client, "_submit", disconnect)
    app = ResidentChatApp(client)
    async with app.run_test() as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "First message"
        await pilot.press("enter")
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            widget.value = "New unsent draft"
        finally:
            release.set()
        await app.workers.wait_for_complete()
        assert app._unsent and widget.value == "New unsent draft"
        await pilot.press("ctrl+r")
        await app.workers.wait_for_complete()
        rows = client.request("GET", f"/api/sessions/{app.sid}")["items"]
        assert [row["content"] for row in rows if row["role"] == "user"] == ["First message"]
        assert len(attempts) == 3 and len(set(attempts)) == 1
        assert widget.value == "New unsent draft" and not app._unsent


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_pending_decision_keeps_tui_responsive(
    service, resident_backend, monkeypatch, action
):
    from syvrio.app.tui.resident import ResidentChatApp
    from syvrio.app.tui.widgets import PickScreen
    from syvrio.memory.models import MemoryDraft

    _, client = resident_backend
    app = ResidentChatApp(client)
    related = service.add(MemoryDraft(text="An existing preference", topic="preferences"))
    item, remaining = [
        service.create_pending(
            MemoryDraft(text=text, topic="preferences"), "Keep this preference?", [related.id]
        )
        for text in ("Synthetic first preference", "Synthetic second preference")
    ]
    started, release = threading.Event(), threading.Event()
    decide = getattr(service, action)

    def delayed_decision(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return decide(*args, **kwargs)

    monkeypatch.setattr(service, action, delayed_decision)
    notifications = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append(message))
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        screen = await _open_resident_pending(app, pilot)
        assert screen.query_one("#pending-count", Static).content == "Pending 2"
        assert screen.query_one("#pending-reason", Static).content == (
            f"Keep this preference?\n{related.id}"
        )
        try:
            await pilot.press("r" if action == "reject" else "c")
            for _ in range(50):
                if started.is_set():
                    break
                await pilot.pause()
            assert started.is_set()
            assert screen.query_one("#pending-approve", Button).disabled
            assert app.screen is screen
            if action == "reject":
                await pilot.press("escape")
                widget.value = "/help"
                await pilot.press("enter")
                for _ in range(50):
                    if isinstance(app.screen, PickScreen):
                        break
                    await pilot.pause()
                assert isinstance(app.screen, PickScreen)
                assert app.screen.query_one(".pick-title", Static).content == "Commands"
        finally:
            release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [pending.id for pending in service.pending()] == [remaining.id]
        assert {entry.text for entry in service.current_facts()} == (
            {related.text, item.text} if action == "approve" else {related.text}
        )
        if action == "approve":
            assert app.screen is screen
            assert screen.query_one("#pending-count", Static).content == "Pending 1"
            assert screen.query_one("#pending-text", Static).content == remaining.text
            assert "approve applied" in str(screen.query_one("#pending-message", Static).content)
        else:
            assert isinstance(app.screen, PickScreen)
            assert any("reject applied" in message for message in notifications)
            assert item.text in service.queue.resolved_path.read_text(encoding="utf-8")
        await pilot.press("escape")
        assert widget.has_focus


async def test_pending_reopen_waits_for_decision_and_preserves_next_draft(
    service, resident_backend, monkeypatch
):
    from textual.widgets import TextArea

    from syvrio.app.tui.resident import ResidentChatApp
    from syvrio.app.tui.screens.pending import PendingScreen
    from syvrio.memory.models import MemoryDraft

    _, client = resident_backend
    app = ResidentChatApp(client)
    first, second = [
        service.create_pending(MemoryDraft(text=text, topic="preferences"), "Keep it?", [])
        for text in ("First candidate", "Second candidate")
    ]
    entered, release = threading.Event(), threading.Event()
    reject = service.reject

    def slow_reject(key):
        entered.set()
        assert release.wait(10)
        return reject(key)

    monkeypatch.setattr(service, "reject", slow_reject)
    async with app.run_test() as pilot:
        first_screen = await _open_resident_pending(app, pilot)
        try:
            await pilot.press("r")
            assert await asyncio.to_thread(entered.wait, 5)
            await pilot.press("escape")
            app.query_one("#input", Input).value = "/pending"
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen is app.default_screen
        finally:
            release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        second_screen = app.screen
        assert isinstance(second_screen, PendingScreen) and second_screen is not first_screen
        assert second_screen.query_one("#pending-text", Static).content == second.text
        assert service.queue.get(first.id) is None
        await pilot.press("e")
        editor = second_screen.query_one(TextArea)
        editor.load_text("Revised second candidate")
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        assert [row.text for row in service.current_facts()] == ["Revised second candidate"]


async def test_pending_saved_decision_with_truncated_receipt_and_failed_reload(
    service, resident_backend, monkeypatch
):
    from syvrio.app.tui.resident import ResidentChatApp
    from syvrio.application import commands
    from syvrio.memory.models import MemoryDraft

    _, client = resident_backend
    app = ResidentChatApp(client)
    full_snapshot = commands._pending_data

    def legacy_receipt(items):
        data = full_snapshot(items)
        return {**data, "rows": data["rows"][:1]}

    monkeypatch.setattr(commands, "_pending_data", legacy_receipt)
    rows = [
        service.create_pending(MemoryDraft(text=text, topic="preferences"), "Keep it?", [])
        for text in ("First candidate", "Second candidate", "Third candidate")
    ]
    notices = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))
    async with app.run_test() as pilot:
        await _open_resident_pending(app, pilot)

        def broken_reload():
            raise OSError("list unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(client, "pending", broken_reload)
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
        assert app.screen is app.default_screen
        assert any("reject applied" in msg and "Reopen /pending" in msg for msg in notices)
        assert service.pending() == rows[1:]
        reopened = await _open_resident_pending(app, pilot)
        assert reopened.query_one("#pending-text", Static).content == rows[1].text


@pytest.mark.parametrize("failure", ["write", "transport"])
async def test_pending_interactive_edit_approves_only_final_text(
    service, resident_backend, monkeypatch, failure
):
    from textual.widgets import TextArea

    from syvrio.app.tui.resident import ResidentChatApp
    from syvrio.memory.models import MemoryDraft

    _, client = resident_backend
    app = ResidentChatApp(client)
    notifications = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append(message))
    item = service.create_pending(
        MemoryDraft(text="可能喜欢浅烘", topic="preferences"), "Which roast?", []
    )
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        screen = await _open_resident_pending(app, pilot)
        await pilot.press("e")
        field = screen.query_one(TextArea)
        assert field.text == item.text
        assert field.has_focus
        edited = "喜欢中烘咖啡\n冲煮时使用滤纸。"
        field.load_text(edited)
        await pilot.press("enter")
        assert field.text == "\n" + edited
        assert service.pending() == [item]

        def fail_approval(*args, **kwargs):
            raise OSError("synthetic write failure")

        with monkeypatch.context() as patch:
            if failure == "write":
                patch.setattr(service, "approve", fail_approval)
            else:
                request = client.request

                def broken_transport(method, path, **kwargs):
                    if path == "/api/commands":
                        raise OSError("synthetic connection error")
                    return request(method, path, **kwargs)

                patch.setattr(client, "request", broken_transport)
            await pilot.press("ctrl+s")
            await app.workers.wait_for_complete()
            await pilot.pause()
        assert app.screen is screen
        assert field.text == "\n" + edited
        assert field.has_focus
        message = str(screen.query_one("#pending-message", Static).content)
        assert ("OSError" if failure == "write" else "synthetic connection error") in message
        assert service.pending() == [item]
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        entries = service.store.entries(include_inactive=True)
        assert [(entry.text, entry.status, entry.supersedes) for entry in entries] == [
            (edited, "active", None)
        ]
        assert service.pending() == []
        assert app.sid is None
        assert not app.query(".bubble")
        assert not app.query(".reply")
        assert app.screen is app.default_screen
        assert widget.value == ""
        assert widget.has_focus
        assert any("edit applied" in message for message in notifications)


async def test_topics_command_lists_and_drills(resident_backend, service):
    from syvrio.app.tui.resident import ResidentChatApp
    from syvrio.app.tui.widgets import PickScreen
    from syvrio.memory.models import MemoryDraft

    _, client = resident_backend
    service.add(MemoryDraft(text="Synthetic coffee preference", topic="food/preferences"))
    app = ResidentChatApp(client)
    async with app.run_test() as pilot:
        await app._command("/topics")
        await pilot.pause()
        assert isinstance(app.screen, PickScreen)
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert any(
            "Synthetic coffee preference" in str(card.render()) for card in app.query(".card")
        )


async def test_reading_diagnostics_records_state_transitions_without_content(
    layout_app, monkeypatch
):
    from syvrio.app.tui import widgets as chat_widgets

    logged = []
    monkeypatch.setattr(
        chat_widgets, "record_event", lambda *args, **kwargs: logged.append((args, kwargs))
    )
    app = layout_app
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app._chat()
        logged.clear()
        chat.follow_output = False
        chat.record_reading_state()
        chat.record_reading_state()
        chat.selecting = True
        chat.record_reading_state()
        assert len(logged) == 2
        assert all(args == ("reading.changed",) for args, _ in logged)
        assert logged[0][1]["follow"] is False
        assert logged[1][1]["selecting"] is True
        assert all(
            type(value) in (bool, int, float) for _, state in logged for value in state.values()
        )


async def test_switch_defers_distill_then_catches_up_in_background(resident_backend, monkeypatch):
    import asyncio

    from syvrio.memory.background import distillation
    from syvrio.memory.models import MemoryDraft

    owner, client = resident_backend
    entered, release = threading.Event(), threading.Event()

    def extract(service, prompt, data, schema):
        entered.set()
        assert release.wait(8)
        return schema(memories=[MemoryDraft(text=data["source_text"], topic="preferences")])

    # 后台模型是合成替身，固定其容量，避免把 LiteLLM 首次加载模型目录的时间算入切换时限。
    monkeypatch.setattr(
        "syvrio.infra.llm.client.model_metadata", lambda _: {"max_input_tokens": 32_000}
    )
    monkeypatch.setattr(distillation, "ask", extract)
    first = owner._sessions.create()
    second = owner._sessions.create()
    owner._sessions.append(
        first, {"type": "message", "role": "user", "id": "u1", "content": "Synthetic preference"}
    )
    client.request("POST", f"/api/sessions/{first.id}/switch")
    try:
        client.request("POST", f"/api/sessions/{second.id}/switch")
        assert await asyncio.to_thread(entered.wait, 5)
        assert client.request("GET", "/api/chat/queue")["session_id"] == second.id
        assert not owner._service.current_facts()
    finally:
        release.set()
    for _ in range(100):
        if not owner._sessions.load_snapshot(first.id).undistilled_messages():
            break
        await asyncio.sleep(0.05)
    assert not owner._sessions.load_snapshot(first.id).undistilled_messages()
    assert [fact.text for fact in owner._service.current_facts()] == ["Synthetic preference"]


async def test_startup_catchup_does_not_block_switching(service, monkeypatch):
    from syvrio.backend.api.channel import APIChannel as WebBackend
    from syvrio.memory.background import distillation

    service.config.search.auto_start = False
    service.config.git.auto_sync = False
    service.initialize_files()
    sessions = SessionStore(service.paths)
    crashed = sessions.create()
    sessions.append(
        crashed,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "Synthetic message"},
    )
    started, release = threading.Event(), threading.Event()

    def slow_extract(*_):
        started.set()
        assert release.wait(8)
        return distillation.Result(memories=[])

    monkeypatch.setattr(
        "syvrio.infra.llm.client.model_metadata", lambda _: {"max_input_tokens": 32_000}
    )
    monkeypatch.setattr(distillation, "ask", slow_extract)
    owner = WebBackend(service=service)
    owner.startup()
    try:
        assert await asyncio.to_thread(started.wait, 5)
        assert not owner.health().busy
        other = sessions.create()
        assert owner.switch_session(other.id).session_id == other.id
        release.set()
        async with asyncio.timeout(8):
            while sessions.load(crashed.id).undistilled_messages():
                await asyncio.sleep(0.02)
        assert owner._runtime.current.id == other.id
    finally:
        release.set()
        owner.shutdown()


async def test_transcript_replays_tool_events(resident_backend):
    # 回归：旧会话不仅回放问答正文，也要能看到当时的工具事件。
    import json

    from syvrio.agent.tools import SEARCH_NOTICE

    owner, client = resident_backend
    app = ResidentChatApp(client)
    session = owner._sessions.create()
    owner._sessions.append(
        session, {"type": "message", "id": "m1", "role": "user", "content": "查 GLM"}
    )
    owner._sessions.append(
        session,
        {
            "type": "assistant",
            "id": "a1",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "search", "arguments": {"query": "GLM-5.3"}}],
        },
    )
    payload = {
        "results": [{"title": "t", "url": "https://example.com", "snippet": "", "published": None}],
        "provider": "open_websearch",
        "failed": [],
    }
    owner._sessions.append(
        session,
        {
            "type": "tool",
            "id": "t1",
            "tool_call_id": "c1",
            "name": "search",
            "ok": True,
            "duration_ms": 10,
            "content": SEARCH_NOTICE + "\n" + json.dumps(payload, ensure_ascii=False),
        },
    )
    owner._sessions.append(session, {"type": "assistant", "id": "a2", "content": "最终回答"})
    app.sid = session.id

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        events = [str(widget.render()) for widget in app.query(".event-card")]
        assert any("example.com" in event and "search" in event for event in events)
        replies = [getattr(widget, "_markdown", "") for widget in app.query(".reply")]
        assert any("最终回答" in reply for reply in replies)


async def test_modal_lists_share_chat_scrollbar_style(resident_backend):
    from syvrio.app.tui.widgets import PickScreen, SessionPickerScreen

    def scrollbar_style(widget):
        return (
            widget.styles.scrollbar_size_vertical,
            widget.styles.scrollbar_background,
            widget.styles.scrollbar_color,
            widget.styles.scrollbar_color_hover,
            widget.styles.scrollbar_color_active,
        )

    owner, client = resident_backend
    app = ResidentChatApp(client)
    session = owner._sessions.create()
    owner._sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "会话"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        chat_style = scrollbar_style(app.query_one("#chat"))

        app.push_screen(ModelPickerScreen("Model", [], app._model_choice, searchable=True))
        await pilot.pause()
        assert isinstance(app.screen, PickScreen)
        model_list = app.screen.query_one("#pick-list")
        assert model_list.has_class("thin-scroll")
        assert scrollbar_style(model_list) == chat_style
        await pilot.press("escape")
        await pilot.pause()

        await app._command("/sessions")
        await pilot.pause()
        assert isinstance(app.screen, SessionPickerScreen)
        session_list = app.screen.query_one("#picker-list")
        assert session_list.has_class("thin-scroll")
        assert scrollbar_style(session_list) == chat_style


async def test_session_picker_is_centered(resident_backend):
    from syvrio.app.tui.widgets import SessionPickerScreen

    owner, client = resident_backend
    app = ResidentChatApp(client)
    session = owner._sessions.create()
    owner._sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "会话"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app._command("/sessions")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, SessionPickerScreen)
        card = picker.query_one("#pick-card")
        # 卡片应水平、垂直居中，而不是贴在左上角。
        assert abs(card.region.x - (picker.region.width - card.region.width) / 2) <= 1
        assert abs(card.region.y - (picker.region.height - card.region.height) / 2) <= 1
        assert "Ctrl+R" in str(picker.query_one(".pick-hint", Static).content)


async def test_dialog_inputs_share_evolution_focus_style(resident_backend):
    from textual.color import Color

    from syvrio.app.tui.widgets import SessionPickerScreen

    owner, client = resident_backend
    app = ResidentChatApp(client)
    session = owner._sessions.create()
    owner._sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "会话"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        main_input = app.query_one("#input", Input)
        assert main_input.styles.background == Color.parse("#202020")
        assert main_input.styles.border_left[1] == Color.parse("#6cb6ff")
        main_input.blur()
        await pilot.pause()
        assert main_input.styles.background == Color.parse("#202020")
        main_input.focus()

        await app._command("/sessions")
        await pilot.pause()
        assert isinstance(app.screen, SessionPickerScreen)
        picker_input = app.screen.query_one("#picker-input", Input)
        assert picker_input.styles.background == Color.parse("#222222")
        assert picker_input.styles.border_top[1] == main_input.styles.border_left[1]


async def test_session_picker_ctrl_r_renames_session(resident_backend):
    from syvrio.app.tui.widgets import SessionPickerScreen, TextInputScreen

    owner, client = resident_backend
    app = ResidentChatApp(client)
    session = owner._sessions.create()
    owner._sessions.rename(session, "旧标题")
    owner._sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "旧标题"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app._command("/sessions")
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, TextInputScreen)
        rename_input = app.screen.query_one("#pick-input", Input)
        assert rename_input.value == "旧标题"

        rename_input.value = "新标题"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, SessionPickerScreen):
                break
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert owner._sessions.load(session.id).title() == "新标题"
        titles = [item.row["title"] for item in app.screen.query("#picker-list SessionItem")]
        assert "新标题" in titles


async def test_session_picker_rename_current_updates_runtime(resident_backend):
    from syvrio.app.tui.widgets import SessionPickerScreen, TextInputScreen

    owner, client = resident_backend
    session = owner._sessions.create()
    owner._sessions.rename(session, "旧标题")
    owner._sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "旧标题"},
    )
    app = ResidentChatApp(client, session.id)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app._command("/sessions")
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, TextInputScreen)
        app.screen.query_one("#pick-input", Input).value = "当前新标题"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, SessionPickerScreen):
                break
        assert owner._runtime.current.title() == "当前新标题"


async def test_terminal_confirms_dream_through_the_shared_command(resident_backend, monkeypatch):
    from syvrio.app.tui.widgets import PickScreen
    from syvrio.application.commands import CommandResult

    owner, client = resident_backend
    app = ResidentChatApp(client)
    calls = []
    cards = []

    def dream(argument):
        calls.append(argument)
        return CommandResult("ok", "dream", "dream", "Shared Dream receipt.", {"rows": []})

    monkeypatch.setattr(owner._app_service.reflection, "command", dream)
    monkeypatch.setattr(app, "add_card", lambda text, *args: cards.append(text))
    async with app.run_test() as pilot:
        await app._command("/dream")
        await pilot.pause()
        assert isinstance(app.screen, PickScreen)
        await pilot.press("escape")
        assert not calls

        await app._command("/dream run")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert calls == ["run"]
        assert cards[-1] == "Shared Dream receipt."

        await app._command("/dream status")
        await pilot.pause()
        assert calls == ["run", "status"]


def test_fixed_tui_text_is_english():
    import ast
    import re
    from pathlib import Path

    from syvrio.app.tui import resident as resident_tui

    directory = Path(resident_tui.__file__).parent
    for path in (directory / name for name in ("resident.py", "commands.py", "widgets.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if (
                    node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                ):
                    docstrings.add(id(node.body[0].value))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                assert not re.search(r"[\u4e00-\u9fff]", node.value), (
                    f"{path.name}:{node.lineno}: {node.value}"
                )


async def test_native_input_cursor_tracks_editing_and_cleans_up(layout_app, monkeypatch):
    import asyncio

    app = layout_app
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", BarInput)
        writes = []
        cursor_shown = asyncio.Event()

        # 聚焦后要等刷新才输出光标位置，可能晚于测试驱动的暂停。
        def capture_cursor(data):
            if data in ("\x1b[5 q\x1b[?25h", "\x1b[?25l\x1b[0 q"):
                writes.append(data)
                if data == "\x1b[5 q\x1b[?25h":
                    cursor_shown.set()

        monkeypatch.setattr(app._driver, "write", capture_cursor)
        monkeypatch.setattr(BarInput, "_uses_native_cursor", property(lambda self: True))
        widget._restart_blink()
        widget._show_native_cursor()
        assert writes[-1] == "\x1b[5 q\x1b[?25h"
        widget.value = "你好abc"
        widget.cursor_position = 3
        await pilot.pause()
        assert app.cursor_position == widget.cursor_screen_offset
        assert "│" not in widget.render_line(0).text
        assert widget.value == "你好abc"
        assert not widget._cursor_visible
        widget.blur()
        await pilot.pause()
        assert writes[-1] == "\x1b[?25l\x1b[0 q"
        writes.clear()
        widget._hide_native_cursor()
        assert not writes
        cursor_shown.clear()
        widget.focus()
        await asyncio.wait_for(cursor_shown.wait(), timeout=5)
        assert widget.has_focus and widget._native_cursor_active
        assert writes[-1] == "\x1b[5 q\x1b[?25h"


class GatedChatModel:
    """每个回合在返回前等待各自的 Event，用于精确构造"请求进行中"的窗口。"""

    def __init__(self, turns, stream_first=False):
        self.turns = list(turns)
        self.gates: list[threading.Event] = []
        self._gate_index = 0
        self.calls: list[str] = []
        self.stream_first = stream_first

    def chat(self, messages, tools, on_delta=None):
        self.calls.append(str(messages[-1]["content"]) if messages else "")
        # 按调用序取门、不弹出：测试里始终用 gates[i] 控制第 i+1 个请求。
        gate = (
            self.gates[self._gate_index]
            if self._gate_index < len(self.gates)
            else threading.Event()
        )
        self._gate_index += 1
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if self.stream_first and on_delta is not None and turn.content:
            half = max(1, len(turn.content) // 2)
            on_delta(turn.content[:half])
            on_delta(turn.content[half:])
        gate.wait(timeout=30)
        if not self.stream_first and on_delta is not None and turn.content:
            half = max(1, len(turn.content) // 2)
            on_delta(turn.content[:half])
            on_delta(turn.content[half:])
        return turn


async def test_resident_ctrl_enter_routes_slash_commands_during_a_turn(
    resident_backend, monkeypatch
):
    owner, client = resident_backend
    model = GatedChatModel([AssistantTurn(content="Synthetic reply")])
    gate = threading.Event()
    model.gates.append(gate)
    owner._runtime.model = model
    session = owner._sessions.create()
    app = ResidentChatApp(client, session.id)
    notices = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))
    async with app.run_test() as pilot:
        widget = app.query_one("#input", Input)
        try:
            widget.value = "Synthetic turn"
            await pilot.press("enter")
            async with asyncio.timeout(5):
                while not (app._active_id and model.calls):
                    await pilot.pause(0.02)
            widget.value = "/undo extra"
            await pilot.press("ctrl+enter")
            async with asyncio.timeout(3):
                while "Invalid command." not in notices:
                    await pilot.pause(0.02)
            assert widget.value == ""
            assert app._active_id and model.calls == ["Synthetic turn"]
            widget.value = "/status"
            await pilot.press("ctrl+enter")
            async with asyncio.timeout(3):
                while not any(
                    "Generating: yes" in str(card.content) for card in app.query(".card")
                ):
                    await pilot.pause(0.02)
            assert owner._sessions.load(session.id).user_texts() == ["Synthetic turn"]
        finally:
            gate.set()


def test_native_cursor_requires_supported_terminal(monkeypatch):
    # conhost 无 DECSCUSR 支持：必须退回自绘光标，避免转义序列被画出来。
    for var in ("WT_SESSION", "WT_PROFILE_ID", "TERM_PROGRAM"):
        monkeypatch.delenv(var, raising=False)
    assert BarInput._terminal_supports_native_cursor() is False
    monkeypatch.setenv("WT_SESSION", "{abc}")
    assert BarInput._terminal_supports_native_cursor() is True
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    assert BarInput._terminal_supports_native_cursor() is True


@pytest.mark.parametrize(
    ("release", "term", "headless", "expected"),
    [
        ("6.6.87.2-microsoft-standard-WSL2", "xterm-256color", False, True),
        ("6.6.87.2-microsoft-standard-WSL2", "dumb", False, False),
        ("6.6.87.2-microsoft-standard-WSL2", "xterm-256color", True, False),
        ("6.12.0-amd64", "xterm-256color", False, False),
    ],
)
def test_wsl_native_cursor_without_windows_terminal_variables(
    monkeypatch,
    release,
    term,
    headless,
    expected,
):
    from types import SimpleNamespace

    from syvrio.app.tui import input as chat_input

    monkeypatch.setattr(chat_input.sys, "platform", "linux")
    monkeypatch.setattr(chat_input.platform, "release", lambda: release)
    monkeypatch.setenv("TERM", term)
    for name in ("WT_SESSION", "WT_PROFILE_ID", "TERM_PROGRAM"):
        monkeypatch.delenv(name, raising=False)
    widget = SimpleNamespace(app=SimpleNamespace(_driver=SimpleNamespace(is_headless=headless)))
    assert BarInput._uses_native_cursor.fget(widget) is expected


async def test_startup_compensation_runs_missed_daily_sync(service):
    from syvrio.backend.api.channel import APIChannel as WebBackend

    service.config.git = GitConfig(auto_sync=True, sync_time="00:00")
    service.config.search.auto_start = False
    owner = WebBackend(service=service)
    state_path = service.paths.state_dir / "git-sync.json"
    owner.startup()
    try:
        async with asyncio.timeout(8):
            while True:
                state = json.loads(state_path.read_text()) if state_path.exists() else {}
                if (
                    state.get("last_daily_blocked_date") == date.today().isoformat()
                    and not owner.git.running
                ):
                    break
                await asyncio.sleep(0.02)
        assert state["last_sync_trigger"] == "scheduled"
        assert state["last_daily_attempt_date"] == date.today().isoformat()
        assert state.get("last_daily_success_date") != date.today().isoformat()
        assert not owner.git.running
    finally:
        owner.shutdown()


async def test_reflection_status_shows_pending_inference_batch(
    resident_backend, service, monkeypatch
):
    from syvrio.app.tui.widgets import PickScreen
    from syvrio.memory.background import dream

    owner, client = resident_backend
    service.config.chat.default_budget = 1
    service.note("Synthetic fact for Dream", topic="preferences")
    monkeypatch.setattr(dream, "ask", lambda _, prompt, data, schema: schema(summary="Profile"))
    app = ResidentChatApp(client)
    async with app.run_test() as pilot:
        await app._command("/dream run", confirmed=True)
        async with asyncio.timeout(8):
            while owner._background.dream_status()["state"] != "paused":
                await pilot.pause(0.05)
        await app._command("/dream status")
        await pilot.pause()
        assert isinstance(app.screen, PickScreen)
        detail = str(app.screen.query_one("#pick-detail-text", Static).content)
        assert "State: paused" in detail and "Phase: hypotheses" in detail


async def test_attachment_picker_send_and_undo_through_resident(resident_backend, tmp_path):
    from textual.widgets import TextArea

    from syvrio.app.tui.screens.attachments import AttachmentScreen

    owner, client = resident_backend
    owner._runtime.model = FakeChatModel([AssistantTurn(content="Both files received")])
    paths = [tmp_path / name for name in ("first.txt", "second.md")]
    for path in paths:
        path.write_text(path.name, encoding="utf-8")
    app = ResidentChatApp(client)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._command("/import")
        await pilot.pause()
        assert isinstance(app.screen, AttachmentScreen)
        app.screen.query_one(TextArea).load_text("\n".join(map(str, paths)))
        await pilot.click("#attach-selected")
        await pilot.pause()
        composer = app.query_one("#input", Input)
        composer.value = "Read both attachments"
        await pilot.press("enter")
        async with asyncio.timeout(15):
            while not app.sid or not app.query(".reply") or app._ask_active:
                await pilot.pause(0.06)
        assert owner._sessions.verify_attachments() == (2, [])
        message = owner._sessions.load(app.sid).user_texts()[-1]
        assert message == "Read both attachments"
        await pilot.click("#undo-turn")
        async with asyncio.timeout(10):
            while composer.value != message:
                await pilot.pause(0.06)
        assert composer.has_focus
        assert owner._sessions.load(app.sid).user_texts() == []
        assert all(path.is_file() for path in paths)


async def test_archive_confirmation_defaults_to_cancel_and_targets_selected_row(resident_backend):
    from syvrio.app.tui.widgets import PickScreen, SessionPickerScreen

    owner, client = resident_backend
    store = owner._sessions
    current, target = store.create(), store.create()
    for item, title in ((current, "Current chat"), (target, "Archive me")):
        store.rename(item, title)
        store.append(
            item, {"type": "message", "role": "user", "id": new_message_id(), "content": title}
        )
    app = ResidentChatApp(client, current.id)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._poll(force=True)
        await app._command("/sessions")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, SessionPickerScreen)
        picker.query_one("#picker-input", Input).value = "Archive me"
        await pilot.pause()
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert isinstance(app.screen, PickScreen)
        assert "Archive me" in app.screen._title
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is picker and store.exists(target.id)
        await pilot.click("#session-delete")
        await pilot.pause()
        await pilot.press("down", "enter")
        async with asyncio.timeout(10):
            while not store.exists(target.id, archived=True):
                await pilot.pause(0.06)
        assert store.exists(current.id) and app.sid == current.id


async def test_model_picker_prioritizes_exact_api_id(layout_app):
    from textual.widgets import ListView

    model = "deepseek-v4.1-flash-1017"
    vendor_model = f"vanchin/{model}"
    choices = [
        (f"Recent · {vendor_model}", ("dashscope", vendor_model), False),
        (model, ("dashscope", model), True),
    ]
    async with layout_app.run_test() as pilot:
        screen = ModelPickerScreen("Model", choices, lambda _: None, searchable=True)
        layout_app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#pick-search", Input).value = model
        await pilot.pause()
        listing = screen.query_one("#pick-list", ListView)
        assert [item.value[1] for item in listing.children] == [model, vendor_model]
        assert listing.highlighted_child.value[1] == model


async def test_provider_key_entry_and_removal_confirmation_use_resident_commands(
    resident_backend, monkeypatch
):
    from textual.widgets import ListView

    from syvrio.app.tui.widgets import TextInputScreen

    _, client = resident_backend
    app = ResidentChatApp(client)
    sent = []
    original = client.request

    def request(method, path, **kwargs):
        if method == "POST" and path.startswith("/api/settings/providers/"):
            sent.append((path, kwargs["json"]))
            return {"message": "Updated"}
        return original(method, path, **kwargs)

    async def choose(index):
        listing = app.screen.query_one("#pick-list", ListView)
        listing.index = index
        listing.focus()
        await pilot.press("enter")
        await pilot.pause()

    monkeypatch.setattr(client, "request", request)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._providers()
        await pilot.pause()
        provider = app.screen.query_one("#pick-list", ListView).children[0].value
        await choose(0)
        await choose(0)
        assert isinstance(app.screen, TextInputScreen)
        editor = app.screen.query_one("#pick-input", Input)
        assert editor.password
        editor.value = "test-key"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert sent == [
            (
                f"/api/settings/providers/{provider['key']}",
                {"action": "set_key", "confirmed": False, "api_key": "test-key"},
            )
        ]
        for confirm in (False, True):
            app._provider_actions(provider)
            await pilot.pause()
            await choose(2)
            assert app.screen.query_one("#pick-list", ListView).highlighted_child.value is False
            await choose(int(confirm))
            await app.workers.wait_for_complete()
            assert len(sent) == 1 + int(confirm)
        assert sent[-1][1] == {"action": "remove", "confirmed": True}


async def test_replayed_markdown_stays_selectable_and_copies_rendered_text(resident_backend):
    from textual.selection import SELECT_ALL

    from syvrio.app.tui.widgets import ChatReply

    owner, client = resident_backend
    session = owner._sessions.create()
    text = "**合成回复** sample\n\n" + "wrapped words " * 18
    owner._sessions.append(session, {"type": "assistant", "id": "a1", "content": text})
    app = ResidentChatApp(client, session.id)
    async with app.run_test(size=(120, 40)) as pilot:
        reply = app.query_one(ChatReply)
        await pilot.mouse_down(reply, offset=(0, 0))
        await pilot.hover(reply, offset=(7, 0))
        await pilot.mouse_up(reply, offset=(7, 0))
        await pilot.pause()
        assert app.screen.get_selected_text() == "合成回复 "
        await pilot.press("ctrl+c")
        assert app.clipboard == "合成回复 "
        wide = reply.get_selection(SELECT_ALL)[0]
        await pilot.resize_terminal(80, 40)
        await pilot.pause()
        narrow = reply.get_selection(SELECT_ALL)[0]
        assert narrow.count("wrapped") == 18 and "**" not in narrow
        assert len(narrow.splitlines()) > len(wide.splitlines())
        assert not reply.query("*")


async def test_context_command_validates_and_updates_without_starting_chat(
    resident_backend, monkeypatch
):
    owner, client = resident_backend
    notices = []
    app = ResidentChatApp(client)
    monkeypatch.setattr(app, "notify", lambda message, **_: notices.append(str(message)))
    async with app.run_test() as pilot:
        editor = app.query_one("#input", Input)
        for value in ("①", "16x", "400000"):
            editor.value = f"/context {value}"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
        assert len(notices) >= 2 and all("context" in msg.lower() for msg in notices[:2])
        assert owner._service.config.chat.context_window == 400_000
        assert not app.sid and not app.query(".bubble")
