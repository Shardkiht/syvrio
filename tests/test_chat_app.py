from __future__ import annotations

import json
import subprocess
import threading
from datetime import date

import pytest

pytestmark = pytest.mark.ui

from textual.geometry import Offset
from textual.widgets import Button, Input, Static

from syvrio.agent.runtime import AgentRuntime
from syvrio.agent.session import SessionStore, new_message_id
from syvrio.app.chat_app import ChatApp, SidebarToggle
from syvrio.app.chat_input import BarInput
from syvrio.app.command_catalog import SLASH_COMMANDS
from syvrio.app.git_screen import GitConfirmScreen, GitScreen
from syvrio.infra.config import ChatConfig, Config, GitConfig, LLMConfig, RetrievalConfig, SearchConfig
from syvrio.infra.tokens import format_tokens
from syvrio.llm import AssistantTurn
from syvrio.memory.extractor import StaticExtractor
from syvrio.memory.models import ExtractionResult
from syvrio.memory.service import MemoryService


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


class FakeSummarizer:
    def summarize(self, previous, transcript, max_chars=1200):
        return "会话摘要"


async def test_inference_review_uses_worker_and_keeps_facts_separate(tmp_path):
    import asyncio
    from syvrio.app.inference_screen import InferenceScreen

    app = build_app(tmp_path, [])
    source = app.service.note("合成阅读偏好")
    item = app.service.propose_inference("可能喜欢安静活动", [source.id])
    async with app.run_test() as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        app._command("/inferences")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert isinstance(app.screen, InferenceScreen)
        assert not app.busy
        await pilot.press("escape")
        app._run_inference_review(item.id, "confirm")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert app.service.list_inferences()[0].status == "confirmed"
        assert len(app.service.store.entries()) == 1
        assert not app.busy


async def _open_inferences(app, pilot):
    import asyncio
    from syvrio.app.inference_screen import InferenceScreen

    await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
    app._command("/inferences")
    await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
    await pilot.pause()
    assert isinstance(app.screen, InferenceScreen)
    return app.screen


async def _finish_inference_worker(app, pilot):
    import asyncio

    await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
    await pilot.pause()


async def test_inference_screen_empty_state(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _open_inferences(app, pilot)
        assert screen.query_one("#inference-empty").display
        assert not screen.query_one("#inference-body").display
        assert not screen.query_one("#inference-actions").display
        assert not screen.query_one("#inference-filters").display
        assert "No inferences yet" in str(screen.query_one("#inference-empty").render())
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, type(screen))


async def test_inference_screen_single_pending_confirm_refreshes_in_place(tmp_path):
    from syvrio.app.inference_screen import InferenceScreen

    app = build_app(tmp_path, [])
    source = app.service.note("合成事实：喜欢安静活动")
    item = app.service.propose_inference("可能喜欢安静活动", [source.id])
    async with app.run_test(size=(120, 36)) as pilot:
        screen = await _open_inferences(app, pilot)
        assert screen._filter == "hypothesis"
        assert "Pending" in str(screen.query_one("#inference-detail-status").render())
        assert "confirmation never writes facts" in str(screen.query_one("#inference-detail-sources").render())
        assert str(screen.query_one("#inference-detail-text").render()) == item.text
        assert "合成事实：喜欢安静活动" in str(screen.query_one("#inference-detail-sources").render())
        assert not screen.query_one("#inference-confirm", Button).disabled

        await pilot.press("c")
        await _finish_inference_worker(app, pilot)
        assert app.screen is screen  # 原界面内刷新，不叠加弹窗
        assert not app.query(".card")  # 不再向聊天区插入详情卡片
        assert app.service.list_inferences()[0].status == "confirmed"
        assert len(app.service.store.entries()) == 1  # 确认不写入事实
        assert not app.busy
        assert screen._filter == "hypothesis"
        assert screen._current() is None
        assert "Pending 0" in str(screen.query_one("#inference-filters").render())
        assert "Confirmed 1" in str(screen.query_one("#inference-filters").render())
        assert "Confirmed. 0 pending remaining." in str(screen.query_one("#inference-message").render())


async def test_inference_screen_edit_labels_and_shortcut_letters_in_editor(tmp_path):
    app = build_app(tmp_path, [])
    source = app.service.note("合成事实")
    app.service.propose_inference("可能喜欢安静活动", [source.id])
    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _open_inferences(app, pilot)
        assert str(screen.query_one("#inference-correct", Button).label) == "Edit"
        assert str(screen.query_one("#inference-hint").render()) == "C Confirm · R Reject · E Edit · Esc Close"
        await pilot.press("e")
        await pilot.pause()
        editor = screen.query_one("#inference-editor")
        editor.load_text("")
        await pilot.press("c", "r", "e")
        assert editor.text == "cre"
        assert screen._editing
        assert str(screen.query_one("#inference-hint").render()) == "Ctrl+S Save · Esc Cancel"


