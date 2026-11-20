"""Session event projection and rendering, independent of storage and clients."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path

from ..infra.tokens import estimate_tokens
from ..time import parse_timestamp, serialize_timestamp, utc_now, utc_timestamp


def now_iso() -> str:
    return utc_timestamp()


def new_message_id() -> str:
    stamp = utc_now()
    return f"m-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


@dataclass
class Session:
    id: str
    started_at: str
    path: Path
    records: list[dict] = field(default_factory=list)
    damaged_tail_line: int | None = None

    def active_records(self) -> list[dict]:
        """按 undo 事件投影有效会话；撤销同时令其后的摘要失效。"""
        active: list[dict] = []
        for record in self.records:
            record_type = record.get("type")
            if record_type == "undo":
                target_id = record.get("message_id")
                target = next(
                    (
                        index
                        for index, item in enumerate(active)
                        if item.get("id") == target_id
                        and item.get("type") == "message"
                        and item.get("role") == "user"
                    ),
                    None,
                )
                if target is not None:
                    del active[target:]
            elif record_type in ("message", "assistant", "tool", "summary"):
                active.append(record)
        return active

    @property
    def messages(self) -> list[dict]:
        return [record for record in self.active_records() if record.get("type") == "message"]

    def latest_turn_inputs(self) -> list[dict]:
        """Original user input followed by any messages inserted in that same turn."""
        users = [record for record in self.messages if record.get("role") == "user"]
        if not users:
            return []
        index = len(users) - 1
        root = users[index].get("turn_root") or users[index]["id"]
        while index > 0 and users[index].get("turn_root") == root:
            index -= 1
        return users[index:] if users[index].get("id") == root else []

    @property
    def updated_at(self) -> str:
        stamps = [record["at"] for record in self.records if record.get("at")]
        return stamps[-1] if stamps else self.started_at

    @property
    def last_message_at(self) -> str:
        """最后一条有效消息的时间；重命名/摘要/沉淀等控制记录不得改变会话顺序。"""
        for record in reversed(self.active_records()):
            if record.get("type") == "message" and record.get("at"):
                return str(record["at"])
        return self.started_at

    def user_texts(self) -> list[str]:
        return [
            record["content"]
            for record in self.active_records()
            if record.get("type") == "message" and record.get("role") == "user"
        ]

    def last_user_message_id(self) -> str | None:
        for record in reversed(self.active_records()):
            if record.get("type") == "message" and record.get("role") == "user":
                return record.get("id")
        return None

    def title(self, width: int = 40) -> str:
        for record in reversed(self.records):
            if record.get("type") == "title":
                return record.get("title", "")[:width]
        texts = self.user_texts()
        if not texts:
            return "(空会话)"
        title = texts[0].replace("\n", " ").strip()
        return title[:width] + ("…" if len(title) > width else "")

    def latest_summary(self) -> dict | None:
        for record in reversed(self.active_records()):
            if record.get("type") == "summary":
                return record
        return None

    def summary_text(self) -> str:
        summary = self.latest_summary()
        return summary.get("content", "") if summary else ""

    def covered_records(self) -> int:
        summary = self.latest_summary()
        return int(summary.get("covered_records", 0)) if summary else 0

    def records_after_summary(self) -> list[dict]:
        active_ids = {id(record) for record in self.active_records()}
        return [
            record
            for record in self.records[self.covered_records() :]
            if id(record) in active_ids and record.get("type") in ("message", "assistant", "tool")
        ]

    def needs_consolidation(self) -> bool:
        """以沉淀游标为准：游标后还有用户消息就需要补记。

        正常退出/补记成功会推进游标；异常退出、抽取失败或恢复后再次崩溃时
        游标不动，因此不依赖 closed/consolidated 标记的历史顺序。
        """
        return bool(self.user_texts_after_distilled())

    def user_texts_after_summary(self) -> list[str]:
        return [
            record["content"]
            for record in self.records_after_summary()
            if record.get("type") == "message" and record.get("role") == "user"
        ]

    def undistilled_messages(self) -> list[dict]:
        positions = {
            record["id"]: index for index, record in enumerate(self.records) if record.get("id")
        }
        covered = 0
        distilled_ids: set[str] = set()
        # 沉淀是不可由 undo 撤回的写入结果，确认信息须读取原始流而非有效会话投影。
        for record in self.records:
            if record.get("type") == "distilled":
                covered = max(covered, positions.get(record.get("up_to"), -1) + 1)
            elif record.get("type") == "summary":
                if "distilled_ids" in record:
                    distilled_ids.update(record["distilled_ids"])
                else:
                    # 旧摘要没有逐消息确认信息，沿用原有边界；新摘要的空列表不代表已沉淀。
                    covered = max(covered, int(record.get("covered_records", 0)))
        active_ids = {id(record) for record in self.active_records()}
        return [
            record
            for record in self.records[covered:]
            if id(record) in active_ids
            and record.get("type") == "message"
            and record.get("role") == "user"
            and record.get("id") not in distilled_ids
        ]

    def user_texts_after_distilled(self) -> list[str]:
        return [record["content"] for record in self.undistilled_messages()]

    def distillation_texts(self) -> list[str]:
        return [dated_message(record) for record in self.undistilled_messages()]

    def estimated_tokens(self) -> int:
        return sum(
            estimate_tokens(str(record.get("content") or ""))
            for record in self.active_records()
            if record.get("type") in ("message", "assistant", "summary")
        )


def dated_message(record: dict) -> str:
    raw = record.get("at")
    if not raw:
        return record.get("content", "")
    stamp = serialize_timestamp(parse_timestamp(raw))
    local = record.get("original_local_date")
    calendar = f"；原始本地日期：{local}" if local else ""
    return f"[归档时间（UTC）：{stamp}{calendar}]\n{record.get('content', '')}"


def record_label(record: dict) -> str:
    record_type = record.get("type")
    if record_type == "message":
        return f"用户：{record.get('content', '')}"
    if record_type == "assistant":
        content = (record.get("content") or "").strip()
        tools = [call.get("name", "") for call in record.get("tool_calls") or []]
        suffix = f"（调用工具：{', '.join(tools)}）" if tools else ""
        return f"助手：{content}{suffix}"
    if record_type == "tool":
        return f"工具 {record.get('name')}：{record.get('content', '')}"
    if record_type == "summary":
        return f"此前摘要：{record.get('content', '')}"
    return ""


def transcript(records: list[dict]) -> str:
    return "\n".join(label for label in map(record_label, records) if label)
