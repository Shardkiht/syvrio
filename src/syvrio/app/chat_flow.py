from __future__ import annotations

import time
from functools import partial

from textual.widgets import Input, Static

from ..application.contracts import TurnCallbacks, UndoOutcome
from ..application.conversation import QueuedLine as _QueuedLine
from ..infra.diagnostics import record_event
from .chat_widgets import ASSISTANT_NAME, ChatReply


class ChatFlowMixin:
    """LLM 请求调度与流式渲染：忙碌态解耦、全局 FIFO 排队、切走切回的流式续播。

    依赖 ChatApp 的 runtime/sessions/service、busy 三态字段与 `_begin_ask`/`_end_ask`
    等生命周期方法；提交路由规则见 `_submit_during_ask`。
    """

    # ---------- 提交与调度 ----------

    def _ask_in_current_view(self) -> bool:
        """请求的会话是否就是当前正在查看的会话（尚未绑定时按当前视图处理）。"""
        if self._ask_in_flight is None:
            return True
        return self.application.conversation.current_id == self._ask_in_flight

    def _bind_ask_session(self, session_id: str) -> None:
        # on_session 先于一切 delta/事件回调投递，这里补齐首条消息的会话绑定。
        self.application.conversation.bind_session_id(session_id)

    def _bind_ask_user_message(self, message_id: str) -> None:
        self.application.conversation.bind_user_message(message_id)

    def _submit_during_ask(self, line: str) -> None:
        submission = self.application.conversation.submit_during_ask(line)
        if submission.kind == "command":
            self._command(line)
        elif submission.kind in ("undo", "undo_busy"):
            self._after_undo_requested(submission)
        elif submission.kind == "queued":
            self._refresh_queue()
            if line.startswith("/"):
                self.notify(f"{line} will run after the current reply, before the next message.")

    def _after_undo_requested(self, submission) -> None:
        self._progress_phase = "Stopping"
        self._render_model_status()
        self.query_one("#input").focus()
        if submission.message:
            self.notify(submission.message)

    def _refresh_queue(self) -> None:
        snapshot = self.application.conversation.queue_snapshot()
        panel = self.query_one("#send-queue", Static)
        panel.display = bool(snapshot.items)
        lines = [f"Queued ({snapshot.count}) · commands before messages"]
        for item in snapshot.items[:3]:
            kind = "Command: " if item.line.startswith("/") else ""
            lines.append(f"{item.index + 1}. {kind}{item.line[:100]}")
        if snapshot.count > 3:
            lines.append(f"+{snapshot.count - 3} more")
        panel.update("\n".join(lines))

    def _stop_generation(self) -> None:
        if not self.application.conversation.request_cancel():
            return
        self._progress_phase = "Stopping"
        self._render_model_status()
        self.query_one("#input").focus()

    def _request_undo(self) -> None:
        """生成期间的 /undo：立即取消，不进入 FIFO；撤销在 Worker 退出后执行。"""
        submission = self.application.conversation.request_undo()
        if submission.kind == "undo_now":
            self._undo_message()
        else:
            self._after_undo_requested(submission)

    def _purge_queued_undo(self) -> None:
        self.application.conversation.purge_undo()
        self._refresh_queue()

    def _finish_undo(
        self,
        session_id: str | None,
        user_message_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Worker 已退出后执行撤销；不启动新请求，也不自动发送旧队列。"""
        outcome = self.application.conversation.perform_undo(
            session_id, user_message_id, error
        )
        self._apply_undo_outcome(outcome)

    def _apply_undo_outcome(self, outcome: UndoOutcome) -> None:
        """应用层已完成撤销状态变更，这里只更新视图、通知与队列接续。"""
        self._progress_phase = "Undoing"
        self._render_model_status()
        if outcome.failed:
            self.notify(f"Undo failed: {outcome.error}")
        elif outcome.error:
            self.notify(f"Request failed before undo: {outcome.error}")
        if outcome.restored is None and not outcome.failed:
            self.notify("No generated turn to undo.")

        current_id = self.application.conversation.current_id
        if outcome.restored is not None and outcome.target_session_id:
            # 按会话保存待回填，后台撤销互不覆盖；同一会话的新撤销覆盖
            # 尚未应用的旧文本（旧回合已撤销，输入框未被占用）。
            self._pending_undo_restore[outcome.target_session_id] = outcome.restored
            if current_id == outcome.target_session_id and not self._navigation_busy:
                self._flush_undo_refresh()
            elif current_id != outcome.target_session_id:
                self.notify("Generation stopped and message undone in background session")
        self._heal_current_session(outcome.target_session_id)
        self.refresh_sessions()
        self.refresh_pending()
        self.update_status()
        if outcome.paused:
            self.notify(
                f"{outcome.queued_count} queued message(s) paused after undo; "
                "they resume after your next reply."
            )
        else:
            # 撤销失败/无目标时不能把 FIFO 永久留在暂停状态；Worker 已退出，
            # 可安全接续队列（成功后 pause 分支不会走到这里）。
            if outcome.queued_count:
                self.notify(f"{outcome.queued_count} queued message(s) resumed.")

    def _apply_undo_view(self, session_id: str, text: str) -> bool:
        """目标会话就是当前视图：从磁盘重载，清除已撤销回合并回填原文。"""
        if self.application.conversation.current_id != session_id:
            return False
        try:
            self.application.conversation.reload_current_session(session_id)
        except Exception as exc:
            record_event("session.reload_failed", exc)
        self._clear_chat()
        self.build_transcript()
        self.refresh_sessions()
        self.update_status()
        widget = self.query_one("#input", Input)
        # 用户已在输入框写了新内容时不能静默覆盖；保留待回填，
        # 等之后带回填会话且输入为空时再应用。
        if widget.value.strip() and widget.value != text:
            self.notify(
                "Undo text not restored: the input has unsent text. "
                "It will be restored when you return with an empty input."
            )
            return False
        widget.value = text
        widget.cursor_position = len(text)
        widget.focus()
        return True

    def _flush_undo_refresh(self) -> None:
        """切换完成后消费当前会话对应的回填；其他会话的待回填保持不变。"""
        if not self._pending_undo_restore:
            return
        current_id = self.application.conversation.current_id
        if current_id is None:
            return
        text = self._pending_undo_restore.get(current_id)
        if text is None:
            return
        if self._apply_undo_view(current_id, text):
            self._pending_undo_restore.pop(current_id, None)

    def _finish_cancelled(
        self,
        session_id: str | None = None,
        user_message_id: str | None = None,
    ) -> None:
        if self._undo_requested:
            self._finish_undo(session_id, user_message_id)
            return
        sid = session_id or self._ask_in_flight
        if self._ask_in_current_view():
            content = self._stream_buffer + "\n\n*(generation stopped by user)*"
            if self._stream_widget is not None:
                self._finalize_stream_segment(content)
            else:
                self.add_card("Generation stopped by user")
        else:
            self.notify("Generation stopped in background session")
        self._heal_current_session(sid)
        self.refresh_sessions()
        self.refresh_pending()
        self.update_status()

    def _start_ask(self, line: str, session_id: str | None = None) -> None:
        stamp = time.strftime("%H:%M")
        # 用户气泡只渲染进它所属会话的视图；后台任务不串入当前视图。
        if session_id is None or self.application.conversation.current_id == session_id:
            self.add_user_turn(line, stamp)
        else:
            self.notify("Running queued task in background session")
        self._begin_ask(session_id)
        token = self.application.conversation.cancel_event
        self._stream_stamp = stamp
        self.run_worker(
            partial(self._ask_work, line, stamp, session_id, token),
            group="ask",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _dispatch_queue(self) -> None:
        if self.busy or not self.application.conversation.queue_snapshot().items:
            return
        item = self.application.conversation.take_next()
        if item is None:
            return
        self._refresh_queue()
        if item.line.startswith("/"):
            # 排队的命令按命令分发，不能当聊天消息发给模型；会话归属见
            # _run_queued_command——不能拿当前查看的会话顶替提交时的会话。
            self._run_queued_command(item)
            self._drain_pending_intent()
            return
        self._start_ask(item.line, item.session_id)

    def _run_queued_command(self, item: _QueuedLine) -> None:
        """排队命令绑定提交时的会话：目标不是当前会话时先切回再执行。

        切换完成后由 _after_session_switch → _drain_pending_intent 消费
        _pending_command；目标已删除或损坏时提示并跳过，继续处理后续队列。
        """
        current_id = self.application.conversation.current_id
        if current_id == item.session_id:
            self._command(item.line)
            return
        try:
            if not self.application.conversation.session_available(item.session_id):
                raise KeyError(item.session_id)
        except Exception as exc:
            record_event("queue.failed", exc)
            self.notify("Queued command skipped: session no longer available")
            return
        self.application.conversation.set_pending_command(item)
        self._switch_session(item.session_id)

    def _ui(self, fn, *args) -> None:
        try:
            self.call_from_thread(fn, *args)
        except RuntimeError:
            pass  # 界面已退出：丢弃收尾回调，避免淹没退出流程。

    def _finish_on_ui(self, turn_id: int | None, fn, *args) -> None:
        """UI 终态确认；渲染异常也要释放请求，避免输入框永久忙碌。"""
        conversation = self.application.conversation
        if turn_id is None or conversation.active_turn_id != turn_id:
            return  # 缺失或过期回调：不渲染、不释放当前请求。
        try:
            fn(*args)
        except Exception as exc:
            record_event("tui.finalize_failed", exc)
            self.notify("UI update failed; request state was released.", severity="warning")
        finally:
            if self._end_ask(turn_id):
                self._drain_pending_intent()

    def _ask_work(
        self,
        line: str,
        stamp: str,
        session_id: str | None,
        token,
    ) -> None:
        started = time.monotonic()
        callbacks = TurnCallbacks(
            on_event=lambda event: self.call_from_thread(self.add_event, event),
            on_delta=lambda delta: self.call_from_thread(self._stream_append, stamp, delta),
            on_progress=lambda phase: self.call_from_thread(self._progress_update, phase),
            on_session=lambda session: self.call_from_thread(self._bind_ask_session, session.id),
            on_user_message=lambda message_id: self.call_from_thread(
                self._bind_ask_user_message, message_id
            ),
        )
        result = self.application.conversation.run_turn(
            line, callbacks, token, session_id
        )
        if result.status == "undo":
            self._ui(self._finish_on_ui, result.turn_id, self._apply_undo_outcome, result.undo)
            return
        if result.status == "cancelled":
            self._ui(
                self._finish_on_ui,
                result.turn_id,
                self._finish_cancelled,
                result.session_id,
                result.user_message_id,
            )
            return
        if result.status == "failed":
            record_event("chat.failed", result.exception or result.error or "request failed")
            self._ui(
                self._finish_on_ui,
                result.turn_id,
                self._finish_chat_error,
                result.error or "Request failed",
                result.session_id,
                result.user_message_id,
            )
            return
        reply = result.reply
        elapsed = time.monotonic() - started
        meta = [f"{reply.steps} steps", f"{reply.estimated_tokens} tok"] if reply else []
        if reply and reply.usage.cost_usd:
            meta.append(f"${reply.usage.cost_usd:.4f}")
        meta.append(f"{elapsed:.1f}s")
        self._ui(
            self._finish_on_ui,
            result.turn_id,
            self._finish_reply,
            result.content,
            stamp,
            " · ".join(meta),
            result.session_id,
            result.user_message_id,
        )

    # ---------- 流式渲染：帧合并更新，完成/中断后收尾 ----------

    def _stream_append(self, stamp: str, delta: str) -> None:
        self._progress_phase = "Streaming"
        self._stream_buffer += delta
        if not self._ask_in_current_view():
            return  # 请求在后台会话：内容留在 buffer，切回时重放。
        chat = self._chat()
        if self._stream_widget is None:
            chat.mount(Static(f"{ASSISTANT_NAME} · {stamp}", classes="who-ai"))
            self._stream_widget = ChatReply("", classes="reply")
            chat.mount(self._stream_widget)
        if not self._stream_flush_scheduled:
            self._stream_flush_scheduled = True
            self.call_after_refresh(self._stream_flush)

    def _stream_flush(self) -> None:
        self._stream_flush_scheduled = False
        if self._stream_widget is not None:
            self._stream_widget.update(self._stream_buffer)
            self._scroll_end()

    def _finalize_stream_segment(self, content: str) -> None:
        """就地定稿当前可见的活跃段；后台会话没有挂载控件，无需处理。"""
        if self._stream_widget is not None:
            self._stream_widget.update(content)
            self._stream_widget = None
        self._stream_flush_scheduled = False

    def _replay_stream(self) -> None:
        """切回生成中的会话：磁盘重建负责已完成轮次，这里只补挂尚未落盘的活跃段。"""
        if not self._stream_buffer:
            return
        chat = self._chat()
        chat.mount(Static(f"{ASSISTANT_NAME} · {self._stream_stamp}", classes="who-ai"))
        self._stream_widget = ChatReply(self._stream_buffer, classes="reply")
        chat.mount(self._stream_widget)
        self._scroll_end()

    def add_event(self, event: str) -> None:
        if self._stream_buffer:
            # 工具事件在两轮生成之间：先就地定稿当前段（已完成段随 assistant 记录落盘），
            # 后续 delta 另起一段，否则最终回复会覆盖工具前的正文，事件顺序也会颠倒。
            self._finalize_stream_segment(self._stream_buffer)
            self._stream_buffer = ""
        memo = event.startswith(("Saved", "Corrected", "Deleted"))
        # 事件卡只进当前视图；后台请求的事件由磁盘 tool 记录在切回时重建。
        if self._ask_in_current_view():
            self._chat().mount(
                Static(f"✓ {event}" if memo else event, classes="event-card memo" if memo else "event-card")
            )
            self._scroll_end()

    # ---------- 回合收尾 ----------

    def _heal_current_session(self, session_id: str | None) -> None:
        """后台写入期间用户可能切走再切回，内存对象会落后于磁盘：完成后从磁盘刷新。"""
        if not session_id or self.application.conversation.current_id != session_id:
            return
        try:
            self.application.conversation.reload_current_session(session_id)
        except Exception as exc:
            record_event("session.reload_failed", exc)
            pass  # 会话文件异常时保留内存对象，后续操作各自报错。

    def _finish_reply(
        self,
        content: str,
        stamp: str,
        meta: str,
        session_id: str | None,
        user_message_id: str | None = None,
    ) -> None:
        if self._undo_requested:
            # 用户已要求撤销：自然完成与取消竞争时只撤销一次，不保留最终回复。
            self._finish_undo(session_id, user_message_id)
            return
        sid = session_id or self._ask_in_flight
        in_view = sid is None or self.application.conversation.current_id == sid
        if in_view:
            if self._stream_widget is not None:
                # 流式已实时渲染正文：就地定稿并补 meta/分隔线，不再重复挂载。
                self._finalize_stream_segment(content or "(empty reply)")
                chat = self._chat()
                if meta:
                    chat.mount(Static(meta, classes="meta"))
                chat.mount(Static("", classes="divider"))
                self._scroll_end()
            else:
                self.add_assistant_turn(content, stamp, meta)
        else:
            # 后台完成：不动当前视图；回复已落盘，切回时从磁盘重建。
            self.notify("Reply ready in background session")
        self._heal_current_session(sid)
        self.refresh_sessions()  # 首条消息会创建会话，侧栏需要随之出现
        self.refresh_pending()
        self.update_status()

    def _finish_chat_error(
        self,
        message: str,
        session_id: str | None = None,
        user_message_id: str | None = None,
    ) -> None:
        if self._undo_requested:
            self._finish_undo(session_id, user_message_id, error=message)
            return
        sid = session_id or self._ask_in_flight
        in_view = sid is None or self.application.conversation.current_id == sid
        if in_view:
            if self._stream_widget is not None:
                # 中途断流：保留已生成的半截并显式标注，不静默吞掉。
                interrupted = self._stream_buffer + "\n\n*(generation interrupted)*"
                self._finalize_stream_segment(interrupted)
                self._chat().mount(Static("", classes="divider"))
                self._scroll_end()
            self.add_card(message)
        else:
            self.notify(f"Background request failed: {message}")
        self._heal_current_session(sid)
        self.refresh_sessions()
        self.refresh_pending()
        self.update_status()

    def _finish_navigation_error(self, message: str, *, drop_pending: bool = False) -> None:
        """只释放本次导航状态，不触碰活动聊天请求。"""
        self.add_card(message)
        self._end_navigation()
        if drop_pending:
            item = self.application.conversation.take_pending_command()
            if item is not None:
                # 切回目标会话失败：挂起的命令失去目标上下文，丢弃而非误伤当前会话。
                self.notify("Queued command dropped: session switch failed")
        self.update_status()
        self._drain_pending_intent()

    def _background_error(self, message: str) -> None:
        """普通后台任务失败：只展示错误并恢复自身 UI 状态。"""
        self.add_card(message)
        self._release_input_if_idle()

    def _plan_is_current(self) -> bool:
        """last_plan 属于别的会话时不得污染当前视图的上下文用量显示。"""
        return self.application.conversation.plan_is_current()