async def test_inference_sources_use_one_fact_scan(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    sources = [app.service.note(f"合成来源 {index}") for index in range(3)]
    for index in range(6):
        app.service.propose_inference(f"合成推断 {index}", [sources[index % 3].id])
    items = app.service.list_inferences()
    monkeypatch.setattr(app.service, "list_inferences", lambda: items)
    calls = []
    entries = app.service.store.entries

    def counted_entries(*args, **kwargs):
        calls.append(kwargs)
        return entries(*args, **kwargs)

    monkeypatch.setattr(app.service.store, "entries", counted_entries)
    monkeypatch.setattr(app.service.store, "find", lambda *_: pytest.fail("来源不得逐个 find 全库扫描"))
    async with app.run_test(size=(100, 30)) as pilot:
        await app.workers.wait_for_complete()
        before = len(calls)
        screen = await _open_inferences(app, pilot)
        assert len(calls) - before == 1
        assert calls[-1] == {"include_inactive": True}
        assert "合成来源 0" in str(screen.query_one("#inference-detail-sources").render())


async def test_inference_edit_failure_preserves_draft_and_retries(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    source = app.service.note("合成事实")
    item = app.service.propose_inference("原始假设", [source.id])
    original_review = app.service.review_inference

    def fail(*args, **kwargs):
        raise ValueError("合成保存失败")

    monkeypatch.setattr(app.service, "review_inference", fail)
    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _open_inferences(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        revised = "修改后的完整草稿\nSecond line with C R E letters."
        editor = screen.query_one("#inference-editor")
        editor.load_text(revised)
        await pilot.press("ctrl+s")
        await _finish_inference_worker(app, pilot)
        assert screen._editing and editor.display
        assert editor.text == revised
        assert editor.has_focus
        assert "合成保存失败" in str(screen.query_one("#inference-message").render())
        assert not screen.query_one("#inference-save", Button).disabled

        monkeypatch.setattr(app.service, "review_inference", original_review)
        await pilot.press("ctrl+s")
        await _finish_inference_worker(app, pilot)
        assert not screen._editing
        assert screen._current().id != item.id
        assert screen._current().text == revised
        assert "Edit saved as a new pending hypothesis." in str(screen.query_one("#inference-message").render())


async def test_inference_screen_filters_and_readonly_states(tmp_path):
    app = build_app(tmp_path, [])
    source = app.service.note("合成事实：偏好简洁工具")
    pending = app.service.propose_inference("待审查假设", [source.id])
    confirmed = app.service.propose_inference("已确认假设", [source.id])
    app.service.review_inference(confirmed.id, "confirm")
    rejected = app.service.propose_inference("已拒绝假设", [source.id])
    app.service.review_inference(rejected.id, "reject")
    superseded_source = app.service.note("将被更正的事实")
    superseded = app.service.propose_inference("将失效的假设", [superseded_source.id])
    app.service.correct(superseded_source.id, "更正后的事实")

    async with app.run_test(size=(120, 36)) as pilot:
        screen = await _open_inferences(app, pilot)
        filters = str(screen.query_one("#inference-filters").render())
        for text in ("All 4", "Pending 1", "Confirmed 1", "Rejected 1", "Invalidated 1"):
            assert text in filters

        await pilot.press("right")  # Pending → Confirmed
        await pilot.pause()
        assert screen._filter == "confirmed"
        assert screen._current().id == confirmed.id
        assert screen.query_one("#inference-confirm", Button).disabled
        assert not screen.query_one("#inference-reject", Button).disabled

        await pilot.press("right")  # Confirmed → Rejected
        await pilot.pause()
        assert screen._current().id == rejected.id
        assert all(button.disabled for button in screen.query("#inference-actions Button"))
        assert "Read-only record" in str(screen.query_one("#inference-detail-sources").render())

        await pilot.press("right")  # Rejected → Invalidated
        await pilot.pause()
        assert screen._current().id == superseded.id
        assert all(button.disabled for button in screen.query("#inference-actions Button"))

        await pilot.press("right")  # Invalidated → All
        await pilot.pause()
        assert screen._filter == "all"
        from textual.widgets import OptionList
        listing = screen.query_one("#inference-list", OptionList)
        assert len(listing.options) == 4
        assert pending.id in {option.id for option in listing.options}


async def test_inference_screen_long_text_and_long_source_ids(tmp_path):
    from syvrio.app.inference_screen import InferenceScreen
    from syvrio.memory.models import Inference

    app = build_app(tmp_path, [])
    long_fact = "合成事实：" + "长期项目偏好先验证最小闭环。" * 20
    source = app.service.note(long_fact)
    long_text = "可能在做长期项目时偏好先搭建可验证的最小闭环，再逐步扩展功能；面对不确定需求时倾向先确认边界。" * 4
    app.service.propose_inference(long_text, [source.id])
    async with app.run_test(size=(120, 36)) as pilot:
        screen = await _open_inferences(app, pilot)
        assert str(screen.query_one("#inference-detail-text").render()) == long_text
        sources = str(screen.query_one("#inference-detail-sources").render())
        assert "Sources (1)" in sources
        assert len(sources) < len(long_fact) + 40  # 来源只给摘录，不整段搬运
        assert screen.query_one("#inference-detail-body").region.height <= screen.query_one("#inference-detail").region.height
        assert screen.query_one("#inference-actions").region.bottom <= screen.query_one("#inference-card").region.bottom

        # 长来源 ID 由组件级数据构造：业务接口不允许伪造来源，这里只验证展示规则。
        stamp = "2026-11-05T18:00:00+08:00"
        item = Inference(
            id="inf-synthetic", text="可能偏好简洁界面", kind="profile",
            source_ids=["mem-20990101-000000-abcdef123456"], created_at=stamp, updated_at=stamp,
        )
        app.push_screen(InferenceScreen([item], {}, lambda *args: None))
        await pilot.pause()
        detail = app.screen
        assert isinstance(detail, InferenceScreen)
        sources = str(detail.query_one("#inference-detail-sources").render())
        assert "mem-20990101-000000-abcdef123456" not in sources
        assert "mem-209901…23456" in sources
        assert "Source unavailable" in sources


async def test_inference_screen_reject_and_correct_full_text(tmp_path):
    app = build_app(tmp_path, [])
    source = app.service.note("合成事实：先确认边界")
    item = app.service.propose_inference("可能偏好先确认需求边界", [source.id])
    async with app.run_test(size=(120, 36)) as pilot:
        screen = await _open_inferences(app, pilot)
        await pilot.press("r")
        await _finish_inference_worker(app, pilot)
        assert app.service.list_inferences()[0].status == "rejected"

        screen._set_filter("all")
        await pilot.pause()
        from textual.widgets import OptionList
        listing = screen.query_one("#inference-list", OptionList)
        listing.highlighted = next(index for index, option in enumerate(listing.options) if option.id == item.id)
        await pilot.pause()
        # 已拒绝不可再操作。
        assert all(button.disabled for button in screen.query("#inference-actions Button"))

        editable = app.service.propose_inference("可能偏好分阶段交付", [source.id])
        app._run_inference_review()  # 新推断经 worker 重新加载，界面内刷新
        await _finish_inference_worker(app, pilot)
        screen._set_filter("hypothesis")
        await pilot.pause()
        assert screen._current().id == editable.id
        await pilot.press("e")
        await pilot.pause()
        editor = screen.query_one("#inference-editor")
        assert editor.display and editor.text == editable.text
        revised = editable.text + "；先给最小验证版本，再补充细节。\n第二行保留完整原文，不静默截断。"
        editor.load_text(revised)
        await pilot.press("ctrl+s")
        await _finish_inference_worker(app, pilot)
        records = {record.id: record for record in app.service.list_inferences()}
        assert records[editable.id].status == "superseded"
        current = screen._current()
        assert current is not None and current.id != editable.id
        assert current.status == "hypothesis"
        assert current.text == revised  # 完整多行原文保留
        assert str(screen.query_one("#inference-detail-text").render()) == revised
        assert not screen._editing


async def test_inference_screen_correct_escape_cancels_then_closes(tmp_path):
    from syvrio.app.inference_screen import InferenceScreen

    app = build_app(tmp_path, [])
    source = app.service.note("合成事实")
    item = app.service.propose_inference("可能喜欢安静活动", [source.id])
    async with app.run_test(size=(120, 36)) as pilot:
        screen = await _open_inferences(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        assert screen._editing
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, InferenceScreen)  # 第一次 Esc 只退出编辑
        assert not screen._editing
        assert not screen.query_one("#inference-editor").display
        assert app.service.list_inferences()[0].text == item.text
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, InferenceScreen)
        assert app.query_one("#input", Input).has_focus


async def test_inference_screen_action_failure_keeps_screen_usable(tmp_path, monkeypatch):
    from syvrio.app.inference_screen import InferenceScreen

    app = build_app(tmp_path, [])
    source = app.service.note("合成事实")
    app.service.propose_inference("可能喜欢安静活动", [source.id])

    def fail(*args, **kwargs):
        raise ValueError("合成推断写入失败")

    monkeypatch.setattr(app.service, "review_inference", fail)
    async with app.run_test(size=(120, 36)) as pilot:
        screen = await _open_inferences(app, pilot)
        await pilot.press("c")
        await _finish_inference_worker(app, pilot)
        assert isinstance(app.screen, InferenceScreen)
        assert not app.busy and not screen._busy
        assert "Inference action failed" in str(screen.query_one("#inference-message").render())
        assert "合成推断写入失败" in str(screen.query_one("#inference-message").render())
        assert screen.query_one("#inference-message").has_class("error")
        assert not screen.query_one("#inference-confirm", Button).disabled
        assert not app.query_one("#input", Input).disabled


async def test_inference_screen_list_and_detail_scroll(tmp_path):
    app = build_app(tmp_path, [])
    source = app.service.note("合成事实")
    for index in range(12):
        app.service.propose_inference(f"第{index}条待审查推断。" + "补充说明" * 80, [source.id])
    async with app.run_test(size=(100, 26)) as pilot:
        from textual.widgets import OptionList
        screen = await _open_inferences(app, pilot)
        listing = screen.query_one("#inference-list", OptionList)
        assert listing.option_count == 12
        listing.highlighted = 11
        await pilot.pause()
        listing.scroll_end(animate=False)
        await pilot.pause()
        assert screen._current().text.startswith("第11条")
        assert listing.scroll_y > 0  # 列表内部滚动，不撑破卡片
        detail = screen.query_one("#inference-detail-body")
        detail.scroll_end(animate=False)
        await pilot.pause()
        assert detail.scroll_y > 0  # 长详情独立滚动


async def test_inference_screen_reopen_and_responsive_layout(tmp_path):
    from syvrio.app.inference_screen import InferenceScreen

    for size, layout in (
        ((120, 36), "horizontal"),
        ((100, 30), "horizontal"),
        ((80, 24), "vertical"),
        ((60, 20), "vertical"),
    ):
        app = build_app(tmp_path / f"{size[0]}x{size[1]}", [])
        source = app.service.note("合成事实")
        app.service.propose_inference("可能喜欢安静活动", [source.id])
        async with app.run_test(size=size) as pilot:
            screen = await _open_inferences(app, pilot)
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
            screen = await _open_inferences(app, pilot)
            assert isinstance(app.screen, InferenceScreen)
            await pilot.press("escape")
            await pilot.pause()


async def test_inference_sync_updates_options_in_place(tmp_path):
    from textual.widgets import OptionList

    app = build_app(tmp_path, [])
    source = app.service.note("合成事实")
    entries = [app.service.propose_inference(f"合成假设 {index}", [source.id]) for index in range(5)]
    sources = {source.id: source.text}
    async with app.run_test(size=(120, 36)) as pilot:
        screen = await _open_inferences(app, pilot)
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


async def test_inference_review_error_releases_input(tmp_path, monkeypatch):
    import asyncio

    app = build_app(tmp_path, [])
    def fail():
        raise ValueError("合成损坏日志")
    monkeypatch.setattr(app.service, "list_inferences", fail)
    async with app.run_test() as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        app._command("/inferences")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert not app.busy
        assert not app.query_one("#input", Input).disabled


async def test_dream_background_allows_session_switch_and_cancellation(tmp_path, monkeypatch):
    import asyncio
    import threading
    from syvrio.memory.models import ReflectionProposal

    app = build_app(tmp_path, [])
    source = app.service.note("合成回顾来源", topic="preferences")
    started, release = threading.Event(), threading.Event()
    def model(mode, entries):
        started.set()
        assert release.wait(10)
        return [ReflectionProposal(text="可能偏好合成活动", source_ids=[source.id])]
    monkeypatch.setattr(app.service.reflection, "_model", model)
    async with app.run_test() as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        app._command("/dream 1")
        try:
            assert await asyncio.to_thread(started.wait, 5)
            assert not app.busy
            assert not app.query_one("#input", Input).disabled
            app._command("/new")
            async with asyncio.timeout(5):
                while app.busy:
                    await pilot.pause()
            app._command("/dream cancel")
        finally:
            release.set()
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        assert not app._reflection_pending
        assert not app.service.inferences.all()


async def test_profile_view_does_not_invoke_model(tmp_path, monkeypatch):
    import asyncio
    from syvrio.app.chat_widgets import ProfileScreen

    app = build_app(tmp_path, [])
    app.service.note("合成偏好", topic="preferences")
    app.service.reflection.run("profile", 1, refresh_summary=True,
                               summary_model=lambda payload: "缓存中的画像正文")
    monkeypatch.setattr(app.service.reflection, "_model", lambda *_: pytest.fail("查看不应调用模型"))
    monkeypatch.setattr(app.service.reflection, "profile", lambda: pytest.fail("查看不得全量遍历画像"))
    async with app.run_test() as pilot:
        app._command("/profile")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert not app.busy
        assert isinstance(app.screen, ProfileScreen)
        assert "缓存中的画像正文" in str(app.screen.query_one("#profile-text", Static).content)
        assert str(app.screen.query_one("#profile-title").render()) == "User Profile"
        assert str(app.screen.query_one("#profile-subtitle").render()) == "Personal memory overview"
        assert str(app.screen.query_one("#profile-status").render()) == "Up to date"
        metadata = str(app.screen.query_one("#profile-meta").render())
        assert "Generated " in metadata and "Facts 1" in metadata and "Unique 1" in metadata
        assert str(app.screen.query_one("#profile-hint").render()) == "↑↓ Scroll · PgUp/PgDn Page · Esc Close"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ProfileScreen)
        assert app.query_one("#input", Input).has_focus


async def test_profile_view_shows_empty_state_without_cache(tmp_path):
    import asyncio
    from syvrio.app.chat_widgets import ProfileScreen

    app = build_app(tmp_path, [])
    async with app.run_test() as pilot:
        app._command("/profile")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert isinstance(app.screen, ProfileScreen)
        assert "No profile summary yet" in str(app.screen.query_one("#profile-text", Static).content)
        assert str(app.screen.query_one("#profile-status").render()) == "Not generated"
        assert not app.screen.query_one("#profile-meta").display
        await pilot.press("escape")
        await pilot.pause()


async def test_profile_view_marks_outdated_summary(tmp_path):
    import asyncio
    from syvrio.app.chat_widgets import ProfileScreen

    app = build_app(tmp_path, [])
    app.service.note("合成早期偏好", topic="preferences")
    app.service.reflection.run("profile", 1, refresh_summary=True,
                               summary_model=lambda payload: "旧画像")
    path = app.service.store.topic_path("preferences")
    path.write_text(path.read_text(encoding="utf-8").replace("合成早期偏好", "合成后期偏好"),
                    encoding="utf-8")
    async with app.run_test() as pilot:
        app._command("/profile")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert isinstance(app.screen, ProfileScreen)
        assert "旧画像" in str(app.screen.query_one("#profile-text", Static).content)
        assert "Outdated" in str(app.screen.query_one("#profile-status", Static).content)
        await pilot.press("escape")
        await pilot.pause()


async def test_profile_view_reports_cache_read_failure(tmp_path, monkeypatch):
    import asyncio
    from syvrio.app.chat_widgets import ProfileScreen

    app = build_app(tmp_path, [])
    monkeypatch.setattr(app.service.reflection, "profile_summary", lambda: (_ for _ in ()).throw(OSError("合成缓存故障")))
    async with app.run_test(size=(80, 24)) as pilot:
        app._command("/profile")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert isinstance(app.screen, ProfileScreen)
        assert str(app.screen.query_one("#profile-status").render()) == "Cache unavailable"
        assert "Could not read" in str(app.screen.query_one("#profile-text").content)
        assert not app.screen.query_one("#profile-meta").display
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#input", Input).has_focus


async def test_profile_long_mixed_text_scrolls_and_resizes(tmp_path):
    import asyncio
    from syvrio.app.chat_widgets import ProfileScreen

    app = build_app(tmp_path, [])
    app.service.note("合成偏好", topic="preferences")
    paragraphs = [
        "日常阅读偏好清淡、简洁且可验证的信息组织方式，保留自然段落与适当留白。" * 12,
        "For technical work, prefer small iterations, explicit evidence, Python 3.12 and Textual interfaces." * 8,
        "中英文与数字混排时保持完整语义，例如 120×36、80×24，以及 API、JSON、Markdown 等术语。" * 12,
        "最后一段用于验证滚动到底部后正文仍然完整，标题与底部快捷键提示不会随正文移动。" * 12,
    ]
    summary_text = "\n\n".join(paragraphs)
    app.service.reflection.run("profile", 1, refresh_summary=True, summary_model=lambda payload: "短摘要")
    cache_path = app.service.reflection.summary_path
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    cached["text"] = summary_text
    cache_path.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
    async with app.run_test(size=(120, 36)) as pilot:
        app._command("/profile")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
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
            assert body.region.bottom <= hint.y
            assert hint.bottom <= card.bottom
            body.scroll_end(animate=False)
            await pilot.pause()
            assert body.scroll_y > 0
            assert screen.query_one("#profile-hint").region == hint
            await pilot.press("pageup")
            await pilot.pause()
            assert body.scroll_y >= 0

        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#input", Input).has_focus
        app._command("/profile")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert isinstance(app.screen, ProfileScreen)
        assert str(app.screen.query_one("#profile-text").content) == summary_text


async def test_profile_refresh_updates_cache_in_background(tmp_path, monkeypatch):
    import asyncio

    app = build_app(tmp_path, [])
    app.service.note("合成偏好", topic="preferences")
    monkeypatch.setattr(app.service.reflection, "_summary_model",
                        lambda payload: "刷新后的画像摘要")
    monkeypatch.setattr(app.service.reflection, "_model", lambda *_: [])
    async with app.run_test() as pilot:
        app._command("/profile refresh 1")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert not app.busy
        assert not app.query_one("#input", Input).disabled
        assert app.service.reflection.profile_summary().text == "刷新后的画像摘要"
        cards = [str(card.content) for card in app.query(".card")]
        assert any("Profile summary: updated" in text for text in cards)


async def test_profile_refresh_does_not_block_chat_while_running(tmp_path, monkeypatch):
    import asyncio

    app = build_app(tmp_path, [])
    app.service.note("合成偏好", topic="preferences")
    started, release = threading.Event(), threading.Event()

    def slow_summary(payload):
        started.set()
        assert release.wait(10)
        return "慢速画像摘要"

    monkeypatch.setattr(app.service.reflection, "_summary_model", slow_summary)
    try:
        async with app.run_test() as pilot:
            app._command("/profile refresh 1")
            assert await asyncio.to_thread(started.wait, 5)
            assert not app.busy
            assert not app.query_one("#input", Input).disabled
            app._command("/profile")
            for _ in range(100):
                await pilot.pause()
                if app.screen.query("#profile-text"):
                    break
            # 摘要生成中仍可打开当前缓存视图（此时应显示空状态）。
            assert "No profile summary yet" in str(app.screen.query_one("#profile-text", Static).content)
            await pilot.press("escape")
            release.set()
            await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
    finally:
        release.set()


@pytest.mark.parametrize("apply", [False, True])
async def test_import_command_uses_worker_and_releases_busy(tmp_path, monkeypatch, apply):
    from syvrio.memory import service as module
    from syvrio.memory.models import Candidate

    class Extractor:
        def __init__(self, *args, **kwargs):
            pass

        def extract(self, text):
            return ExtractionResult(memories=[Candidate(text="合成用户偏好茶饮", confidence=0.95)])

    monkeypatch.setattr(module, "ImportExtractor", Extractor)
    path = tmp_path / "synthetic conversation.txt"
    path.write_text("合成聊天记录", encoding="utf-8")
    app = build_app(tmp_path, [])
    async with app.run_test() as pilot:
        app._command(f'/import "{path}"' + (" --apply" if apply else ""))
        if apply:
            await pilot.pause()
            assert "Confirm Memory Import" in str(app.screen.query_one(".pick-title").render())
            assert app.screen.query_one("#pick-list").highlighted_child.value is False
            assert not app.busy and not app.service.store.entries()
            app.screen.query_one("#pick-list").index = 1
            await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        assert not app.busy
        assert bool(app.service.store.entries()) is apply
        assert not app.query_one("#input", Input).disabled


async def test_import_error_releases_busy(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test() as pilot:
        app._command(f'/import "{tmp_path / "missing.pdf"}"')
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        assert not app.busy
        assert any("import failed" in str(card.render()).lower() for card in app.query(".card"))


async def test_import_apply_cancel_starts_no_parsing_or_writes(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    path = tmp_path / "synthetic.txt"
    path.write_text("合成导入内容", encoding="utf-8")
    monkeypatch.setattr(
        "syvrio.application.memory.read_document",
        lambda *_: pytest.fail("取消前不得解析文档"),
    )
    async with app.run_test(size=(80, 24)) as pilot:
        app._command(f'/import "{path}" --apply')
        await pilot.pause()
        assert "Confirm Memory Import" in str(app.screen.query_one(".pick-title").render())
        assert str(path) in str(app.screen.query_one("#pick-detail-text").content)
        assert app.screen.query_one("#pick-list").highlighted_child.value is False
        for width, height in ((120, 36), (100, 30), (80, 24), (60, 20)):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            card = app.screen.query_one("#pick-card").region
            assert card.width <= width and card.height <= height
            assert app.screen.query_one("#pick-list").region.bottom <= card.bottom
            hint_widget = app.screen.query_one(".pick-hint")
            assert hint_widget.region.height >= 1 and hint_widget.region.bottom <= card.bottom
            if width == 60:
                assert str(hint_widget.render()) == "PgUp/PgDn Read · Enter Confirm · Esc Cancel"
                assert hint_widget.region.height == 1
        await pilot.press("enter")
        await pilot.pause()
        assert not app.busy and not app.service.store.entries()
        assert app.query_one("#input", Input).has_focus
        assert any("Import cancelled" in str(card.render()) for card in app.query(".card"))

        app._command(f'/import "{path}" --apply')
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not app.busy and not app.service.store.entries()


async def test_import_apply_confirms_once_for_multiple_files(tmp_path, monkeypatch):
    import asyncio
    from syvrio.memory import service as module
    from syvrio.memory.models import Candidate

    extracted = []

    class Extractor:
        def __init__(self, *args, **kwargs):
            pass

        def extract(self, text):
            extracted.append(text)
            return ExtractionResult(memories=[Candidate(text=text, confidence=0.95)])

    monkeypatch.setattr(module, "ImportExtractor", Extractor)
    source = tmp_path / "batch"
    source.mkdir()
    (source / "one.txt").write_text("合成第一条记忆", encoding="utf-8")
    (source / "two.txt").write_text("合成第二条记忆", encoding="utf-8")
    app = build_app(tmp_path, [])
    async with app.run_test(size=(100, 30)) as pilot:
        app._command(f'/import "{source}" --apply')
        await pilot.pause()
        assert "Confirm Memory Import" in str(app.screen.query_one(".pick-title").render())
        app.screen.query_one("#pick-list").index = 1
        await pilot.press("enter")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert len(extracted) == 2
        assert {entry.text for entry in app.service.store.entries()} == {"合成第一条记忆", "合成第二条记忆"}
        assert not app.busy
        assert "PickScreen" not in type(app.screen).__name__


def build_app(tmp_path, turns, extract_on_exit=False, search=None, git=None):
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        retrieval=RetrievalConfig(method="keyword"),
        search=search or SearchConfig(auto_start=False),
        chat=ChatConfig(context_window=32000, auto_recall=3, recall_rewrite=False, extract_on_exit=extract_on_exit),
        git=git or GitConfig(auto_sync=False),
    )
    service = MemoryService(config, extractor=StaticExtractor(ExtractionResult(memories=[])))
    service.init_vault()
    runtime = AgentRuntime(
        service,
        FakeChatModel(turns),
        SessionStore(config.vault),
        summarizer=FakeSummarizer(),
        config=config.chat,
    )
    runtime.start()
    return ChatApp(runtime, service, runtime.sessions)


async def _wait_reply(app, pilot, limit=200):
    # press 后消息处理是异步的：必须等回复真正挂载，不能只看 busy 标志。
    for _ in range(limit):
        await pilot.pause()
        # 回复块在流式中途就挂载，侧栏刷新发生在收尾阶段；必须等 busy 释放再断言。
        if app.query(".reply") and not app.busy:
            return
    raise AssertionError("等待回复超时")


async def _wait_context_usage(app, pilot):
    for _ in range(100):
        await pilot.pause()
        if "Calculating" not in str(app.query_one("#status", Static).content):
            return
    raise AssertionError("等待后台上下文统计超时")


async def test_chat_roundtrip(tmp_path):
    app = build_app(tmp_path, [AssistantTurn(content="收到，已了解。")])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "你好，记住我喜欢喝茶"
        await pilot.press("enter")
        await _wait_reply(app, pilot)

        assert app.query(".bubble")  # 用户气泡已渲染
        assert app.query(".reply")  # 助手 Markdown 回复已渲染
        assert app.runtime.has_session
        assert app.query("SessionItem")  # 侧栏出现当前会话
        assert "Context" in str(app.query_one("#status", Static).content)


async def test_undo_restores_last_message_for_editing(tmp_path):
    app = build_app(tmp_path, [AssistantTurn(content="旧回答"), AssistantTurn(content="新回答")])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "原始提问"
        await pilot.press("enter")
        await _wait_reply(app, pilot)

        widget.value = "/undo"
        await pilot.press("enter")
        await pilot.pause()

        assert widget.value == "原始提问"
        assert widget.cursor_position == len("原始提问")
        assert not app.busy
        assert not app.query(".bubble")
        assert app.runtime.current.user_texts() == []
        assert any(record.get("type") == "undo" for record in app.runtime.current.records)

        widget.value = "编辑后的提问"
        await pilot.press("enter")
        await _wait_reply(app, pilot)

        assert app.runtime.current.user_texts() == ["编辑后的提问"]
        assert [getattr(reply, "_markdown", "") for reply in app.query(".reply")] == ["新回答"]
        raw_texts = [
            record["content"]
            for record in app.runtime.current.records
            if record.get("type") == "message" and record.get("role") == "user"
        ]
        assert raw_texts == ["原始提问", "编辑后的提问"]

@pytest.mark.parametrize("action", ["new", "resume"])
async def test_session_switch_drops_previous_context_usage(tmp_path, action):
    app = build_app(tmp_path, [AssistantTurn(content="合成回复")])
    target = app.sessions.create()
    app.sessions.append(target, {
        "type": "message", "id": new_message_id(), "role": "user", "content": "历史合成提问",
    })
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "新合成提问"
        await pilot.press("enter")
        await _wait_reply(app, pilot)
        assert app.runtime.last_plan is not None
        if action == "new":
            app._new_session()
        else:
            app._switch_session(target.id)
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        assert not app.busy
        assert app.runtime.last_plan is None
        await _wait_context_usage(app, pilot)
        status = str(app.query_one("#status", Static).content)
        if action == "new":
            assert "0/32K" in status
        else:
            # 恢复会话后不能沿用上一个会话的用量，但要显示目标会话自身的占用。
            assert app.runtime.context_usage() > 0
            assert f"{format_tokens(app.runtime.context_usage())}/32K" in status
        # 重建 transcript 后焦点恢复可能晚一帧，busy 释放不等于 focus Processed。
        for _ in range(50):
            await pilot.pause()
            if widget.has_focus:
                break
        assert widget.has_focus, app.focused
        if action == "new":
            assert not app.runtime.has_session
        else:
            assert app.runtime.current.id == target.id


async def test_status_bar_renders_context_usage(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        status = app.query_one("#status", Static)
        rendered = "".join(
            status.render_line(y).text for y in range(status.size.height)
        )

        assert "Context" in rendered
        assert "/32K" in rendered


async def test_resumed_session_shows_its_own_context_usage(tmp_path):
    app = build_app(tmp_path, [])
    target = app.sessions.create()
    app.sessions.append(
        target,
        {
            "type": "message",
            "id": new_message_id(),
            "role": "user",
            "content": "历史合成陈述" * 20,
        },
    )
    app.runtime.resume(target.id)

    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_context_usage(app, pilot)
        assert app.runtime.last_plan is None
        usage = app.runtime.context_usage()
        assert usage > 0
        assert f"{format_tokens(usage)}/32K" in str(
            app.query_one("#status", Static).content
        )


async def test_undo_recomputes_context_usage(tmp_path):
    app = build_app(tmp_path, [AssistantTurn(content="合成回复")])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "待撤销的合成陈述"
        await pilot.press("enter")
        await _wait_reply(app, pilot)
        assert app.runtime.last_plan is not None

        app.runtime.undo()
        app.update_status()
        await _wait_context_usage(app, pilot)

        assert app.runtime.last_plan is None
        usage = app.runtime.context_usage()
        assert usage > 0
        assert f"{format_tokens(usage)}/32K" in str(
            app.query_one("#status", Static).content
        )


async def test_status_context_usage_matches_last_plan(tmp_path):
    app = build_app(tmp_path, [AssistantTurn(content="合成回复")])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "合成提问"
        await pilot.press("enter")
        await _wait_reply(app, pilot)

        plan = app.runtime.last_plan
        assert plan is not None
        assert f"{format_tokens(plan.estimated_tokens)}/32K" in str(
            app.query_one("#status", Static).content
        )


async def test_history_pages_preserve_records_and_flat_reply_widgets(tmp_path):
    from syvrio.app.chat_widgets import ChatReply, HistoryMore, TranscriptView

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    for index in range(14):
        app.sessions.append(session, {"type": "message", "id": f"u{index}", "role": "user", "content": f"合成提问{index}"})
        app.sessions.append(session, {"type": "assistant", "id": f"a{index}", "content": f"# 合成回复{index}\n\n" + "- 合成条目\n" * 30})
    original = session.path.read_bytes()
    app.runtime.resume(session.id)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert len(app.query(ChatReply)) == TranscriptView.PAGE_SIZE // 2
        assert "合成提问0" not in str([widget.content for widget in app.query(".bubble")])
        while app.query(HistoryMore):
            app.query_one(HistoryMore).action_load()
            await pilot.pause()
        assert [str(widget.content) for widget in app.query(".bubble")] == [f"合成提问{i}" for i in range(14)]
        assert len(app.query(ChatReply)) == 14
        assert all(len(reply.query("*")) == 0 for reply in app.query(ChatReply))
    assert session.path.read_bytes() == original


@pytest.mark.parametrize("restored", [False, True])
async def test_assistant_markdown_can_be_selected_and_copied(tmp_path, restored):
    from syvrio.app.chat_widgets import ChatReply

    app = build_app(tmp_path, [AssistantTurn(content="**合成回复** sample")])
    if restored:
        session = app.sessions.create()
        app.sessions.append(session, {"type": "assistant", "content": "**合成回复** sample"})
        app.runtime.resume(session.id)
    async with app.run_test(size=(120, 40)) as pilot:
        if not restored:
            app.query_one("#input", Input).value = "合成提问"
            await pilot.press("enter")
            await _wait_reply(app, pilot)
        reply = app.query_one(ChatReply)
        await pilot.pause()
        await pilot.mouse_down(reply, offset=(0, 0))
        await pilot.hover(reply, offset=(7, 0))
        await pilot.mouse_up(reply, offset=(7, 0))
        await pilot.pause()
        assert app.screen.get_selected_text() == "合成回复 "
        await pilot.press("ctrl+c")
        assert app.clipboard == "合成回复 "
        assert not reply.query("*")


async def test_resume_session_does_not_show_success_notification(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    # 只观察恢复动作；启动补记线程可能稍后发出独立的自动提交通知。
    monkeypatch.setattr(app, "_run_catchup", lambda: None)
    session = app.sessions.create()
    app.sessions.append(session, {"type": "assistant", "content": "合成历史回复"})
    async with app.run_test(size=(120, 40)) as pilot:
        notifications = []
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append(message))
        app._switch_session(session.id)
        await _wait_reply(app, pilot)
        assert app.runtime.current.id == session.id
        assert not notifications
        assert not app.query_one("#input", Input).disabled


async def test_reply_selection_tracks_stream_updates_and_resize(tmp_path):
    from textual.selection import SELECT_ALL
    from syvrio.app.chat_widgets import ChatReply

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        app._stream_append("12:00", "**合成标题**\n\n")
        app._stream_flush()
        await pilot.pause()
        reply = app.query_one(ChatReply)
        assert "合成标题" in reply.get_selection(SELECT_ALL)[0]
        app._stream_append("12:00", "- 合成条目\n\n```python\nprint('sample')\n```\n\n" + "wrapped words " * 12)
        app._stream_flush()
        await pilot.pause()
        wide = reply.get_selection(SELECT_ALL)[0]
        assert "合成条目" in wide and "print('sample')" in wide
        assert "**" not in wide and "```" not in wide
        await pilot.resize_terminal(80, 40)
        await pilot.pause()
        narrow = reply.get_selection(SELECT_ALL)[0]
        assert narrow.count("wrapped") == 12
        assert len(narrow.splitlines()) > len(wide.splitlines())
        reply.text_select_all()
        await pilot.press("ctrl+c")
        assert app.clipboard == narrow


async def test_switch_clears_previous_history_page_state(tmp_path):
    from syvrio.app.chat_widgets import HistoryMore, TranscriptView

    app = build_app(tmp_path, [])
    first, second = app.sessions.create(), app.sessions.create()
    for index in range(22):
        app.sessions.append(first, {"type": "message", "id": f"m{index}", "role": "user", "content": f"旧会话{index}"})
    app.sessions.append(second, {"type": "message", "id": "new", "role": "user", "content": "新会话"})
    app.runtime.resume(first.id)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.query(HistoryMore)
        app._switch_session(second.id)
        for _ in range(100):
            await pilot.pause()
            if not app.busy and not app.query(HistoryMore):
                break
        assert not app.query(HistoryMore)
        app.query_one(TranscriptView).load_earlier()
        assert [str(widget.content) for widget in app.query(".bubble")] == ["新会话"]


async def test_context_estimation_uses_worker_cache_and_rejects_stale_results(tmp_path, monkeypatch):
    import asyncio
    import threading

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.append(session, {"type": "message", "id": "m1", "role": "user", "content": "合成陈述"})
    entered, release = threading.Event(), threading.Event()
    calls = []
    main_thread = threading.get_ident()

    def estimate(snapshot, prompt):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return 1234

    monkeypatch.setattr(app.runtime.context, "estimate_usage", estimate)
    async with app.run_test(size=(120, 40)) as pilot:
        app.runtime.resume(session.id)
        try:
            app.update_status()
            assert await asyncio.to_thread(entered.wait, 3)
            for _ in range(4):
                app.update_status()
            assert len(calls) == 1 and calls[0] != main_thread
            assert not app.query_one("#input", Input).disabled
            app.runtime.start()
            app.update_status()
        finally:
            release.set()
        await pilot.pause()
        assert "0/32K" in str(app.query_one("#status", Static).content)
        app.runtime.resume(session.id)
        app.update_status()
        for _ in range(50):
            await pilot.pause()
            if app._usage_request is None:
                break
        assert f"{format_tokens(1234)}/32K" in str(app.query_one("#status", Static).content)
        before = len(calls)
        for _ in range(4):
            app.update_status()
        await pilot.pause()
        assert len(calls) == before
        app.sessions.append(app.runtime.current, {"type": "undo", "message_id": "m1"})
        app.update_status()
        for _ in range(50):
            await pilot.pause()
            if len(calls) == before + 1 and app._usage_request is None:
                break
        assert len(calls) == before + 1


async def test_context_estimation_failure_keeps_input_enabled(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.append(session, {"type": "message", "id": "m1", "role": "user", "content": "合成陈述"})
    app.runtime.resume(session.id)
    calls = []

    def fail(*args):
        calls.append(1)
        raise RuntimeError("合成统计失败")

    monkeypatch.setattr(app.runtime.context, "estimate_usage", fail)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert "Unavailable" in str(app.query_one("#status", Static).content)
        assert not app.query_one("#input", Input).disabled
        app.update_status()
        await pilot.pause()
        assert len(calls) == 1


async def test_slash_help_card(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/help"
        await pilot.press("enter")
        await pilot.pause()

        cards = app.query(".card")
        assert cards
        assert "Commands" in str(cards[-1].content)


async def test_command_palette_filter_navigate_execute(tmp_path):
    from syvrio.app.chat_app import CommandPalette

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/th"  # 触发 Changed -> 面板打开
        await pilot.pause()

        palette = app.query_one("#cmd-palette", CommandPalette)
        assert palette.display
        assert [m[0] for m in palette.matches] == ["think"]
        assert palette.current == "think"

        await pilot.press("enter")  # 执行高亮命令
        await pilot.pause()
        assert not palette.display
        # /think 要么弹出选择器，要么提示模型不支持；两者都说明命令已执行。
        assert app.query(".card") or app.screen.__class__.__name__ in {"PickScreen", "TextInputScreen"}
        assert widget.value == ""


async def test_command_palette_escape_closes(tmp_path):
    from syvrio.app.chat_app import CommandPalette

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/"
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        assert palette.display and len(palette.matches) == len(SLASH_COMMANDS)

        await pilot.press("escape")
        await pilot.pause()
        assert not palette.display


async def test_tab_completes_highlighted_command_without_executing(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    commands = []
    monkeypatch.setattr(app, "_command", commands.append)
    async with app.run_test() as pilot:
        widget = app.query_one("#input", Input)
        await pilot.press("slash", "down", "tab")
        assert widget.value == "/sessions"
        assert widget.cursor_position == len(widget.value)
        assert not commands
        await pilot.press("enter")
        assert commands == ["/sessions"]


@pytest.mark.parametrize(
    ("partial", "completed"),
    [("/profile r", "/profile refresh"), ("/model re", "/model refresh"),
     ("/dream ca", "/dream cancel"), ("/profile refresh 3", "/profile refresh 3"),
     ("/import /tmp/notes", "/import /tmp/notes"), ("ordinary text", "ordinary text")],
)
async def test_tab_completes_subcommands_and_preserves_arguments(tmp_path, partial, completed):
    app = build_app(tmp_path, [])
    async with app.run_test() as pilot:
        widget = app.query_one("#input", Input)
        widget.value = partial
        widget.cursor_position = len(partial)
        await pilot.press("tab")
        assert widget.value == completed
        assert widget.has_focus


async def test_tab_completes_command_then_subcommand(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    commands = []
    monkeypatch.setattr(app, "_command", commands.append)
    async with app.run_test() as pilot:
        widget = app.query_one("#input", Input)
        await pilot.press("slash", "p", "r", "o", "tab")
        assert widget.value == "/profile "
        await pilot.press("tab")
        assert widget.value == "/profile refresh"
        assert not commands
        await pilot.press("enter")
        assert commands == ["/profile refresh"]


@pytest.mark.parametrize("prefix", ["/", "/rename"])
async def test_command_palette_sits_above_input(tmp_path, prefix):
    from syvrio.app.chat_app import CommandPalette

    app = build_app(tmp_path, [])
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


async def test_command_palette_columns_align(tmp_path):
    from syvrio.app.chat_app import CommandPalette
    from syvrio.app.chat_widgets import CommandRow

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#input", Input).value = "/"
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        rows = list(palette.query(CommandRow))
        assert len(rows) == len(SLASH_COMMANDS)
        assert rows[0].has_class("cmd-active")

        starts = set()
        for row, (_, usage, description) in zip(rows, SLASH_COMMANDS):
            plain = row.render().plain
            # [refresh] 等方括号必须原样可见，不能被当成标记吞掉
            assert usage in plain
            starts.add(plain.index(description))
        assert len(starts) == 1


async def test_command_palette_scrolls_highlighted_command_into_view(tmp_path):
    from syvrio.app.chat_app import CommandPalette

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#input", Input).value = "/"
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        assert palette.listing.max_scroll_y > 0

        for _ in range(len(SLASH_COMMANDS) - 1):
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


async def test_command_palette_mouse_wheel_scrolls(tmp_path):
    from syvrio.app.chat_app import CommandPalette
    from textual.events import MouseScrollDown

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#input", Input).value = "/"
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        before = palette.listing.scroll_y
        palette.listing.post_message(MouseScrollDown(
            palette.listing, 0, 0, 0, 1, 0, False, False, False))
        await pilot.pause()
        assert palette.listing.scroll_y > before


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [("/pro", ["profile"]), ("/dre", ["dream"]), ("/inf", ["inferences"])],
)
async def test_command_palette_prefix_filters(tmp_path, prefix, expected):
    from syvrio.app.chat_app import CommandPalette

    app = build_app(tmp_path, [])
    async with app.run_test() as pilot:
        app.query_one("#input", Input).value = prefix
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        assert palette.display
        assert [match[0] for match in palette.matches] == expected


async def test_command_palette_click_runs_visible_command(tmp_path):
    import asyncio
    from syvrio.app.chat_app import CommandPalette

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#input", Input).value = "/hel"
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        await pilot.click(palette.rows()[0])
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert not palette.display
        assert any("Commands" in str(card.content) for card in app.query(".card"))


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_command_palette_fits_small_viewport_and_input_stays_visible(tmp_path, size):
    from syvrio.app.chat_app import CommandPalette

    app = build_app(tmp_path, [])
    async with app.run_test(size=size) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/"
        await pilot.pause()
        palette = app.query_one("#cmd-palette", CommandPalette)
        assert palette.display
        assert palette.region.height <= 16
        assert palette.region.bottom <= widget.region.y
        assert widget.region.height > 0

        for _ in range(len(SLASH_COMMANDS) - 1):
            await pilot.press("down")
        await pilot.pause()
        last = palette.rows()[-1]
        assert last.region.y >= palette.listing.region.y
        assert last.region.bottom <= palette.listing.region.bottom


async def test_rename_command_prompts_for_title(tmp_path):
    from syvrio.app.chat_widgets import TextInputScreen

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.rename(session, "旧标题")
    app.runtime.resume(session.id)
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
            if app.runtime.current.title() == "新标题":
                break
        assert app.runtime.current.title() == "新标题"
        assert app.sessions.load(session.id).title() == "新标题"


async def test_model_picker_can_configure_provider_key(tmp_path, monkeypatch):
    import sys
    import types

    from textual.widgets import Label, ListView

    from syvrio.app.chat_widgets import PickScreen

    keys: dict[str, str] = {}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service_name, name: keys.get(name)
    fake.set_password = lambda service_name, name, value: keys.__setitem__(name, value)
    monkeypatch.setitem(sys.modules, "keyring", fake)

    app = build_app(tmp_path, [])
    app.service.config_path = tmp_path / "syvrio.toml"
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/model"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            # 等模态挂载完成并拿到焦点，再改高亮；否则 on_mount 可能重置 index。
            if isinstance(app.screen, PickScreen) and app.screen.query("#pick-list"):
                if app.screen.query_one("#pick-search", Input).has_focus:
                    break

        model_picker = app.screen
        listing = model_picker.query_one("#pick-list", ListView)
        labels = [str(item.query_one(Label).render()) for item in listing.children]
        assert labels
        assert all(not item.value[0].startswith("__") for item in listing.children)
        assert "Ctrl+P" in str(model_picker.query_one(".pick-hint").render())
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert "Providers" in str(app.screen.query_one(".pick-title").render())
        listing = app.screen.query_one("#pick-list", ListView)
        assert [item.value for item in listing.children] == ["add", "update", "remove"]
        listing.index = 0
        listing.focus()
        await pilot.press("enter")
        # 第一个选择器 dismiss 与下一个 push 不在同一帧，等新屏幕出现再查询。
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, PickScreen) and app.screen is not model_picker:
                if app.screen.query(".pick-title"):
                    break
        assert "Add provider" in str(app.screen.query_one(".pick-title").render())

        providers = app.screen.query_one("#pick-list", ListView)
        labels = [str(item.query_one(Label).render()) for item in providers.children]
        providers.index = next(i for i, label in enumerate(labels) if label.startswith("SiliconFlow"))
        providers.focus()
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            if app.screen.__class__.__name__ == "TextInputScreen":
                break

        key_input = app.screen.query_one("#pick-input", Input)
        assert key_input.password
        key_input.value = "sk-test"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            if app.service.config.llm.provider == "siliconflow" and not app.busy:
                break

        assert keys["SILICONFLOW_API_KEY"] == "sk-test"
        assert app.service.config.llm.model == "deepseek-ai/DeepSeek-V3"
        assert not app.busy


async def test_model_picker_can_remove_provider_key(tmp_path, monkeypatch):
    import sys
    import types

    from textual.widgets import Label, ListView

    from syvrio.app.chat_widgets import PickScreen

    keys = {"SILICONFLOW_API_KEY": "sk-test"}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service_name, name: keys.get(name)
    fake.delete_password = lambda service_name, name: keys.pop(name, None)
    monkeypatch.setitem(sys.modules, "keyring", fake)

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._show_model_picker(app._model_entries())
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        listing = app.screen.query_one("#pick-list", ListView)
        listing.index = 2
        listing.focus()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, PickScreen) and "Remove provider" in str(
                app.screen.query_one(".pick-title").render()
            ):
                break

        providers = app.screen.query_one("#pick-list", ListView)
        labels = [str(item.query_one(Label).render()) for item in providers.children]
        providers.index = next(i for i, label in enumerate(labels) if label.startswith("SiliconFlow"))
        providers.focus()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, PickScreen) and "Remove SiliconFlow provider?" in str(
                app.screen.query_one(".pick-title").render()
            ):
                break

        confirm = app.screen.query_one("#pick-list", ListView)
        assert confirm.highlighted_child.value is False  # 默认 Cancel，Enter 不会误删凭据
        await pilot.press("escape")
        await pilot.pause()
        assert keys["SILICONFLOW_API_KEY"] == "sk-test"

        app._provider_selected("remove", ("siliconflow", "SiliconFlow", True))
        await pilot.pause()
        confirm = app.screen.query_one("#pick-list", ListView)
        confirm.index = 1
        confirm.focus()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if app.query(".card"):
                break

        assert "SILICONFLOW_API_KEY" not in keys
        assert "provider removed" in str(app.query(".card")[-1].render())


async def test_provider_removal_failure_keeps_key_and_ui_usable(tmp_path, monkeypatch):
    import sys
    import types

    keys = {"SILICONFLOW_API_KEY": "sk-test"}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service_name, name: keys.get(name)

    def fail_delete(service_name, name):
        raise OSError("synthetic keyring failure")

    fake.delete_password = fail_delete
    monkeypatch.setitem(sys.modules, "keyring", fake)
    app = build_app(tmp_path, [])
    async with app.run_test(size=(80, 24)) as pilot:
        app._remove_provider_result("siliconflow", True)
        await pilot.pause()
        assert keys["SILICONFLOW_API_KEY"] == "sk-test"
        assert "Could not remove SiliconFlow" in str(app.query(".card")[-1].render())
        assert not app.query_one("#input", Input).disabled
        assert app.query_one("#input", Input).has_focus


async def test_model_picker_provider_menu_returns_to_models(tmp_path, monkeypatch):
    from textual.widgets import ListView
    from syvrio.app.chat_widgets import ModelPickerScreen

    monkeypatch.setattr("syvrio.app.chat_commands.fetch_provider_models", lambda *args, **kwargs: [])
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        app._show_model_picker(app._model_entries())
        await pilot.pause()
        picker = app.screen
        picker.query_one("#pick-search", Input).value = "no-matching-model"
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert not isinstance(app.screen, ModelPickerScreen)
        assert [item.value for item in app.screen.query_one("#pick-list", ListView).children] == ["add", "update", "remove"]
        app._apply_fetched_models(picker, [("late-model", ("deepseek", "late"), False)])
        assert [item.value for item in app.screen.query_one("#pick-list", ListView).children] == ["add", "update", "remove"]
        await pilot.press("escape")
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, ModelPickerScreen) and app.screen.query("#pick-search"):
                break
        assert isinstance(app.screen, ModelPickerScreen)
        assert not app.busy


@pytest.mark.parametrize("shortcut,sentinel", [("ctrl+r", "__refresh__"), ("ctrl+n", "__custom__")])
async def test_model_picker_shortcuts_work_from_search(tmp_path, monkeypatch, shortcut, sentinel):
    selected = []
    app = build_app(tmp_path, [])
    monkeypatch.setattr(app, "_model_picked", selected.append)
    async with app.run_test(size=(120, 40)) as pilot:
        app._show_model_picker(app._model_entries())
        await pilot.pause()
        assert app.screen.query_one("#pick-search", Input).has_focus
        await pilot.press(shortcut)
        await pilot.pause()
        assert selected == [(sentinel, "")]

@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_model_picker_keeps_shortcut_hint_visible(tmp_path, size):
    app = build_app(tmp_path, [])
    entries = [(f"model-{i}", ("deepseek", f"model-{i}"), i == 0) for i in range(50)]
    async with app.run_test(size=size) as pilot:
        app._show_model_picker(entries)
        await pilot.pause()
        hint = app.screen.query_one(".pick-hint", Static)
        card = app.screen.query_one("#pick-card")
        assert hint.region.bottom <= card.content_region.bottom
        assert hint.region.height > 0
        assert "Ctrl+P" in str(hint.render())
        assert card.region.bottom <= app.screen.region.bottom


async def test_ctrl_p_outside_model_picker_keeps_command_palette(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "CommandPalette"


async def test_model_picker_filters_by_search(tmp_path):
    import asyncio
    from textual.widgets import ListView

    from syvrio.app.chat_widgets import PickScreen

    app = build_app(tmp_path, [])
    entries = [
        ("DeepSeek · DeepSeek Flash", ("deepseek", "deepseek-flash"), True),
        ("SiliconFlow · DeepSeek V3", ("siliconflow", "deepseek-ai/DeepSeek-V3"), False),
        ("Spark · Spark X2.6", ("spark", "spark-x2.6"), False),
    ]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._show_model_picker(entries)
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, PickScreen)
        search = picker.query_one("#pick-search", Input)
        assert search.has_focus

        search.value = "silicon"
        # ListView 的移除与挂载异步完成，不能假设一帧后已经得到最终列表。
        async with asyncio.timeout(5):
            while [item.label for item in picker.query("#pick-list OptionItem")] != ["SiliconFlow · DeepSeek V3"]:
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


async def test_model_picker_uses_cache_and_refreshes_in_background(tmp_path, monkeypatch):
    import threading

    from syvrio.app.chat_widgets import PickScreen

    release = threading.Event()

    def fake_fetch(config, refresh=False):
        release.wait(5)
        return ["fresh-model"]

    monkeypatch.setattr(
        "syvrio.app.chat_commands.cached_provider_models",
        lambda config: ["cached-model"],
    )
    monkeypatch.setattr(
        "syvrio.app.chat_commands.fetch_provider_models", fake_fetch
    )

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._open_models()
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, PickScreen):
                break

        picker = app.screen
        assert isinstance(picker, PickScreen)
        assert not app.busy
        for _ in range(50):
            await pilot.pause()
            labels = [item.label for item in picker.query("#pick-list OptionItem")]
            if labels:
                break
        assert any("cached-model" in label for label in labels)
        assert not any("fresh-model" in label for label in labels)

        release.set()
        for _ in range(100):
            await pilot.pause()
            labels = [item.label for item in picker.query("#pick-list OptionItem")]
            if any("fresh-model" in label for label in labels):
                break
        assert any("fresh-model" in label for label in labels)


async def test_model_picker_preserves_highlight_during_background_refresh(tmp_path):
    from textual.widgets import ListView

    app = build_app(tmp_path, [])
    entries = [(f"model-{i}", ("deepseek", f"model-{i}"), i == 0) for i in range(12)]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._show_model_picker(entries)
        await pilot.pause()
        picker = app.screen
        listing = picker.query_one("#pick-list", ListView)
        search = picker.query_one("#pick-search", Input)
        await pilot.press("down")
        await pilot.pause()
        assert search.has_focus and listing.index == 1
        listing.index = 5
        await pilot.pause()

        app._apply_fetched_models(picker, entries)
        await pilot.pause()
        assert listing.index == 5
        assert search.has_focus
        assert search.value == ""

        # 线上目录插入新模型导致索引偏移时，仍按原值保持高亮。
        app._apply_fetched_models(
            picker, [("new-model", ("deepseek", "new"), False), *entries]
        )
        await pilot.pause()
        assert listing.highlighted_child.value == ("deepseek", "model-5")
        assert search.has_focus

        # 搜索过滤文本和过滤后的高亮也必须在刷新后保留。
        search.focus()
        search.value = "model-1"
        await pilot.pause()
        assert search.has_focus
        listing.index = 1
        app._apply_fetched_models(picker, [("new-model", ("deepseek", "new"), False), *entries])
        await pilot.pause()
        assert search.value == "model-1"
        assert listing.index == 1


async def test_model_picker_arrow_keys_keep_search_focus(tmp_path):
    from textual.widgets import ListView

    selected = []
    app = build_app(tmp_path, [])
    entries = [(f"model-{i}", ("deepseek", f"model-{i}"), i == 0) for i in range(6)]
    async with app.run_test(size=(120, 40)) as pilot:
        original = app._model_picked
        app._model_picked = selected.append
        try:
            await pilot.pause()
            app._show_model_picker(entries)
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
            app._model_picked = original


async def test_model_picker_opens_quickly_before_metadata_enrichment(tmp_path, monkeypatch):
    import threading

    from syvrio.app.actions import ModelOption
    from syvrio.app.chat_widgets import ModelPickerScreen

    release = threading.Event()
    calls: list[bool] = []

    def fake_options(self, fetched=None, capabilities=True):
        calls.append(capabilities)
        label = "DeepSeek · Model (128K)" if capabilities else "DeepSeek · Model"
        return [ModelOption("deepseek", "model", label, True)]

    monkeypatch.setattr("syvrio.application.settings.SettingsUseCases.model_options", fake_options)
    monkeypatch.setattr("syvrio.app.chat_commands.cached_provider_models", lambda config: None)

    def fake_fetch(config, refresh=False):
        release.wait(5)
        return []

    monkeypatch.setattr("syvrio.app.chat_commands.fetch_provider_models", fake_fetch)
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._open_models()
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, ModelPickerScreen):
                break
        picker = app.screen
        assert isinstance(picker, ModelPickerScreen)
        for _ in range(50):
            await pilot.pause()
            labels = [item.label for item in picker.query("#pick-list OptionItem")]
            if labels:
                break
        assert labels == ["DeepSeek · Model"]
        assert calls[0] is False

        release.set()
        for _ in range(200):
            await pilot.pause()
            labels = [item.label for item in picker.query("#pick-list OptionItem")]
            if labels == ["DeepSeek · Model (128K)"]:
                break
        assert labels == ["DeepSeek · Model (128K)"]
        assert True in calls


async def test_context_picker_builds_options_off_ui_thread(tmp_path, monkeypatch):
    import threading

    from syvrio.app.chat_widgets import PickScreen

    started = threading.Event()
    release = threading.Event()

    def slow_options(self):
        started.set()
        release.wait(5)
        return [8_000, 16_000]

    monkeypatch.setattr("syvrio.application.settings.SettingsUseCases.context_options", slow_options)
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        ticks: list[int] = []
        app.set_interval(0.02, lambda: ticks.append(1))
        app._command("/context")
        for _ in range(100):
            if started.is_set():
                break
            await pilot.pause()
        assert started.is_set()
        before = len(ticks)
        for _ in range(50):
            await pilot.pause()
            if len(ticks) > before:
                break
        assert len(ticks) > before  # 元数据等待期间事件循环仍在响应
        assert not isinstance(app.screen, PickScreen)
        assert not app.query_one("#input", Input).disabled

        app._command("/context")  # 加载中重复触发只保留一个等待
        release.set()
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, PickScreen):
                break
        assert isinstance(app.screen, PickScreen)
        labels = [item.label for item in app.screen.query("#pick-list OptionItem")]
        assert labels[:2] == ["8K", "16K"]
        assert labels[-1] == "Custom…"


async def test_context_picker_failure_reports_and_recovers(tmp_path, monkeypatch):
    from syvrio.app.chat_widgets import PickScreen

    monkeypatch.setattr(
        "syvrio.application.settings.SettingsUseCases.context_options",
        lambda self: (_ for _ in ()).throw(RuntimeError("合成元数据失败")),
    )
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._command("/context")
        for _ in range(100):
            await pilot.pause()
            if app.query(".card"):
                break
        assert "Loading context options failed" in str(app.query(".card")[-1].render())
        assert "合成元数据失败" in str(app.query(".card")[-1].render())
        assert app._context_loading is False
        assert not app.query_one("#input", Input).disabled


async def test_ctrl_b_toggles_sidebar(tmp_path):
    from textual.containers import Vertical

    from syvrio.app.chat_app import SidebarToggle

    app = build_app(tmp_path, [])
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


async def test_main_focus_stays_on_chat_input(tmp_path):
    from textual.containers import Vertical
    from textual.widgets import ListView

    app = build_app(tmp_path, [])
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


async def test_clicking_chat_keeps_input_focus(tmp_path):
    from textual.containers import VerticalScroll

    app = build_app(tmp_path, [])
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


async def test_sidebar_rows_align_with_new_session(tmp_path):
    # 回归：New session 与会话列表必须同宽同起点；列表滚动时也不能缩窄。
    app = build_app(tmp_path, [])
    store = app.sessions
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
        rows = [
            item.region
            for item in app.query("#sessions > ListItem")
            if hasattr(item, "row")
        ]
        assert rows
        assert all(row.x == new_session.x and row.width == new_session.width for row in rows)


async def test_sidebar_items_are_compact_and_clickable(tmp_path):
    from syvrio.agent.session import new_message_id
    from syvrio.app.chat_app import SessionItem

    app = build_app(tmp_path, [])
    ids = []
    for title in ("会话甲", "会话乙"):
        session = app.sessions.create()
        app.sessions.rename(session, title)
        app.sessions.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": title},
        )
        ids.append(session.id)
    async with app.run_test(size=(100, 30)) as pilot:
        app.show_sidebar()
        await pilot.pause()
        # 等启动补沉淀（含自动提交）结束再操作列表：补记的 refresh_sessions 会重建
        # 列表控件，点击旧引用会被吞掉。busy 不覆盖后台补记，必须等 worker 收尾。
        for _ in range(200):
            await pilot.pause()
            if not app.busy:
                break
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
            if not app.busy and app.runtime.has_session:
                break
        assert app.runtime.current.id == second.row["id"]
        assert app.query_one("#input", Input).has_focus


async def test_compact_failure_releases_busy(tmp_path):
    # 回归：force_compact 抛异常时输入框必须恢复可用，而不是永久禁用。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        def boom():
            raise RuntimeError("persona broken")

        app.runtime.force_compact = boom
        app._run_compact()
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        assert app.busy is False
        input_widget = app.query_one("#input", Input)
        assert input_widget.disabled is False
        assert "Compaction failed" in str(app.query(".card")[-1].render())


async def test_session_switch_failure_releases_busy(tmp_path):
    # 回归：resume 抛异常（会话文件损坏等）时同样不能卡死输入框。
    app = build_app(tmp_path, [])
    app.runtime.config.extract_on_exit = True
    session = app.runtime.ensure_session()
    app.runtime.sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "合成陈述"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        def boom(session_id, **kwargs):
            raise KeyError("会话不存在：s-xxx")

        app.runtime.resume = boom
        app._switch_session("s-20260101-000000-dead")
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        assert app.busy is False
        assert app.query_one("#input", Input).disabled is False
        assert "Session switch failed" in str(app.query(".card")[-1].render())
        assert app.runtime.current is session
        app.runtime.close()
        assert any(record.get("type") == "distilled" for record in session.records)


async def test_resuming_torn_session_shows_recovery_notice_and_continues(tmp_path):
    app = build_app(tmp_path, [AssistantTurn(content="接续成功")])
    session = app.runtime.ensure_session()
    app.runtime.sessions.append(session, {
        "type": "message", "id": new_message_id(), "role": "user", "content": "合成历史",
    })
    with session.path.open("ab") as handle:
        handle.write(b'{"type":"assistant","content":"')
    original = session.path.read_bytes()
    app.runtime.resume(session.id)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert any("Incomplete session tail" in str(card.render()) for card in app.query(".card"))
        widget = app.query_one("#input", Input)
        widget.value = "接续提问"
        await pilot.press("enter")
        await _wait_reply(app, pilot)
        assert session.path.read_bytes().startswith(original)
        assert app.sessions.load(session.id).user_texts() == ["合成历史", "接续提问"]


async def test_session_switch_updates_sidebar_without_reload(tmp_path, monkeypatch):
    from syvrio.app.chat_app import SessionItem

    app = build_app(tmp_path, [])
    store = app.sessions
    ids = []
    for title in ("会话甲", "会话乙"):
        session = store.create()
        store.rename(session, title)
        store.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": title},
        )
        ids.append(session.id)

    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(100):
            await pilot.pause()
            if len(list(app.query(SessionItem))) >= 2:
                break
        # 启动补记可能在低负载下稍后才触发 refresh_sessions：等 worker 收尾再计数，
        # 否则补记的全量刷新会被误判为切换引起的重载。
        await app.workers.wait_for_complete()
        await pilot.pause()

        calls = []
        original_list = store.list

        def counted_list(*args, **kwargs):
            calls.append(1)
            return original_list(*args, **kwargs)

        monkeypatch.setattr(store, "list", counted_list)
        app._switch_session(ids[0])
        for _ in range(100):
            await pilot.pause()
            if not app.busy and app.runtime.has_session:
                break
        await pilot.pause()

        assert calls == []
        current = [item for item in app.query(SessionItem) if item.has_class("current")]
        assert [item.row["id"] for item in current] == [ids[0]]


async def test_delete_session_uses_captured_row(tmp_path):
    # 回归：确认弹窗期间列表被刷新时，仍删除弹窗捕获的那条会话（不读高亮项）。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from syvrio.agent.session import new_message_id

        # 待删目标是非当前会话；再留一个有消息的会话撑住列表渲染。
        store = app.sessions
        session = store.create()
        store.rename(session, "待删除会话")
        store.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": "待删除会话"},
        )
        survivor = store.create()
        store.rename(survivor, "保留会话")
        store.append(
            survivor,
            {"type": "message", "id": new_message_id(), "role": "user", "content": "保留会话"},
        )
        await pilot.pause()
        app.refresh_sessions()
        row = {"id": session.id, "title": "待删除会话"}

        app.confirm_delete_session(row)
        await pilot.pause()
        # 模态打开期间模拟列表刷新（如后台回复完成后的 refresh_sessions）。
        app.refresh_sessions()
        # 默认第一项是 Cancel；显式选择 Delete permanently 才执行。
        assert "cannot be undone" in str(app.screen.query_one(".pick-title").render())
        app.screen.query_one("#pick-list").index = 1
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            if not store.exists(row["id"]):
                break

        assert not store.exists(session.id)
        assert store.exists(survivor.id)
        from syvrio.app.chat_app import SessionItem

        # 文件删除先于 DOM 异步清理完成；等到旧节点移除、列表收敛后再断言。
        remaining = []
        for _ in range(100):
            await pilot.pause()
            remaining = [item.row["id"] for item in app.query(SessionItem)]
            if remaining == [survivor.id]:
                break
        assert remaining == [survivor.id]


async def test_delete_confirmation_rechecks_current_session(tmp_path):
    # 回归：弹窗期间目标变成当前会话时，确认也不能删除它。
    app = build_app(tmp_path, [])
    store = app.sessions
    session = store.create()
    store.rename(session, "目标会话")
    store.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "内容"},
    )
    row = {"id": session.id, "title": "目标会话"}

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.confirm_delete_session(row)
        await pilot.pause()
        app.runtime.resume(session.id)
        app._delete_session_result(row, True)
        await pilot.pause()

        assert store.exists(session.id)


async def test_session_delete_failure_keeps_file_and_ui_usable(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    session = app.sessions.create()
    row = {"id": session.id, "title": "Synthetic session"}
    notifications = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append(message))
    monkeypatch.setattr(app.sessions, "delete", lambda *_: (_ for _ in ()).throw(OSError("synthetic delete failure")))
    async with app.run_test(size=(80, 24)) as pilot:
        app._delete_session_result(row, True)
        await pilot.pause()
        assert app.sessions.exists(session.id)
        assert any("Session could not be deleted" in message for message in notifications)
        assert "Session deleted." not in notifications
        assert not app.query_one("#input", Input).disabled


async def test_model_picker_drains_pending_session_intent(tmp_path):
    # 回归：模型列表加载期间排队的会话意图，在释放 busy 时必须接续。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._pending_new = True
        app._show_model_picker([])
        await pilot.pause()

        assert app._pending_new is False


async def test_context_custom_rejects_unicode_digits(tmp_path):
    # 回归："①".isdigit() 为 True 但 int() 抛错；现在应给出错误卡片而不是崩进事件循环。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._context_custom("①")
        await pilot.pause()
        assert "positive integer" in str(app.query(".card")[-1].render())
        app._context_custom("16x")
        await pilot.pause()
        assert "positive integer" in str(app.query(".card")[-1].render())


async def test_context_custom_does_not_send_value_as_chat_message(tmp_path):
    from textual.widgets import Label, ListView

    from syvrio.app.chat_widgets import PickScreen, TextInputScreen

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "/context"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, PickScreen):
                break

        listing = app.screen.query_one(ListView)
        labels = [str(item.query_one(Label).render()) for item in listing.children]
        listing.index = labels.index("Custom…")
        listing.focus()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, TextInputScreen):
                break

        field = app.screen.query_one("#pick-input", Input)
        field.value = "400000"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            if not isinstance(app.screen, TextInputScreen) and not app.busy:
                break

        assert app.service.config.chat.context_window == 400_000
        assert app.runtime.has_session is False
        assert not app.query(".bubble")


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


async def test_session_picker_focus_bar_follows_selection(tmp_path):
    # 回归：竖线只标记选中行；普通/当前行保留同宽占位但不显示，切换时位置和行高不变。
    from textual.color import Color
    from textual.widgets import ListView

    from syvrio.app.chat_widgets import SessionItem, SessionPickerScreen

    app = build_app(tmp_path, [])
    store = app.sessions
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
        app.runtime.resume(ids[0])
        app.open_session_picker()
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
async def test_session_picker_rail_stays_aligned_for_long_titles_and_widths(tmp_path, size):
    # 长标题只截断不换行；窄终端和宽终端下竖线列都不抖动、不溢出。
    from textual.widgets import ListView

    from syvrio.app.chat_widgets import SessionPickerScreen

    app = build_app(tmp_path, [])
    store = app.sessions
    titles = ("超长会话标题" * 12 + " https://github.com/example/repository", "短标题", "第三条会话")
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
        app.runtime.resume(ids[0])
        app.open_session_picker()
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
            assert all(row.query_one(".session-title").size.height == 1 for row in picker.query("#picker-list SessionItem"))
            # 列表不横向溢出，底部提示仍完整可见。
            assert all(item.region.right <= listing.region.right for item in picker.query("#picker-list SessionItem"))
            assert footer.region.bottom <= picker.query_one("#pick-card").region.bottom


async def test_session_picker_escape_and_arrow_navigation_with_rail(tmp_path):
    # 竖线改动不影响 ↓ 进入列表、继续 ↓ 切换选中和 ESC 取消。
    from textual.widgets import ListView

    from syvrio.app.chat_widgets import SessionItem, SessionPickerScreen

    app = build_app(tmp_path, [])
    store = app.sessions
    for title in ("甲会话", "乙会话", "丙会话"):
        session = store.create()
        store.rename(session, title)
        store.append(
            session,
            {"type": "message", "id": new_message_id(), "role": "user", "content": title},
        )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.open_session_picker()
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


async def test_startup_commits_dirty_vault_without_crashed_sessions(tmp_path):
    # 回归：没有漏沉淀会话时，启动检查仍应提交未保存的 vault 变更。
    import asyncio

    app = build_app(tmp_path, [])
    app.service.note("启动前未提交的变更", topic="misc")
    async with app.run_test(size=(120, 40)) as pilot:
        # 先等待启动 worker 完成，再验证落盘结果，避免 Git 用时随负载波动。
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert app.busy is False
        assert not app.service.repo.has_changes()


async def test_chat_scrollbar_is_thin(tmp_path):
    from textual.containers import Vertical, VerticalScroll

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        chat = app.query_one("#chat", VerticalScroll)
        assert chat.styles.scrollbar_size_vertical == 1
        # 滚动条要贴着窗口右缘，而不是停在对话区右侧留白里。
        assert chat.region.right == app.query_one("#main", Vertical).region.right
        assert chat.styles.padding.right == 0


async def test_input_hint_uses_border_not_text(tmp_path):
    # 回归：提示词留在文字区会和中文输入法的组字内容重叠。
    app = build_app(tmp_path, [])
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


async def test_input_cursor_is_vertical_bar(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", Input)
        assert isinstance(widget, BarInput)
        # Exercise the visible phase without relying on the blink timer.
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


async def test_auto_start_warms_search_in_background(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "syvrio.application.service.ensure_search_daemon",
        lambda base_url: calls.append(base_url) or "running",
    )
    app = build_app(
        tmp_path,
        [],
        search=SearchConfig(enabled=True, auto_start=True),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(100):
            await pilot.pause()
            if calls:
                break
        assert calls == ["http://127.0.0.1:3000"]
        assert app.busy is False


async def test_new_session_click_queues_while_busy(tmp_path):
    # 回归：导航期间点击新会话不能被静默丢弃；LLM 等待期则应立即切换（另测）。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        app.show_sidebar()
        await pilot.pause()
        app._begin_navigation()
        await pilot.click("#new-session")

        assert app._pending_new is True

        app._end_navigation()
        app._drain_pending_intent()
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        assert app._pending_new is False
        assert app.busy is False


async def test_startup_catchup_shows_card_for_crashed_session(tmp_path):
    # 回归：启动检查发现异常终止的会话时，补记并显示卡片；无欠账时保持沉默。
    import asyncio

    app = build_app(tmp_path, [AssistantTurn(content="好")], extract_on_exit=True)
    crashed = app.sessions.create()
    app.sessions.append(
        crashed,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "中断前说的话"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert app.busy is False
        assert app.query_one("#input", Input).disabled is False
        cards = [str(widget.render()) for widget in app.query(".card")]
        assert any("Consolidated 1 unfinished session(s)" in card for card in cards)
        assert app.runtime.consolidate_pending()["scanned"] == 0  # 已标记，不会重复


async def test_pending_interactive_approve(tmp_path):
    # 回归：/pending 必须能在界面内完成确认闭环（issue #1）。
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from syvrio.memory.models import Candidate, PendingItem

        item = PendingItem.from_candidate(
            Candidate(text="低置信的偏好内容", topic="preferences", confidence=0.5), "chat", "low confidence"
        )
        app.service.queue.add(item)
        app.refresh_pending()

        widget = app.query_one("#input", Input)
        widget.value = "/pending"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if app.screen.__class__.__name__ == "PickScreen":
                break
        await pilot.press("enter")  # 选中第一条 → 动作菜单
        for _ in range(50):
            await pilot.pause()
            if len(app.screen_stack) >= 2 and app.screen.__class__.__name__ == "PickScreen":
                break
        await pilot.press("enter")  # 高亮"确认写入"
        for _ in range(100):
            await pilot.pause()
            if not app.service.pending():
                break
        assert not app.service.pending()
        assert app.service.store.has_text("低置信的偏好内容")
        cards = [str(w.render()) for w in app.query(".card")]
        assert any("Saved" in c for c in cards)


async def test_pending_interactive_edit_approves_only_final_text(tmp_path):
    from textual.widgets import ListView

    from syvrio.app.chat_widgets import PickScreen, TextInputScreen
    from syvrio.memory.models import Candidate, PendingItem

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        item = PendingItem.from_candidate(
            Candidate(text="可能喜欢浅烘", topic="preferences", confidence=0.5),
            "chat", "low confidence",
        )
        app.service.queue.add(item)
        app.refresh_pending()

        widget = app.query_one("#input", Input)
        widget.value = "/pending"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, PickScreen) and app.screen.query_one(ListView).has_focus:
                break
        picker = app.screen
        assert isinstance(picker, PickScreen)
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, PickScreen) and app.screen is not picker:
                if app.screen.query_one(ListView).has_focus:
                    break
        assert isinstance(app.screen, PickScreen) and app.screen is not picker
        await pilot.press("down")
        listing = app.screen.query_one(ListView)
        assert listing.highlighted_child.label == "Edit and approve"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, TextInputScreen):
                if app.screen.query_one("#pick-input", Input).has_focus:
                    break
        assert isinstance(app.screen, TextInputScreen)
        field = app.screen.query_one("#pick-input", Input)
        assert field.value == item.text
        field.value = "喜欢中烘咖啡"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            if not app.service.pending() and widget.has_focus and not app.busy:
                break

        entries = app.service.store.entries(include_inactive=True)
        assert [(entry.text, entry.status, entry.supersedes) for entry in entries] == [
            ("喜欢中烘咖啡", "active", None)
        ]
        assert app.service.pending() == []
        assert app.runtime.has_session is False
        assert not app.query(".bubble")
        assert not app.query(".reply")
        assert not app.busy
        assert widget.value == ""
        assert widget.has_focus
        cards = [str(card.render()) for card in app.query(".card")]
        assert any("Saved" in card and "喜欢中烘咖啡" in card for card in cards)


async def test_topics_command_lists_and_drills(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.service.note("喝咖啡偏好清淡", topic="preferences", title="偏好")
        app._show_topics()
        await pilot.pause()
        cards = [str(w.render()) for w in app.query(".card")]
        assert any("preferences" in c and "Memory topics" in c for c in cards)
        app._show_topics("preferences")
        await pilot.pause()
        cards = [str(w.render()) for w in app.query(".card")]
        assert any("喝咖啡偏好清淡" in c for c in cards)


async def test_streaming_roundtrip_renders_single_reply(tmp_path):
    # 回归（issue #4）：流式增量渲染只挂载一个回复块，完成后不重复。
    app = build_app(tmp_path, [AssistantTurn(content="流式回复全文")])
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "测流式"
        await pilot.press("enter")
        for _ in range(200):
            await pilot.pause()
            if not app.busy:
                break
        assert app._stream_widget is None  # 收尾已清
        assert len(app.query(".reply")) == 1
        assert len(app.query(".meta")) == 1
        session = app.runtime.current
        assert any(
            record.get("type") == "assistant" and record.get("content") == "流式回复全文"
            for record in session.records
        )


async def test_streaming_keeps_text_before_tool_calls(tmp_path):
    # 回归：工具调用前的流式正文不能被最终回复覆盖，事件顺序保持生成顺序。
    from syvrio.llm import ToolCall

    app = build_app(
        tmp_path,
        [
            AssistantTurn(
                content="我先查一下",
                tool_calls=[ToolCall(id="c1", name="recall", arguments={"query": "清淡"})],
            ),
            AssistantTurn(content="最终回答"),
        ],
    )
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "测试工具前文本"
        await pilot.press("enter")
        for _ in range(300):
            await pilot.pause()
            if not app.busy:
                break

        children = list(app._chat().children)
        contents = [getattr(child, "_markdown", "") for child in children]
        first = next(i for i, text in enumerate(contents) if "我先查一下" in text)
        final = next(i for i, text in enumerate(contents) if "最终回答" in text)
        event = next(i for i, child in enumerate(children) if "event-card" in child.classes)
        assert first < event < final


async def test_tool_event_rows_use_single_border_and_assistant_is_syvrio(tmp_path):
    # 事件行由 CSS 左边框画一条竖线，正文里不再拼前缀；助手标签统一为 Syvrio。
    from syvrio.llm import ToolCall

    app = build_app(
        tmp_path,
        [
            AssistantTurn(
                content="",
                tool_calls=[ToolCall(id="c1", name="recall", arguments={"query": "清淡"})],
            ),
            AssistantTurn(content="最终回答"),
        ],
    )
    async with app.run_test(size=(120, 40)) as pilot:
        widget = app.query_one("#input", Input)
        widget.value = "测试事件样式"
        await pilot.press("enter")
        for _ in range(300):
            await pilot.pause()
            if not app.busy:
                break

        events = list(app.query(".event-card"))
        assert events
        assert all("┃" not in str(event.render()) for event in events)
        labels = [str(label.render()) for label in app.query(".who-ai")]
        assert labels and all(label.startswith("Syvrio") for label in labels)


async def test_streaming_error_marks_interruption(tmp_path, monkeypatch):
    # 回归（issue #4）：中途断流保留半截内容并显式标注，不静默吞掉。
    app = build_app(tmp_path, [])
    from syvrio.llm import ChatModelError
    from syvrio.app import chat_flow

    logged = []
    monkeypatch.setattr(chat_flow, "record_event", lambda *args, **kwargs: logged.append(args))

    class BoomModel:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, tools, on_delta=None):
            self.calls += 1
            if on_delta is not None:
                on_delta("生成到一半")
            raise ChatModelError("连接中断")

    app.runtime.model = BoomModel()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        widget = app.query_one("#input", Input)
        widget.value = "测试中断"
        await pilot.press("enter")
        for _ in range(200):
            await pilot.pause()
            if not app.busy:
                break
        assert app.query(".reply")
        cards = [str(w.render()) for w in app.query(".card")]
        assert any("连接中断" in c for c in cards)
        assert app.query_one("#input", Input).disabled is False
        assert any(event[0] == "chat.failed" and isinstance(event[1], ChatModelError)
                   for event in logged)


async def test_reading_diagnostics_records_state_transitions_without_content(tmp_path, monkeypatch):
    from syvrio.app import chat_widgets

    logged = []
    monkeypatch.setattr(chat_widgets, "record_event", lambda *args, **kwargs: logged.append((args, kwargs)))
    app = build_app(tmp_path, [])
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
        assert all(type(value) in (bool, int, float) for _, state in logged for value in state.values())


async def test_switch_defers_distill_then_catches_up_in_background(tmp_path):
    # 回归：切换会话不做内联抽取（不卡顿），deferred 标记由后台补沉淀消化。
    app = build_app(tmp_path, [], extract_on_exit=True)
    from syvrio.memory.models import Candidate

    class CountingExtractor:
        def __init__(self):
            self.calls = 0

        def extract(self, text, context=""):
            self.calls += 1
            from syvrio.memory.models import ExtractionResult

            return ExtractionResult(
                memories=[Candidate(text="后台补记的内容", topic="misc", confidence=0.95)]
            )

    app.service.extractor = counting = CountingExtractor()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        first = app.sessions.create()
        # 3 条用户消息：达到后台补沉淀阈值（min_new_messages=3）
        for text in ("第一场对话一", "第一场对话二", "第一场对话三"):
            app.sessions.append(
                first,
                {"type": "message", "id": new_message_id(), "role": "user", "content": text},
            )
        second = app.sessions.create()
        app.sessions.append(
            second,
            {"type": "message", "id": new_message_id(), "role": "user", "content": "第二场对话内容"},
        )
        app.refresh_sessions()
        app.runtime.resume(first.id)  # 活跃会话才有 close(distill=False) 的 deferred 标记

        app._switch_session(second.id)
        for _ in range(300):
            await pilot.pause()
            if not app.busy and app.runtime.current.id == second.id:
                break
        assert app.runtime.current.id == second.id
        types = {r["type"] for r in app.sessions.load(first.id).records}
        assert "distill_deferred" in types
        for _ in range(300):
            await pilot.pause()
            if app.service.store.has_text("后台补记的内容"):
                break
        assert counting.calls >= 1  # 后台补沉淀已执行
        assert app.service.store.has_text("后台补记的内容")


async def test_session_picker_search_resume_and_delete(tmp_path):
    # 回归（issue 反馈）：/sessions 打开 OpenCode 式选择器——搜索/恢复/删除。
    app = build_app(tmp_path, [])
    from textual.widgets import Input

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from syvrio.agent.session import new_message_id
        from syvrio.app.chat_widgets import SessionPickerScreen

        store = app.sessions
        s1 = store.create()
        store.rename(s1, "Java 并发讨论")
        store.append(s1, {"type": "message", "id": new_message_id(), "role": "user", "content": "Java 并发讨论"})
        s2 = store.create()
        store.rename(s2, "租房预算讨论")
        store.append(s2, {"type": "message", "id": new_message_id(), "role": "user", "content": "租房预算讨论"})

        widget = app.query_one("#input", Input)
        widget.value = "/sessions"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, SessionPickerScreen):
                break
        picker = app.screen
        assert isinstance(picker, SessionPickerScreen)
        assert len(list(picker.query("#picker-list ListItem"))) == 2
        # 回归：SessionItem 内层 Vertical 不能撑满列表，否则高亮变成整块、底部提示被挤掉。
        assert all(item.size.height <= 3 for item in picker.query("#picker-list ListItem"))
        hint = picker.query_one(".pick-hint", Static)
        assert hint.region.bottom <= picker.query_one("#pick-card").region.bottom
        assert str(picker.query_one(".pick-title").render()) == "Sessions"
        for width, height in ((120, 36), (100, 30), (80, 24), (60, 20)):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            card = picker.query_one("#pick-card").region
            assert card.width <= width and card.height <= height
            assert picker.query_one("#session-delete", Button).region.bottom <= card.bottom
            assert picker.query_one(".pick-hint", Static).region.bottom <= card.bottom
        await pilot.resize_terminal(120, 40)
        await pilot.pause()

        picker.query_one("#picker-input", Input).value = "java"
        await pilot.pause()
        picker.on_input_changed(Input.Changed(picker.query_one("#picker-input", Input), "java"))
        await pilot.pause()
        assert len(list(picker.query("#picker-list ListItem"))) == 1

        await pilot.press("enter")
        for _ in range(200):
            await pilot.pause()
            if not app.busy and app.runtime.has_session:
                break
        assert app.runtime.current.id == s1.id

        # 再次打开：Delete 单键不触发永久删除，Ctrl+D 打开默认 Cancel 的确认。
        widget.value = "/sessions"
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, SessionPickerScreen):
                break
        picker = app.screen
        picker.query_one("#picker-input", Input).value = "租房"
        picker.on_input_changed(Input.Changed(picker.query_one("#picker-input", Input), "租房"))
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert app.screen is picker and store.exists(s2.id)
        assert picker.query_one("#picker-input", Input).has_focus

        await pilot.press("ctrl+d")
        await pilot.pause()
        assert "PickScreen" in type(app.screen).__name__
        confirmation = app.screen.query_one("#pick-list")
        assert confirmation.highlighted_child.value is False
        await pilot.press("enter")  # 默认 Cancel
        await pilot.pause()
        assert app.screen is picker and store.exists(s2.id)
        await pilot.press("ctrl+d")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is picker and store.exists(s2.id)

        await pilot.click("#session-delete")  # 鼠标入口使用同一确认流程
        await pilot.pause()
        app.screen.query_one("#pick-list").index = 1
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            if not store.exists(s2.id):
                break
        assert not store.exists(s2.id)
        assert store.exists(s1.id)
        # 过滤结果也要同步：已删会话不能继续留在过滤后的列表里。
        assert len(list(picker.query("#picker-list ListItem"))) == 0


async def test_startup_catchup_does_not_block_switching(tmp_path):
    # 回归：启动补沉淀在后台执行，不能再把输入和会话切换一起锁住。
    import threading

    app = build_app(tmp_path, [], extract_on_exit=True)
    crashed = app.sessions.create()
    app.sessions.append(
        crashed,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "中断前说的话"},
    )

    class BlockingExtractor:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def extract(self, text, context=None):
            self.started.set()
            assert self.release.wait(timeout=10)
            return ExtractionResult(memories=[])

    blocker = BlockingExtractor()
    app.service.extractor = blocker
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(200):
            await pilot.pause()
            if blocker.started.is_set():
                break
        assert blocker.started.is_set()
        assert app.busy is False
        assert app.query_one("#input", Input).disabled is False

        app._switch_session(crashed.id)
        for _ in range(200):
            await pilot.pause()
            if app.runtime.has_session and app.runtime.current.id == crashed.id and not app.busy:
                break
        assert app.runtime.current.id == crashed.id

        blocker.release.set()
        for _ in range(300):
            await pilot.pause()
            if any("Consolidated" in str(widget.render()) for widget in app.query(".card")):
                break
        assert app.busy is False
        assert any("Consolidated 1" in str(widget.render()) for widget in app.query(".card"))


async def test_transcript_replays_tool_events(tmp_path):
    # 回归：旧会话不仅回放问答正文，也要能看到当时的工具事件。
    import json

    from syvrio.agent.tools import SEARCH_NOTICE

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.append(
        session, {"type": "message", "id": "m1", "role": "user", "content": "查 GLM"}
    )
    app.sessions.append(
        session,
        {
            "type": "assistant",
            "id": "a1",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "search", "arguments": {"query": "GLM-5.3"}}],
        },
    )
    payload = {
        "results": [
            {"title": "t", "url": "https://example.com", "snippet": "", "published": None}
        ],
        "provider": "open_websearch",
        "failed": [],
    }
    app.sessions.append(
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
    app.sessions.append(session, {"type": "assistant", "id": "a2", "content": "最终回答"})
    app.runtime.resume(session.id)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        events = [str(widget.render()) for widget in app.query(".event-card")]
        assert any('Search "GLM-5.3": 1 results' in event for event in events)
        replies = [getattr(widget, "_markdown", "") for widget in app.query(".reply")]
        assert any("最终回答" in reply for reply in replies)


async def test_modal_lists_share_chat_scrollbar_style(tmp_path):
    from syvrio.app.chat_widgets import PickScreen, SessionPickerScreen

    def scrollbar_style(widget):
        return (
            widget.styles.scrollbar_size_vertical,
            widget.styles.scrollbar_background,
            widget.styles.scrollbar_color,
            widget.styles.scrollbar_color_hover,
            widget.styles.scrollbar_color_active,
        )

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "会话"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        chat_style = scrollbar_style(app.query_one("#chat"))

        app._show_model_picker([])
        await pilot.pause()
        assert isinstance(app.screen, PickScreen)
        model_list = app.screen.query_one("#pick-list")
        assert model_list.has_class("thin-scroll")
        assert scrollbar_style(model_list) == chat_style
        await pilot.press("escape")
        await pilot.pause()

        app.open_session_picker()
        await pilot.pause()
        assert isinstance(app.screen, SessionPickerScreen)
        session_list = app.screen.query_one("#picker-list")
        assert session_list.has_class("thin-scroll")
        assert scrollbar_style(session_list) == chat_style


async def test_session_picker_is_centered(tmp_path):
    from syvrio.app.chat_widgets import SessionPickerScreen

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "会话"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.open_session_picker()
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, SessionPickerScreen)
        card = picker.query_one("#pick-card")
        # 卡片应水平、垂直居中，而不是贴在左上角。
        assert abs(card.region.x - (picker.region.width - card.region.width) / 2) <= 1
        assert abs(card.region.y - (picker.region.height - card.region.height) / 2) <= 1
        assert "Ctrl+R" in str(picker.query_one(".pick-hint", Static).content)


async def test_dialog_inputs_share_evolution_focus_style(tmp_path):
    from textual.color import Color

    from syvrio.app.chat_widgets import SessionPickerScreen

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.append(
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

        app.open_session_picker()
        await pilot.pause()
        assert isinstance(app.screen, SessionPickerScreen)
        picker_input = app.screen.query_one("#picker-input", Input)
        assert picker_input.styles.background == Color.parse("#222222")
        assert picker_input.styles.border_top[1] == main_input.styles.border_left[1]


async def test_session_picker_ctrl_r_renames_session(tmp_path):
    from syvrio.app.chat_widgets import SessionPickerScreen, TextInputScreen

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.rename(session, "旧标题")
    app.sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "旧标题"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.open_session_picker()
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
        assert app.sessions.load(session.id).title() == "新标题"
        titles = [item.row["title"] for item in app.screen.query("#picker-list SessionItem")]
        assert "新标题" in titles


async def test_session_picker_rename_current_updates_runtime(tmp_path):
    from syvrio.app.chat_widgets import SessionPickerScreen, TextInputScreen

    app = build_app(tmp_path, [])
    session = app.sessions.create()
    app.sessions.rename(session, "旧标题")
    app.sessions.append(
        session,
        {"type": "message", "id": new_message_id(), "role": "user", "content": "旧标题"},
    )
    app.runtime.resume(session.id)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.open_session_picker()
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
        assert app.runtime.current.title() == "当前新标题"


async def test_automatic_reflection_idle_uses_persistent_schedule(tmp_path, monkeypatch):
    import asyncio
    from syvrio.memory.reflection import ReflectionResult
    app = build_app(tmp_path, [])
    calls = []
    monkeypatch.setattr(app.service.reflection, "auto_check_due", lambda now=None: True)
    monkeypatch.setattr(app.service.reflection, "auto_plan", lambda now=None: "dream")
    def run(mode, budget, cancel_event=None, **kwargs):
        calls.append((mode, budget, kwargs.get("auto")))
        return ReflectionResult(mode, requests=1)
    monkeypatch.setattr(app.service.reflection, "run", run)
    async with app.run_test() as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        app._reflection_last_activity = 0
        app._navigation_busy = True
        app._auto_reflection_tick()
        assert calls == []
        app._navigation_busy = False
        app._reflection_command("dream", "cancel")
        app._auto_reflection_tick()
        assert calls == []
        app._reflection_command("dream", "resume")
        app._auto_reflection_tick()
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert calls == [("dream", 1, True)]
        assert not app._reflection_pending
        app._auto_reflection_tick()
        await pilot.pause()
        assert len(calls) == 1


async def test_automatic_profile_refresh_spends_one_request(tmp_path, monkeypatch):
    import asyncio
    from syvrio.memory.reflection import ReflectionResult
    app = build_app(tmp_path, [])
    calls = []
    monkeypatch.setattr(app.service.reflection, "auto_check_due", lambda now=None: True)
    monkeypatch.setattr(app.service.reflection, "auto_plan", lambda now=None: "profile")
    def run(mode, budget, cancel_event=None, **kwargs):
        calls.append((mode, budget, kwargs.get("auto"), kwargs.get("refresh_summary")))
        return ReflectionResult(mode, requests=1, summary_status="updated")
    monkeypatch.setattr(app.service.reflection, "run", run)
    async with app.run_test() as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        app._reflection_last_activity = 0
        app._auto_reflection_tick()
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert calls == [("profile", 1, True, True)]
        assert not app._reflection_pending


async def test_automatic_reflection_skips_when_schedule_not_due(tmp_path, monkeypatch):
    import asyncio
    app = build_app(tmp_path, [])
    monkeypatch.setattr(app.service.reflection, "auto_check_due", lambda now=None: False)
    monkeypatch.setattr(app.service.reflection, "auto_plan", lambda now=None: pytest.fail("到期前不得扫描"))
    monkeypatch.setattr(app.service.reflection, "run", lambda *a, **k: pytest.fail("到期前不得请求"))
    async with app.run_test() as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        app._reflection_last_activity = 0
        app._auto_reflection_tick()
        await pilot.pause()
        assert not app._reflection_pending


async def test_automatic_reflection_plan_without_work_does_not_run(tmp_path, monkeypatch):
    import asyncio
    app = build_app(tmp_path, [])
    monkeypatch.setattr(app.service.reflection, "auto_check_due", lambda now=None: True)
    monkeypatch.setattr(app.service.reflection, "auto_plan", lambda now=None: None)
    monkeypatch.setattr(app.service.reflection, "run", lambda *a, **k: pytest.fail("无任务不得请求"))
    async with app.run_test() as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        app._reflection_last_activity = 0
        app._auto_reflection_tick()
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert not app._reflection_pending
        assert not app.busy


async def test_automatic_reflection_failure_releases_state(tmp_path, monkeypatch):
    import asyncio
    app = build_app(tmp_path, [])
    monkeypatch.setattr(app.service.reflection, "auto_check_due", lambda now=None: True)
    monkeypatch.setattr(app.service.reflection, "auto_plan", lambda now=None: "dream")
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic auto failure")
    monkeypatch.setattr(app.service.reflection, "run", fail)
    async with app.run_test() as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        app._reflection_last_activity = 0
        app._auto_reflection_tick()
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert not app._reflection_pending
        assert not app.busy
        assert not app.query_one("#input", Input).disabled


async def test_automatic_reflection_disabled_or_draft_does_not_start(tmp_path, monkeypatch):
    import asyncio
    app = build_app(tmp_path, [])
    monkeypatch.setattr(app.service.reflection, "auto_check_due",
                        lambda now=None: pytest.fail("禁用或草稿时不得检查调度"))
    async with app.run_test() as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        app.service.config.chat.auto_reflect = False
        app._reflection_last_activity = 0
        app._auto_reflection_tick()
        app.service.config.chat.auto_reflect = True
        app.query_one("#input", Input).value = "尚未发送的草稿"
        app._reflection_last_activity = 0
        app._auto_reflection_tick()
        assert not getattr(app, "_reflection_pending", False)


async def test_removed_shortcuts_are_not_available(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    assert not {"resume", "recall", "remember"}.intersection(item[0] for item in SLASH_COMMANDS)
    cards = []
    monkeypatch.setattr(app, "add_card", lambda message, *args: cards.append(message))
    for command in ("/resume", "/recall 合成查询", "/remember 合成事实"):
        app._command(command)
        assert "Unknown command" in cards[-1]
    opened = []
    monkeypatch.setattr(app, "open_session_picker", lambda: opened.append(True))
    app._command("/sessions")
    assert opened == [True]


@pytest.mark.parametrize("width", [80, 120])
async def test_busy_progress_and_right_aligned_context(tmp_path, width):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(width, 40)) as pilot:
        await pilot.pause()
        app._begin_ask(None)
        app._progress_update("Calling tool: search")
        await pilot.pause()
        model = app.query_one("#model-status", Static)
        context = app.query_one("#status", Static)
        assert "search" in str(model.content)
        assert model.region.right <= context.region.x
        app._end_ask(app.application.conversation.active_turn_id)
        assert "search" not in str(model.content)


async def test_progress_tick_after_status_unmount_is_safe(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.query_one("#model-status", Static).remove()
        app._render_model_status()


def test_fixed_tui_text_is_english():
    import ast
    import re
    from pathlib import Path
    from syvrio.app import chat_app

    directory = Path(chat_app.__file__).parent
    for name in ("chat_app", "chat_commands", "chat_widgets", "chat_flow", "command_catalog", "chat_reflection"):
        path = directory / f"{name}.py"
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                    docstrings.add(id(node.body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                assert not re.search(r"[\u4e00-\u9fff]", node.value), f"{path.name}:{node.lineno}: {node.value}"


async def test_native_input_cursor_tracks_editing_and_cleans_up(tmp_path, monkeypatch):
    import asyncio

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", BarInput)
        writes = []
        cursor_shown = asyncio.Event()
        # Focus schedules cursor output after refresh, which may lag a pilot pause.
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


# ---------- 忙碌态解耦与全局 FIFO（base/chat-stability） ----------


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


async def _wait_ask_started(app, pilot, limit=200):
    for _ in range(limit):
        await pilot.pause()
        # The UI flag precedes worker-side session creation on slower runners.
        if app._ask_active and app.runtime.has_session:
            return
    raise AssertionError("请求未开始")


async def _wait_idle(app, pilot, limit=400):
    for _ in range(limit):
        await pilot.pause()
        if not app.busy:
            return
    raise AssertionError("等待空闲超时")


async def test_input_stays_enabled_during_ask(tmp_path):
    # 回归：等待 LLM 期间输入框保持可输入，不再被全局 busy 禁用。
    app = build_app(tmp_path, [])
    gated = GatedChatModel([AssistantTurn(content="first reply")])
    gated.gates.append(threading.Event())
    app.runtime.model = gated
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one("#input", Input).value = "hello"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)
        assert app.query_one("#input", Input).disabled is False
        app.query_one("#input", Input).value = "typing while waiting"
        await pilot.pause()
        assert app.query_one("#input", Input).value == "typing while waiting"
        gated.gates[0].set()
        await _wait_idle(app, pilot)


async def test_submit_during_ask_queues_fifo_across_sessions(tmp_path):
    # 验收场景：A 生成中 → 切 B（/new）→ A/B 各排队一条 → 按提交顺序全局执行，
    # 回复各自落回原会话，且任意时刻至多一个请求。
    app = build_app(tmp_path, [])
    gated = GatedChatModel([
        AssistantTurn(content="reply-1"),
        AssistantTurn(content="reply-2"),
        AssistantTurn(content="reply-3"),
    ])
    gated.gates = [threading.Event(), threading.Event(), threading.Event()]
    app.runtime.model = gated
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one("#input", Input).value = "m1"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)
        session_a = app.runtime.current.id

        app.query_one("#input", Input).value = "m2"
        await pilot.press("enter")
        await pilot.pause()
        assert [item.line for item in app._queue] == ["m2"]

        app._command("/new")  # 等待期导航命令：立即执行
        for _ in range(100):
            await pilot.pause()
            if not app._navigation_busy:
                break
        assert not app.runtime.has_session  # 新会话尚无第一条消息

        app.query_one("#input", Input).value = "m3"
        await pilot.press("enter")
        await pilot.pause()
        session_b = app.runtime.current.id
        assert session_b != session_a
        assert [item.session_id for item in app._queue] == [session_a, session_b]

        gated.gates[0].set()
        for _ in range(400):
            await pilot.pause()
            if len(gated.calls) >= 2:
                break
        assert app._ask_in_flight == session_a  # 全局 FIFO：A 的排队消息先执行
        gated.gates[1].set()
        for _ in range(400):
            await pilot.pause()
            if len(gated.calls) >= 3:
                break
        assert app._ask_in_flight == session_b
        gated.gates[2].set()
        await _wait_idle(app, pilot)

        assert gated.calls[1] == "m2"
        assert gated.calls[2] == "m3"
        store = app.sessions
        records_a = store.load(session_a).records
        records_b = store.load(session_b).records
        assert any(r.get("content") == "m2" for r in records_a)
        assert any(r.get("type") == "assistant" and "reply-2" in str(r.get("content")) for r in records_a)
        assert any(r.get("content") == "m3" for r in records_b)
        assert any(r.get("type") == "assistant" and "reply-3" in str(r.get("content")) for r in records_b)


async def test_undo_during_generation_bypasses_queue_and_targets_original_session(tmp_path):
    # A 生成中 /undo 立即取消，不进入 FIFO；切到 B 后也只撤销 A，不改 B 的输入。
    app = build_app(tmp_path, [])
    gated = GatedChatModel([
        AssistantTurn(content="reply-1"),
        AssistantTurn(content="reply-2"),
        AssistantTurn(content="reply-b"),
    ])
    gated.gates = [threading.Event(), threading.Event(), threading.Event()]
    app.runtime.model = gated
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one("#input", Input).value = "m1"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)
        gated.gates[0].set()
        await _wait_idle(app, pilot)
        session_a = app.runtime.current.id

        app.query_one("#input", Input).value = "m2"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)
        app.query_one("#input", Input).value = "/undo"
        await pilot.press("enter")
        await pilot.pause()
        assert app._queue == []
        assert app._undo_requested is True

        session_b = app.sessions.create().id
        app._switch_session(session_b)
        for _ in range(100):
            await pilot.pause()
            if not app._navigation_busy:
                break
        assert app.runtime.current.id == session_b

        gated.gates[1].set()
        await _wait_idle(app, pilot)
        assert app.runtime.current.id == session_b
        assert app.query_one("#input", Input).value == ""
        assert app.sessions.load(session_a).user_texts() == ["m1"]
        assert app.sessions.load(session_b).user_texts() == []

        # 在 B 里继续对话不应丢弃 A 的待回填文本。
        widget = app.query_one("#input", Input)
        widget.value = "b-msg"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)
        gated.gates[2].set()
        await _wait_idle(app, pilot)
        assert app.runtime.current.id == session_b
        assert app.sessions.load(session_b).user_texts() == ["b-msg"]

        # 切回 A 后补回填原文；B 的历史与输入始终未被修改。
        app._switch_session(session_a)
        for _ in range(200):
            await pilot.pause()
            if not app._navigation_busy:
                break
        assert app.runtime.current.id == session_a
        assert app.query_one("#input", Input).value == "m2"
        # 等回填触发的 Input.Changed 在界面仍挂载时处理完，避免卸载后查询控件。
        await pilot.pause()


async def test_undo_during_ask_is_immediate_and_idempotent(tmp_path):
    app = build_app(tmp_path, [])
    gated = GatedChatModel(
        [AssistantTurn(content="reply-0"), AssistantTurn(content="reply-1")],
        stream_first=True,
    )
    gated.gates = [threading.Event(), threading.Event()]
    app.runtime.model = gated
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", Input)
        widget.value = "m0"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)
        gated.gates[0].set()
        await _wait_idle(app, pilot)

        widget.value = "m1"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)
        session_id = app.runtime.current.id
        assert app._stream_buffer  # 已在流式生成

        for _ in range(3):
            widget.value = "/undo"
            await pilot.press("enter")
            await pilot.pause()

        # 立即取消，不进入 FIFO；重复 /undo 不会叠加撤销。
        assert app._queue == []
        assert app._cancel_generation.is_set()
        assert app._progress_phase == "Stopping"
        assert len(gated.calls) == 2
        assert app._ask_in_flight == session_id

        gated.gates[1].set()
        await _wait_idle(app, pilot)
        await pilot.pause()

        assert widget.value == "m1"
        session = app.sessions.load(session_id)
        assert session.user_texts() == ["m0"]
        assert len([r for r in session.records if r.get("type") == "undo"]) == 1
        active = "\n".join(
            str(r.get("content", "")) for r in session.active_records()
        )
        assert "m1" not in active and "reply-1" not in active


async def test_undo_keeps_queued_message_paused_without_losing_it(tmp_path):
    app = build_app(tmp_path, [])
    gated = GatedChatModel([
        AssistantTurn(content="reply-m1"),
        AssistantTurn(content="reply-edited"),
        AssistantTurn(content="reply-queued"),
    ])
    gated.gates = [threading.Event(), threading.Event(), threading.Event()]
    app.runtime.model = gated
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", Input)
        widget.value = "m1"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)

        widget.value = "queued message"
        await pilot.press("enter")
        await pilot.pause()
        assert [item.line for item in app._queue] == ["queued message"]

        widget.value = "/undo"
        await pilot.press("enter")
        await pilot.pause()
        gated.gates[0].set()
        await _wait_idle(app, pilot)

        # 撤销完成但不自动发送队列，回填文本不会被覆盖。
        assert widget.value == "m1"
        assert [item.line for item in app._queue] == ["queued message"]
        assert app._queue_paused_after_undo is True
        assert len(gated.calls) == 1

        # 用户主动开始下一轮后，旧队列在回复结束后按原顺序接续。
        widget.value = "m1 edited"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)
        assert app._queue_paused_after_undo is False
        gated.gates[1].set()
        for _ in range(400):
            await pilot.pause()
            if len(gated.calls) >= 3:
                break
        gated.gates[2].set()
        await _wait_idle(app, pilot)
        assert gated.calls == ["m1", "m1 edited", "queued message"]
        assert app._queue == []


async def test_natural_completion_after_undo_request_does_not_keep_reply(tmp_path):
    # 模型与取消竞争：即便正常回复先到达，只要 /undo 已请求，也只撤销当前回合。
    app = build_app(tmp_path, [])
    app.runtime.start()
    session = app.runtime.ensure_session()
    app.sessions.append(
        session,
        {"type": "message", "id": "u1", "role": "user", "content": "m1"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._begin_ask(session.id)
        app._ask_user_message_id = "u1"
        app._undo_requested = True
        app._undo_target_session_id = session.id
        app._undo_target_message_id = "u1"
        app._finish_on_ui(app.application.conversation.active_turn_id, app._finish_reply, "final reply", "12:00", "", session.id, "u1")
        await pilot.pause()

        assert app.query_one("#input", Input).value == "m1"
        loaded = app.sessions.load(session.id)
        assert loaded.user_texts() == []
        assert len([r for r in loaded.records if r.get("type") == "undo"]) == 1
        assert "final reply" not in "\n".join(
            str(r.get("content", "")) for r in loaded.active_records()
        )


async def test_undo_without_bound_message_does_not_touch_earlier_turn(tmp_path):
    # 请求在用户消息落盘前失败：没有具体消息 ID 时不得回退撤销上一轮。
    app = build_app(tmp_path, [])
    app.runtime.start()
    session = app.runtime.ensure_session()
    app.sessions.append(
        session,
        {"type": "message", "id": "u0", "role": "user", "content": "earlier"},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._begin_ask(session.id)
        app._undo_requested = True
        app._undo_target_session_id = session.id
        app._undo_target_message_id = None
        app._finish_on_ui(app.application.conversation.active_turn_id, app._finish_cancelled, session.id, None)
        await pilot.pause()

        loaded = app.sessions.load(session.id)
        assert loaded.user_texts() == ["earlier"]
        assert not any(r.get("type") == "undo" for r in loaded.records)


async def test_undo_restores_multiple_background_sessions_independently(tmp_path):
    # 两个后台会话各自撤销：待回填按会话保存，依次切回时互不覆盖。
    app = build_app(tmp_path, [])
    app.runtime.start()
    session_a = app.runtime.ensure_session()
    app.sessions.append(
        session_a,
        {"type": "message", "id": "a1", "role": "user", "content": "alpha"},
    )
    session_b = app.runtime.sessions.create()
    app.sessions.append(
        session_b,
        {"type": "message", "id": "b1", "role": "user", "content": "beta"},
    )
    session_c = app.runtime.sessions.create()
    app.runtime.session = app.runtime.sessions.load(session_c.id)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for session, message_id in ((session_a, "a1"), (session_b, "b1")):
            app._begin_ask(session_c.id)
            app._undo_requested = True
            app._undo_target_session_id = session.id
            app._undo_target_message_id = message_id
            app._finish_on_ui(app.application.conversation.active_turn_id, app._finish_cancelled, session.id, message_id)
            await pilot.pause()

        assert app._pending_undo_restore == {
            session_a.id: "alpha",
            session_b.id: "beta",
        }
        assert app.sessions.load(session_a.id).user_texts() == []
        assert app.sessions.load(session_b.id).user_texts() == []

        app._switch_session(session_a.id)
        for _ in range(200):
            await pilot.pause()
            if not app._navigation_busy:
                break
        assert app.runtime.current.id == session_a.id
        assert app.query_one("#input", Input).value == "alpha"
        assert session_a.id not in app._pending_undo_restore

        # 用户清空输入后再切回 B，消费 B 自己的待回填。
        app.query_one("#input", Input).value = ""
        app._switch_session(session_b.id)
        for _ in range(200):
            await pilot.pause()
            if not app._navigation_busy:
                break
        assert app.runtime.current.id == session_b.id
        assert app.query_one("#input", Input).value == "beta"
        assert app._pending_undo_restore == {}


async def test_undo_restore_keeps_edited_input_and_retries_later(tmp_path, monkeypatch):
    # 输入框已有用户新内容时不得静默覆盖；待回填保留，清空后再次应用。
    app = build_app(tmp_path, [])
    app.runtime.start()
    session = app.runtime.ensure_session()
    notifications: list[str] = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append(message))

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", Input)
        widget.value = "my draft"
        app._pending_undo_restore[session.id] = "original text"
        app._flush_undo_refresh()

        assert widget.value == "my draft"
        assert app._pending_undo_restore[session.id] == "original text"
        assert any("not restored" in item for item in notifications)

        widget.value = ""
        app._flush_undo_refresh()
        assert widget.value == "original text"
        assert session.id not in app._pending_undo_restore


async def test_undo_without_target_resumes_queued_messages(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    gated = GatedChatModel([AssistantTurn(content="queued reply")])
    gated.gates.append(threading.Event())
    app.runtime.model = gated
    session = app.runtime.ensure_session()
    app.sessions.append(
        session,
        {"type": "message", "id": "old", "role": "user", "content": "earlier"},
    )
    notifications: list[str] = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append(message))
    from syvrio.app.chat_flow import _QueuedLine

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._queue.append(_QueuedLine(session.id, "queued message"))
        app._begin_ask(session.id)
        app._undo_requested = True
        app._undo_target_session_id = session.id
        app._undo_target_message_id = None
        app._finish_on_ui(app.application.conversation.active_turn_id, app._finish_cancelled, session.id, None)

        await _wait_ask_started(app, pilot)
        assert app._queue_paused_after_undo is False
        assert app._queue == []
        gated.gates[0].set()
        await _wait_idle(app, pilot)

        loaded = app.sessions.load(session.id)
        assert loaded.user_texts() == ["earlier", "queued message"]
        assert not any(record.get("type") == "undo" for record in loaded.records)
        assert any("No generated turn to undo" in item for item in notifications)
        assert any("resumed" in item for item in notifications)


async def test_undo_session_load_failure_resumes_queued_messages(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    gated = GatedChatModel([AssistantTurn(content="queued reply")])
    gated.gates.append(threading.Event())
    app.runtime.model = gated
    session = app.runtime.ensure_session()
    app.sessions.append(
        session,
        {"type": "message", "id": "old", "role": "user", "content": "earlier"},
    )
    notifications: list[str] = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append(message))
    from syvrio.app.chat_flow import _QueuedLine

    original_load = app.runtime.sessions.load
    load_calls = {"count": 0}

    def fail_first_load(session_id):
        load_calls["count"] += 1
        if load_calls["count"] == 1:
            raise OSError("vault unavailable")
        return original_load(session_id)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # 启动补记可能读取会话；撤销故障注入放在界面稳定之后。
        monkeypatch.setattr(app.runtime.sessions, "load", fail_first_load)
        app._queue.append(_QueuedLine(session.id, "queued message"))
        app._begin_ask(session.id)
        app._undo_requested = True
        app._undo_target_session_id = session.id
        app._undo_target_message_id = "old"
        app._finish_on_ui(app.application.conversation.active_turn_id, app._finish_cancelled, session.id, "old")

        await _wait_ask_started(app, pilot)
        assert app._queue_paused_after_undo is False
        gated.gates[0].set()
        await _wait_idle(app, pilot)

        loaded = app.sessions.load(session.id)
        assert loaded.user_texts() == ["earlier", "queued message"]
        assert not any(record.get("type") == "undo" for record in loaded.records)
        assert any("Undo failed: vault unavailable" in item for item in notifications)
        assert any("resumed" in item for item in notifications)


async def test_undo_write_failure_resumes_queued_messages(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    gated = GatedChatModel([AssistantTurn(content="queued reply")])
    gated.gates.append(threading.Event())
    app.runtime.model = gated
    session = app.runtime.ensure_session()
    app.sessions.append(
        session,
        {"type": "message", "id": "old", "role": "user", "content": "earlier"},
    )
    notifications: list[str] = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append(message))
    from syvrio.app.chat_flow import _QueuedLine

    def fail_undo(*args, **kwargs):
        raise RuntimeError("undo append failed")

    monkeypatch.setattr(app.runtime, "undo", fail_undo)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._queue.append(_QueuedLine(session.id, "queued message"))
        app._begin_ask(session.id)
        app._undo_requested = True
        app._undo_target_session_id = session.id
        app._undo_target_message_id = "old"
        app._finish_on_ui(app.application.conversation.active_turn_id, app._finish_cancelled, session.id, "old")

        await _wait_ask_started(app, pilot)
        assert app._queue_paused_after_undo is False
        gated.gates[0].set()
        await _wait_idle(app, pilot)

        loaded = app.sessions.load(session.id)
        assert loaded.user_texts() == ["earlier", "queued message"]
        assert not any(record.get("type") == "undo" for record in loaded.records)
        assert any("Undo failed: undo append failed" in item for item in notifications)
        assert any("resumed" in item for item in notifications)


async def test_queued_command_skipped_when_target_unavailable(tmp_path):
    # 目标会话文件已删除：命令跳过并提示，不切换、不误伤当前会话。
    app = build_app(tmp_path, [])
    gated = GatedChatModel([AssistantTurn(content="reply-1")])
    gated.gates.append(threading.Event())
    app.runtime.model = gated
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one("#input", Input).value = "m1"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)
        session_a = app.runtime.current.id

        app.query_one("#input", Input).value = "/rename queued"
        await pilot.press("enter")
        await pilot.pause()
        session_b = app.sessions.create().id
        app._switch_session(session_b)
        for _ in range(100):
            await pilot.pause()
            if not app._navigation_busy:
                break

        # 目标会话文件中间行损坏：load 必抛，排队命令应跳过而非切过去。
        # （直接 unlink 会被仍在进行的 ask 的落盘 append 原路重建，测不到这个分支。）
        (tmp_path / "vault" / "sessions" / f"{session_a}.jsonl").write_bytes(b"not-json-corrupt\n")
        gated.gates[0].set()
        await _wait_idle(app, pilot)
        for _ in range(100):
            await pilot.pause()
            if app._pending_command is None and not app.busy:
                break
        assert app.runtime.current.id == session_b  # 没有切走
        assert app._pending_command is None
        assert app._queue == []


async def test_switch_during_ask_is_immediate_and_reply_lands_in_original(tmp_path):
    import asyncio

    # 回归：请求进行中切会话必须立即执行（不再排队），回复写回原会话且不串入当前视图。
    app = build_app(tmp_path, [])
    gated = GatedChatModel([AssistantTurn(content="slow reply")])
    gated.gates.append(threading.Event())
    app.runtime.model = gated
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one("#input", Input).value = "question"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)
        session_a = app.runtime.current.id
        model_status = app.query_one("#model-status", Static)
        model_label = app.application.conversation.model_label
        app._progress_started -= 20  # Simulate a request already waiting before switching.
        started = app._progress_started
        assert str(model_status.content) != model_label

        session_b = app.sessions.create().id
        try:
            app._switch_session(session_b)
            async with asyncio.timeout(5):
                while app._navigation_busy or app.runtime.current.id != session_b:
                    await pilot.pause()
            assert app._ask_active
            assert str(model_status.content) == model_label
            assert app._progress_started == started

            app._switch_session(session_a)
            async with asyncio.timeout(5):
                while app._navigation_busy or app.runtime.current.id != session_a:
                    await pilot.pause()
            assert app._ask_active
            assert app._progress_started == started
            elapsed = str(model_status.content).rsplit(" ", 1)[-1]
            assert int(elapsed.removesuffix("s")) >= 20

            app._switch_session(session_b)
            async with asyncio.timeout(5):
                while app._navigation_busy or app.runtime.current.id != session_b:
                    await pilot.pause()
        finally:
            gated.gates[0].set()
        await _wait_idle(app, pilot)
        records_a = app.sessions.load(session_a).records
        assert any(r.get("type") == "assistant" and "slow reply" in str(r.get("content")) for r in records_a)
        assert not app.query(".reply")  # B 的空视图未被串入


async def test_switch_back_to_generating_session_replays_stream(tmp_path):
    import asyncio

    # 回归：切走再切回生成中的会话，未落盘的活跃段从状态重放并继续实时更新。
    app = build_app(tmp_path, [])
    gated = GatedChatModel([AssistantTurn(content="streamed body")], stream_first=True)
    gated.gates.append(threading.Event())
    app.runtime.model = gated
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one("#input", Input).value = "q"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)
        async with asyncio.timeout(5):
            while app._stream_buffer != "streamed body":
                await pilot.pause()
        session_a = app.runtime.current.id

        session_b = app.sessions.create().id
        app._switch_session(session_b)
        # 导航标志先释放，Textual 的旧控件移除随后异步完成。
        async with asyncio.timeout(5):
            while app._navigation_busy or app.runtime.current.id != session_b or app.query(".reply"):
                await pilot.pause()
        assert not app.query(".reply")  # 切走后视图清空，buffer 保留

        app._switch_session(session_a)
        async with asyncio.timeout(5):
            while app._navigation_busy or app.runtime.current.id != session_a or not app.query(".reply"):
                await pilot.pause()
        assert app.query(".reply")  # 活跃段重放
        assert app._stream_buffer == "streamed body"

        gated.gates[0].set()
        await _wait_idle(app, pilot)
        records_a = app.sessions.load(session_a).records
        assert any(r.get("type") == "assistant" and "streamed body" in str(r.get("content")) for r in records_a)


async def test_closed_marker_after_assistant_records_when_switching_mid_ask(tmp_path):
    # 回归：切换时对生成中的会话延迟打关闭标记，closed 必须排在 assistant 记录之后。
    app = build_app(tmp_path, [])
    gated = GatedChatModel([AssistantTurn(content="late reply")])
    gated.gates.append(threading.Event())
    app.runtime.model = gated
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one("#input", Input).value = "q"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)
        session_a = app.runtime.current.id

        session_b = app.sessions.create().id
        app._switch_session(session_b)
        for _ in range(100):
            await pilot.pause()
            if not app._navigation_busy:
                break
        # 请求未结束时不得提前落 closed 标记
        path = app.sessions.load(session_a).path
        types = [json.loads(line)["type"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert "closed" not in types

        gated.gates[0].set()
        await _wait_idle(app, pilot)
        types = [json.loads(line)["type"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert types.count("closed") == 1
        assert "distill_deferred" in types
        last_assistant = max(i for i, t in enumerate(types) if t == "assistant")
        assert len(types) - 1 - types[::-1].index("closed") > last_assistant


async def test_delete_blocked_while_generating(tmp_path):
    # 回归：删除正在生成回复的会话必须被拒绝，否则后台写入会重建僵尸文件。
    app = build_app(tmp_path, [])
    gated = GatedChatModel([AssistantTurn(content="slow reply")])
    gated.gates.append(threading.Event())
    app.runtime.model = gated
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one("#input", Input).value = "q"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)
        session_a = app.runtime.current.id

        session_b = app.sessions.create().id
        app._switch_session(session_b)
        for _ in range(100):
            await pilot.pause()
            if not app._navigation_busy:
                break

        app.confirm_delete_session({"id": session_a, "title": "A"})
        await pilot.pause()
        assert app.sessions.exists(session_a)  # 拒绝删除
        assert len(app.screen_stack) == 1  # 未弹出确认对话框


async def test_command_during_ask_queues_and_runs_after_reply(tmp_path):
    # 回归：等待期的 /compact 进全局队列，回复完成后执行，而不是取消进行中的请求。
    app = build_app(tmp_path, [])
    gated = GatedChatModel([AssistantTurn(content="reply body")])
    gated.gates.append(threading.Event())
    app.runtime.model = gated
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one("#input", Input).value = "q"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)

        app.query_one("#input", Input).value = "/compact"
        await pilot.press("enter")
        await pilot.pause()
        assert [item.line for item in app._queue] == ["/compact"]
        assert len(gated.calls) == 1  # 请求未被取消

        gated.gates[0].set()
        await _wait_idle(app, pilot)
        assert any("No earlier records" in str(card.render()) for card in app.query(".event-card"))


async def test_switch_consolidate_skipped_while_ask_in_flight(tmp_path, monkeypatch):
    # 回归：切走时目标会话仍在生成，后台补沉淀必须跳过，留给启动补记。
    calls: list[str] = []

    def fake_consolidate(self, **kwargs):
        calls.append("consolidate")
        return {"consolidated": 0, "errors": [], "admitted": 0, "queued": 0, "duplicates": 0, "blocked": 0}

    monkeypatch.setattr(AgentRuntime, "consolidate_pending", fake_consolidate)
    app = build_app(tmp_path, [])
    gated = GatedChatModel([AssistantTurn(content="slow reply")])
    gated.gates.append(threading.Event())
    app.runtime.model = gated
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for _ in range(100):
            await pilot.pause()
            if not app.busy:
                break
        calls.clear()  # 启动补记的调用不计入

        app.query_one("#input", Input).value = "q"
        await pilot.press("enter")
        await _wait_ask_started(app, pilot)
        session_a = app.runtime.current.id

        session_b = app.sessions.create().id
        app._switch_session(session_b)
        for _ in range(100):
            await pilot.pause()
            if not app._navigation_busy:
                break
        assert calls == []  # 生成期间不补沉淀

        gated.gates[0].set()
        await _wait_idle(app, pilot)
        assert calls == []


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
    monkeypatch, release, term, headless, expected,
):
    from types import SimpleNamespace
    from syvrio.app import chat_input

    monkeypatch.setattr(chat_input.sys, "platform", "linux")
    monkeypatch.setattr(chat_input.platform, "release", lambda: release)
    monkeypatch.setenv("TERM", term)
    for name in ("WT_SESSION", "WT_PROFILE_ID", "TERM_PROGRAM"):
        monkeypatch.delenv(name, raising=False)
    widget = SimpleNamespace(app=SimpleNamespace(_driver=SimpleNamespace(is_headless=headless)))
    assert BarInput._uses_native_cursor.fget(widget) is expected


async def test_native_cursor_position_refreshed_on_resize(tmp_path, monkeypatch):
    # 回归：布局变化（折叠侧栏/缩放）后 App 回放的是旧光标坐标，必须重算。
    app = build_app(tmp_path, [])
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


async def test_queued_command_rechecks_target_after_another_navigation(tmp_path, monkeypatch):
    from syvrio.app.chat_flow import _QueuedLine

    app = build_app(tmp_path, [])
    a = app.sessions.create()
    b = app.sessions.create()
    executed = []
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _wait_idle(app, pilot)
        app.runtime.resume(b.id)
        original_switch = app._switch_session
        redirected = False

        def switch(session_id):
            nonlocal redirected
            original_switch(session_id)
            if session_id == a.id and not redirected:
                redirected = True
                app._pending_switch = b.id

        monkeypatch.setattr(app, "_switch_session", switch)
        monkeypatch.setattr(app, "_command", lambda line: executed.append((app.runtime.current.id, line)))
        app._queue.append(_QueuedLine(a.id, "/undo"))
        app._dispatch_queue()
        for _ in range(200):
            await pilot.pause()
            if executed and not app.busy:
                break
        assert executed == [(a.id, "/undo")]
        assert app._pending_command is None


async def test_queued_sync_command_continues_following_message(tmp_path, monkeypatch):
    from syvrio.app.chat_flow import _QueuedLine

    app = build_app(tmp_path, [])
    a = app.sessions.create()
    b = app.sessions.create()
    executed = []
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _wait_idle(app, pilot)
        app.runtime.resume(b.id)
        monkeypatch.setattr(app, "_command", lambda line: executed.append(("command", line)))
        monkeypatch.setattr(app, "_start_ask", lambda line, sid: executed.append(("message", line, sid)))
        app._queue.extend([_QueuedLine(a.id, "/undo"), _QueuedLine(a.id, "next message")])
        app._dispatch_queue()
        for _ in range(200):
            await pilot.pause()
            if len(executed) == 2 and not app.busy:
                break
        assert executed == [("command", "/undo"), ("message", "next message", a.id)]
        assert not app._queue


@pytest.mark.parametrize("value, position, cells", [
    ("", 0, 0),
    ("搜索下", 3, 6),
    ("搜索下 ", 4, 7),
    ("a你b", 3, 4),
    ("a你b", 1, 1),
    ("a你b", 2, 3),
])
async def test_input_caret_matches_insertion_cell(tmp_path, value, position, cells):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        widget = app.query_one("#input", BarInput)
        widget.value = value
        widget.cursor_position = position
        await pilot.pause()
        assert widget.cursor_screen_offset.x == widget.content_region.x + cells - widget.scroll_offset.x
        assert widget.cursor_screen_offset.y == widget.content_region.y
        assert app.cursor_position == widget.cursor_screen_offset
        assert widget.value == value


async def test_escape_stops_stream_keeps_draft_and_drains_visible_queue(tmp_path):
    app = build_app(tmp_path, [])
    release = threading.Event()
    calls = []
    class Model:
        def chat(self, messages, tools, on_delta=None):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                on_delta("partial reply")
                assert release.wait(10)
                on_delta("must not appear")
            else:
                on_delta("queued reply")
            return AssistantTurn(content="queued reply")
    app.runtime.model = Model()
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            widget = app.query_one("#input", Input)
            widget.value = "first"
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause()
                if app._stream_buffer:
                    break
            assert widget.placeholder == ""
            widget.value = "second [literal]"
            await pilot.press("enter")
            panel = app.query_one("#send-queue", Static)
            assert panel.display
            assert "second [literal]" in str(panel.content)
            assert len(calls) == 1
            widget.value = "unfinished draft"
            await pilot.press("escape")
            assert app._cancel_generation.is_set()
            assert widget.value == "unfinished draft"
            assert not widget.disabled
            release.set()
            await _wait_idle(app, pilot)
            assert calls == ["first", "second [literal]"]
            assert not panel.display
            assert widget.placeholder == ""
            assert widget.value == "unfinished draft"
            records = app.sessions.load(app.runtime.current.id).records
            contents = "\n".join(r.get("content", "") for r in records)
            assert "partial reply" in contents and "stopped by user" in contents
            assert "must not appear" not in contents
    finally:
        release.set()


async def test_escape_closes_palette_before_stopping(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test() as pilot:
        app._begin_ask(None)
        app._cancel_generation = threading.Event()
        widget = app.query_one("#input", Input)
        widget.value = "/sta"
        await pilot.pause()
        await pilot.press("escape")
        assert not app._cancel_generation.is_set()
        assert widget.value == "/sta"
        await pilot.press("escape")
        assert app._cancel_generation.is_set()
        app._end_ask(app.application.conversation.active_turn_id)


@pytest.mark.parametrize("size", [(80, 24), (120, 40), (160, 50)])
async def test_pending_long_text_scrolls_without_hiding_actions(tmp_path, size):
    from textual.containers import VerticalScroll
    from textual.widgets import ListView
    from syvrio.app.chat_widgets import PickScreen
    from syvrio.memory.models import Candidate, PendingItem
    app = build_app(tmp_path, [])
    text = "Synthetic [bold] literal 中文 \n" * 100 + "END OF FULL PENDING TEXT"
    item = PendingItem.from_candidate(Candidate(text=text, topic="preferences", confidence=0.5),
                                      "chat", "low confidence")
    app.service.queue.add(item)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app._pending_picked(item.id)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PickScreen)
        assert str(screen.query_one("#pick-detail-text", Static).content) == text
        panel = screen.query_one("#pick-detail", VerticalScroll)
        listing = screen.query_one("#pick-list", ListView)
        assert panel.max_scroll_y > 0
        assert listing.has_focus
        await pilot.press("pagedown")
        await pilot.pause()
        assert panel.scroll_y > 0 and listing.index == 0
        await pilot.press("pageup")
        await pilot.pause()
        assert panel.scroll_y == 0
        panel.scroll_end(animate=False)
        await pilot.pause()
        assert panel.scroll_y == panel.max_scroll_y
        assert listing.region.bottom <= size[1]
        assert screen.query_one("#pick-card").size.width > 64
        assert [child.label for child in listing.children] == ["Approve", "Edit and approve", "Reject"]
        await pilot.press("escape")
        await pilot.pause()
        assert app.service.pending()[0].text == text
        assert app.query_one("#input", Input).has_focus


@pytest.mark.parametrize("action", ["approve", "edit", "reject"])
async def test_long_pending_review_actions_keep_full_text(tmp_path, action):
    from textual.containers import VerticalScroll
    from syvrio.app.chat_widgets import TextInputScreen
    from syvrio.memory.models import Candidate, PendingItem
    app = build_app(tmp_path, [])
    text = "Synthetic pending full text 中文 \n" * 30 + "LAST LINE"
    item = PendingItem.from_candidate(Candidate(text=text, confidence=0.5), "chat", "low confidence")
    app.service.queue.add(item)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._pending_picked(item.id)
        await pilot.pause()
        app.screen.query_one("#pick-detail", VerticalScroll).scroll_end(animate=False)
        await pilot.pause()
        if action == "edit":
            await pilot.press("down", "enter")
            await pilot.pause()
            assert isinstance(app.screen, TextInputScreen)
            assert app.screen.query_one("#pick-input", Input).value == text
            app.screen.query_one("#pick-input", Input).value = "Synthetic final edited text"
            await pilot.press("enter")
        elif action == "reject":
            await pilot.press("down", "down", "enter")
        else:
            await pilot.press("enter")
        await pilot.pause()
        assert not app.service.pending()
        entries = app.service.store.entries()
        if action == "reject":
            assert not entries
        else:
            expected = "Synthetic final edited text" if action == "edit" else text.strip()
            assert [entry.text for entry in entries] == [expected]


@pytest.mark.parametrize("size", [(80, 24), (120, 40), (160, 50)])
async def test_short_pending_review_stays_compact(tmp_path, size):
    from textual.containers import VerticalScroll
    from syvrio.memory.models import Candidate, PendingItem

    app = build_app(tmp_path, [])
    item = PendingItem.from_candidate(Candidate(text="Synthetic short confirmation.", confidence=0.5),
                                      "chat", "low confidence")
    app.service.queue.add(item)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app._pending_picked(item.id)
        await pilot.pause()
        card = app.screen.query_one("#pick-card")
        panel = app.screen.query_one("#pick-detail", VerticalScroll)
        assert card.size.width <= 76
        assert card.size.height <= 14
        assert panel.size.height == 1
        assert panel.max_scroll_y == 0
        assert app.screen.query_one("#pick-list").region.bottom < card.region.bottom


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_welcome_transitions_to_left_aligned_conversation(tmp_path, size):
    app = build_app(tmp_path, [AssistantTurn(content="Synthetic reply")])
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        chat = app.query_one("#chat")
        welcome = app.query_one(".welcome-shell")
        assert chat.has_class("home")
        assert welcome.region.width <= chat.content_region.width
        assert chat.region.bottom <= app.query_one("#input").region.y
        assert chat.styles.overflow_y == "auto"
        assert app.query_one("#composer-hint").region.bottom <= size[1]
        app.query_one("#input", Input).value = "Synthetic question"
        await pilot.press("enter")
        await _wait_reply(app, pilot)
        await pilot.pause()
        assert not chat.has_class("home")
        assert not app.query(".welcome-shell")
        bubble = app.query_one(".bubble")
        assert bubble.region.x <= app.query_one(".reply").region.x + 1
        assert bubble.region.width > chat.content_region.width * 0.8


@pytest.mark.parametrize("size", [(50, 24), (120, 40)])
async def test_home_has_full_project_wordmark(tmp_path, size):
    from syvrio.app.chat_widgets import HomePanel
    from rich.cells import cell_len
    app = build_app(tmp_path, [])
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        logo = app.query_one(".welcome-wordmark", Static)
        for _ in range(30):
            if logo.region.height == (1 if app.query_one(HomePanel).size.width < HomePanel.wordmark_width() else 7):
                break
            await pilot.pause(0.02)
        assert logo.region.height == (1 if app.query_one(HomePanel).size.width < HomePanel.wordmark_width() else 7)
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


async def test_stream_follow_pauses_above_bottom_and_resumes_at_bottom(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(100, 24)) as pilot:
        app.add_user_turn("Synthetic question")
        text = "\n\n".join(f"Synthetic paragraph {i}" for i in range(50))
        app.add_assistant_turn(text, "12:00")
        await pilot.pause()
        await pilot.pause()
        chat = app.query_one("#chat")
        assert chat.is_vertical_scroll_end
        app._scroll_end()  # 已排队的旧刷新也不能覆盖随后发生的上滑。
        chat.scroll_to(y=0, animate=False, immediate=True)
        await pilot.pause()
        assert not chat.follow_output
        old_y = chat.scroll_y
        app._stream_widget = app.query_one(".reply")
        app._stream_buffer = text + "\n\nNew output"
        app._stream_flush()
        await pilot.pause()
        await pilot.pause()
        assert chat.scroll_y == old_y
        chat.scroll_end(animate=False)
        await pilot.pause()
        assert chat.follow_output
        app._stream_buffer += "\n\nMore output" * 5
        app._stream_flush()
        await pilot.pause()
        await pilot.pause()
        assert chat.is_vertical_scroll_end


async def test_chat_selection_suspends_cursor_and_stream_scroll(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from textual.selection import SELECT_ALL
    app = build_app(tmp_path, [])
    async with app.run_test(size=(100, 24)) as pilot:
        app.add_user_turn("Synthetic question")
        app.add_assistant_turn("\n\n".join("Synthetic paragraph" for _ in range(40)), "12:00")
        await pilot.pause()
        await pilot.pause()
        widget = app.query_one("#input", BarInput)
        writes = []
        monkeypatch.setattr(app._driver, "write", writes.append)
        monkeypatch.setattr(BarInput, "_uses_native_cursor", property(lambda self: True))
        widget._show_native_cursor()
        assert widget._native_cursor_active
        reply = app.query_one(".reply")
        chat = app.query_one("#chat")
        app.on_mouse_down(SimpleNamespace(widget=reply))
        assert widget._cursor_suspended and not widget._native_cursor_active
        app.screen.selections = {reply: SELECT_ALL}
        app.on_mouse_up(SimpleNamespace(widget=reply))
        old_y = chat.scroll_y
        app._stream_widget = reply
        app._stream_buffer = reply._markdown + "\n\nNew output" * 5
        app._stream_flush()
        await pilot.pause()
        await pilot.pause()
        assert chat.scroll_y == old_y
        assert app.screen.selections
        widget.on_key()
        assert not widget._cursor_suspended and widget._native_cursor_active


async def test_mouse_drag_in_chat_keeps_input_caret_stable(tmp_path):
    from textual.color import Color
    app = build_app(tmp_path, [])
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


@pytest.mark.parametrize("paragraphs", [40, 50, 60])
async def test_wheel_during_stream_freezes_layout_and_caret_until_bottom(tmp_path, monkeypatch, paragraphs):
    from textual.events import MouseScrollUp, MouseScrollDown
    app = build_app(tmp_path, [])
    async with app.run_test(size=(100, 24)) as pilot:
        app.add_user_turn("Synthetic question")
        original = "\n\n".join(f"Paragraph {i}" for i in range(paragraphs))
        app.add_assistant_turn(original, "12:00")
        await pilot.pause()
        await pilot.pause()
        chat = app.query_one("#chat")
        reply = app.query_one(".reply")
        widget = app.query_one("#input", BarInput)
        monkeypatch.setattr(app._driver, "write", lambda *_: None)
        monkeypatch.setattr(BarInput, "_uses_native_cursor", property(lambda self: True))
        widget._show_native_cursor()
        chat.post_message(MouseScrollUp(chat, x=5, y=5, delta_x=0, delta_y=0, button=0, shift=False, meta=False, ctrl=False))
        await pilot.pause()
        assert widget._cursor_suspended and not widget._native_cursor_active
        assert not chat.follow_output
        y, height = chat.scroll_y, reply.virtual_size.height
        app._stream_widget = reply
        for index in range(8):
            app._stream_buffer = original + "\n\n# New heading\n\n" + ("new content\n" * (index + 1))
            app._stream_flush()
            await pilot.pause()
            assert reply._markdown == original
            assert chat.scroll_y == y and reply.virtual_size.height == height
        final = app._stream_buffer + "\n\nFinal output"
        app._finalize_stream_segment(final)
        assert reply._markdown == original  # 完成也不能重排阅读中的段落。
        widget.on_focus()  # 后台完成或恢复焦点不能擅自重新显示硬件光标。
        await pilot.pause()
        assert widget._cursor_suspended and not widget._native_cursor_active
        chat.scroll_end(animate=False)
        for _ in range(30):
            await pilot.pause(0.02)
            if reply._markdown == final and reply.virtual_size.height > height and chat.is_vertical_scroll_end:
                break
        assert reply._markdown == final
        assert reply.virtual_size.height > height
        assert reply._pending_markdown is None
        assert chat.is_vertical_scroll_end, (chat.scroll_y, chat.max_scroll_y, chat.follow_output,
                                              chat.selecting, bool(app.screen.selections))
        widget.on_key()
        assert widget._native_cursor_active


async def test_selected_stream_text_stays_copyable_until_input_clicked(tmp_path):
    from textual.selection import SELECT_ALL
    app = build_app(tmp_path, [])
    async with app.run_test(size=(100, 24)) as pilot:
        app.add_user_turn("Synthetic question")
        app.add_assistant_turn("Original **selected** text", "12:00")
        await pilot.pause()
        await pilot.pause()
        chat = app.query_one("#chat")
        reply = app.query_one(".reply")
        app.screen.selections = {reply: SELECT_ALL}
        before = reply.get_selection(SELECT_ALL)[0]
        for i in range(5):
            reply.update("Changed reply " + "new text " * (i + 1))
            await pilot.pause()
            assert reply.get_selection(SELECT_ALL)[0] == before
        assert reply._pending_markdown
        await pilot.click("#input")
        await pilot.pause()
        assert not app.screen.selections
        assert reply._pending_markdown is None
        assert "Changed reply" in reply.get_selection(SELECT_ALL)[0]


async def test_dragging_scrollbar_freezes_stream_layout(tmp_path):
    app = build_app(tmp_path, [])
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


def _configure_git_remote(app, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    vault = str(app.service.config.vault)
    subprocess.run(["git", "-C", vault, "remote", "add", "origin", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "-C", vault, "push", "-u", "origin", "HEAD"], check=True, capture_output=True)
    return remote


def _remote_log(remote) -> list[str]:
    # Vault 提交信息含中文；Windows GBK 默认解码会炸掉 subprocess 读取线程。
    completed = subprocess.run(
        ["git", "-C", str(remote), "log", "--oneline"],
        capture_output=True, text=True, encoding="utf-8",
    )
    return [line for line in completed.stdout.splitlines() if line]


async def test_git_confirmation_responsive_layout(tmp_path):
    app = build_app(tmp_path, [])
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


async def test_git_screen_requires_explicit_confirmation(tmp_path):
    import asyncio

    app = build_app(tmp_path, [])
    remote = _configure_git_remote(app, tmp_path)
    before = _remote_log(remote)
    app.service.note("TUI 同步记录", topic="misc")
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        app._command("/git")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert isinstance(app.screen, GitScreen)
        content = str(app.screen.query_one("#git-content", Static).content)
        assert "Initialized" in content
        assert "Not configured" not in content

        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, GitConfirmScreen)
        assert app.screen.query_one("#git-cancel", Button).has_focus
        assert _remote_log(remote) == before

        await pilot.press("enter")  # 默认 Cancel，Enter 不得直接同步
        await pilot.pause()
        assert isinstance(app.screen, GitScreen)
        assert _remote_log(remote) == before

        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, GitScreen)
        assert _remote_log(remote) == before

        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, GitConfirmScreen)
        await pilot.click("#git-synchronize")
        for _ in range(100):
            await pilot.pause()
            if app._git_sync_running:
                break
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()

        assert isinstance(app.screen, GitScreen)
        assert len(_remote_log(remote)) == len(before) + 1
        content = str(app.screen.query_one("#git-content", Static).content)
        assert "Synchronization Completed" in content
        assert "Last Result" in content
        assert "is running" not in content
        assert "Git" not in str(app.query_one("#status", Static).content)


async def test_ctrl_s_outside_git_screen_does_nothing(tmp_path):
    import asyncio

    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        stack = list(app.screen_stack)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen_stack == stack
        assert not isinstance(app.screen, (GitScreen, GitConfirmScreen))


async def test_git_sync_failure_shows_reason(tmp_path):
    import asyncio

    app = build_app(tmp_path, [])
    app.service.note("无远程配置", topic="misc")
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        app._command("/git")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.click("#git-synchronize")
        for _ in range(100):
            await pilot.pause()
            if app._git_sync_running:
                break
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()

        content = str(app.screen.query_one("#git-content", Static).content)
        assert "Partially Completed" in content
        assert "Remote is not configured" in content
        assert not app.service.git_sync.running


async def test_closing_git_screen_does_not_cancel_sync(tmp_path):
    import asyncio

    app = build_app(tmp_path, [])
    remote = _configure_git_remote(app, tmp_path)
    before = _remote_log(remote)
    app.service.note("关闭界面时仍在推送", topic="misc")
    started, release = threading.Event(), threading.Event()
    real_push = app.service.repo.push

    def slow_push():
        started.set()
        assert release.wait(5)
        return real_push()

    app.service.repo.push = slow_push
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        app._command("/git")
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.click("#git-synchronize")
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await pilot.pause()
            content = str(app.screen.query_one("#git-content", Static).content)
            assert "Git Synchronization" in content
            assert "Pushing to remote" in content
            # 执行中再次 Ctrl+S 不得打开第二个确认界面。
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, GitScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, GitScreen)
        finally:
            release.set()
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        assert not app._git_sync_running
        assert not app.service.git_sync.running
        assert len(_remote_log(remote)) == len(before) + 1


async def test_startup_compensation_runs_missed_daily_sync(tmp_path):
    import asyncio

    app = build_app(tmp_path, [], git=GitConfig(auto_sync=True, sync_time="00:00"))
    state_path = app.service.paths.state_dir / "git-sync.json"
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        await pilot.pause()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["last_sync_trigger"] == "scheduled"
        # 临时 Vault 未配置 Remote：已尝试并暂停当日自动重试，不能标记同步成功。
        assert state["last_daily_attempt_date"] == date.today().isoformat()
        assert state["last_daily_blocked_date"] == date.today().isoformat()
        assert state.get("last_daily_success_date") != date.today().isoformat()
        assert not app._git_sync_running


async def test_reflection_status_shows_pending_inference_batch(tmp_path):
    import asyncio

    app = build_app(tmp_path, [])
    state = app.service.reflection.schedule.load()
    state["profile"]["pending_changes"] = 0
    state["profile"]["pending_inferences"] = True
    app.service.reflection.schedule.save(state)
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=30)
        app._reflection_command("profile", "status")
        await pilot.pause()
        cards = [str(card.content) for card in app.query(".card")]
        assert any(
            "0 pending changes" in text and "inference batch pending" in text
            for text in cards
        )


async def test_stale_turn_ack_does_not_clear_new_request(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._begin_ask(None)
        old_turn = app.application.conversation.active_turn_id
        app._end_ask(old_turn)
        app._begin_ask(None)
        new_turn = app.application.conversation.active_turn_id
        app._stream_buffer = "new request partial"
        rendered = []
        app._finish_on_ui(old_turn, lambda: rendered.append(True))
        assert rendered == []
        app._end_ask(old_turn)
        assert app.application.conversation.active_turn_id == new_turn
        assert app._ask_active is True
        assert app._stream_buffer == "new request partial"
        app._end_ask(new_turn)
        assert app.busy is False


async def test_ui_finalize_exception_still_releases_request(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._begin_ask(None)
        turn_id = app.application.conversation.active_turn_id

        def fail_render():
            raise RuntimeError("synthetic render failure")

        app._finish_on_ui(turn_id, fail_render)
        assert app.busy is False
        assert app.application.conversation.ask_active is False


async def test_session_switch_failure_keeps_turn_and_queues_message(tmp_path):
    app = build_app(tmp_path, [])
    gated = GatedChatModel([AssistantTurn(content="reply A"), AssistantTurn(content="reply B")])
    gated.gates.extend([threading.Event(), threading.Event()])
    app.runtime.model = gated
    session = app.runtime.ensure_session()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._start_ask("original turn")
        await _wait_ask_started(app, pilot)
        turn_id = app.application.conversation.active_turn_id

        app.run_worker(lambda: app._session_work("missing-session"), group="session",
                       thread=True, exclusive=True, exit_on_error=False)
        for _ in range(200):
            await pilot.pause()
            if not app._navigation_busy and any(
                "Session switch failed" in str(card.render()) for card in app.query(".card")
            ):
                break
        assert app.application.conversation.active_turn_id == turn_id
        assert app._ask_active is True

        widget = app.query_one("#input", Input)
        widget.value = "queued during failed switch"
        await pilot.press("enter")
        await pilot.pause()
        assert app.application.conversation.queue_snapshot().count == 1

        gated.gates[0].set()
        for _ in range(200):
            await pilot.pause()
            replies = app.query(".reply")
            if replies and "reply B" in str(replies[-1].render()):
                break
        gated.gates[1].set()
        await _wait_idle(app, pilot)
        loaded = app.application.conversation.current
        assert loaded.user_texts() == ["original turn", "queued during failed switch"]
        assert app.application.conversation.active_turn_id is None


async def test_model_load_failure_keeps_active_turn_and_queue(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    gated = GatedChatModel([AssistantTurn(content="reply A"), AssistantTurn(content="reply B")])
    gated.gates.extend([threading.Event(), threading.Event()])
    app.runtime.model = gated
    app.runtime.ensure_session()
    monkeypatch.setattr(
        "syvrio.application.settings.SettingsUseCases.model_options",
        lambda self, *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic model metadata failure")),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._start_ask("original turn")
        await _wait_ask_started(app, pilot)
        turn_id = app.application.conversation.active_turn_id

        app._command("/model")
        for _ in range(200):
            await pilot.pause()
            if any("Loading models failed" in str(card.render()) for card in app.query(".card")):
                break
        assert "synthetic model metadata failure" in str(app.query(".card")[-1].render())
        assert app.application.conversation.active_turn_id == turn_id
        assert app._ask_active is True

        widget = app.query_one("#input", Input)
        widget.value = "queued during model failure"
        await pilot.press("enter")
        await pilot.pause()
        assert app.application.conversation.queue_snapshot().count == 1
        gated.gates[0].set()
        gated.gates[1].set()
        await _wait_idle(app, pilot)
        assert app.application.conversation.current.user_texts() == [
            "original turn",
            "queued during model failure",
        ]


async def test_navigation_and_background_errors_do_not_touch_active_turn(tmp_path):
    app = build_app(tmp_path, [])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._begin_ask(None)
        turn_id = app.application.conversation.active_turn_id
        app._begin_navigation()

        app._finish_navigation_error("navigation failed")
        assert app._navigation_busy is False
        assert app._ask_active is True
        assert app.application.conversation.active_turn_id == turn_id

        app._background_error("background failed")
        assert app._ask_active is True
        assert app.application.conversation.active_turn_id == turn_id
        assert app._end_ask(turn_id) is True


async def test_generation_finished_recorded_once_and_stale_finish_keeps_queue(tmp_path, monkeypatch):
    import syvrio.app.chat_app as chat_app_module

    events = []
    monkeypatch.setattr(chat_app_module, "record_event", lambda name, *args, **kwargs: events.append(name))
    app = build_app(tmp_path, [])
    session = None
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.runtime.ensure_session()
        app._begin_ask(None)
        session = app.application.conversation.current
        turn_id = app.application.conversation.active_turn_id
        app.application.conversation.enqueue(session.id, "queued message")
        dispatched = []
        app._start_ask = lambda line, session_id=None: dispatched.append(line)

        app._finish_on_ui(turn_id, app._finish_reply, "reply", "12:00", "", session.id, "u1")
        assert events.count("generation.finished") == 1
        assert dispatched == ["queued message"]
        assert app.application.conversation.queue_snapshot().count == 0
        # 重复确认与过期确认都不得重复记录或消费队列。
        app.application.conversation.enqueue(session.id, "still queued")
        app._finish_on_ui(turn_id, app._finish_reply, "duplicate", "12:00", "", session.id, "u1")
        assert events.count("generation.finished") == 1
        assert dispatched == ["queued message"]
        assert app.application.conversation.queue_snapshot().count == 1


async def test_context_compact_and_import_failures_release_only_own_state(tmp_path, monkeypatch):
    app = build_app(tmp_path, [])
    gated = GatedChatModel([AssistantTurn(content="reply")])
    gated.gates.append(threading.Event())
    app.runtime.model = gated
    app.runtime.ensure_session()
    monkeypatch.setattr(
        "syvrio.application.settings.SettingsUseCases.context_options",
        lambda self: (_ for _ in ()).throw(RuntimeError("synthetic context failure")),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._begin_ask(None)
        turn_id = app.application.conversation.active_turn_id

        app._command("/context")
        for _ in range(200):
            await pilot.pause()
            if any("Loading context options failed" in str(card.render()) for card in app.query(".card")):
                break
        assert app._context_loading is False
        assert app.application.conversation.active_turn_id == turn_id
        assert app._ask_active is True

        app._end_ask(turn_id)
        gated.gates[0].set()

        def fail_compact(*args, **kwargs):
            raise RuntimeError("synthetic compact failure")

        monkeypatch.setattr(app.runtime, "force_compact", fail_compact)
        app._run_compact()
        for _ in range(200):
            await pilot.pause()
            if any("Compaction failed" in str(card.render()) for card in app.query(".card")):
                break
        assert app._navigation_busy is False
        assert app._ask_active is False

        def fail_import(*args, **kwargs):
            raise RuntimeError("synthetic import failure")

        monkeypatch.setattr(app.application.memory, "import_path", fail_import)
        app._start_import(tmp_path / "missing.txt", False)
        for _ in range(200):
            await pilot.pause()
            if any("Import failed" in str(card.render()) for card in app.query(".card")):
                break
        assert app._navigation_busy is False
        assert app._ask_active is False
        assert not app.busy
