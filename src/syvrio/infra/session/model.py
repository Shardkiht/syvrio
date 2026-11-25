"""会话事件的投影与呈现，独立于存储和客户端。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..llm.tokens import estimate_tokens
from ..time import parse_timestamp, serialize_timestamp, utc_now, utc_timestamp


def now_iso() -> str:
    return utc_timestamp()


def new_message_id() -> str:
    stamp = utc_now()
    return f"m-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


def validate_record(record: Any) -> None:
    # JSON 语法正确的完整记录仍可能不是有效的会话数据。
    # 不能把它误判为尾部写入中断，也不能强制转换历史数据。
    if not isinstance(record, dict) or not isinstance(record.get("type"), str):
        raise ValueError("Session record must be an object with a string type")
    for key in ("id", "at", "started_at", "title", "role", "up_to"):
        if key in record and not isinstance(record[key], str):
            raise ValueError(f"Invalid session field: {key}")
    if "original_local_date" in record:
        local = record["original_local_date"]
        if not isinstance(local, str) or date.fromisoformat(local).isoformat() != local:
            raise ValueError("Invalid original message calendar date.")
    for key in ("at", "started_at"):
        if record.get(key):
            record[key] = serialize_timestamp(parse_timestamp(record[key]))
    if record["type"] in {"message", "assistant"} and not record.get("id"):
        raise ValueError("Missing session message ID")
    if record["type"] == "message" and record.get("role") != "user":
        raise ValueError("Invalid session message role")
    if record["type"] == "summary" and "distilled_ids" not in record:
        raise ValueError("Missing session distilled IDs")
    if "content" in record and not isinstance(record["content"], str):
        if record["type"] != "assistant" or record["content"] is not None:
            raise ValueError("Invalid session content")
    if record["type"] == "message" and "content" not in record:
        raise ValueError("Missing session message content")
    if "covered_records" in record:
        count = record["covered_records"]
        if type(count) is not int or count < 0:
            raise ValueError("Invalid session summary boundary")
    if "distilled_ids" in record:
        ids = record["distilled_ids"]
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            raise ValueError("Invalid session distilled IDs")
    calls = record.get("tool_calls")
    if calls is not None and (
        not isinstance(calls, list)
        or any(
            not isinstance(call, dict) or not isinstance(call.get("name", ""), str)
            for call in calls
        )
    ):
        raise ValueError("Invalid session tool calls")


@dataclass
class Session:
    id: str
    started_at: str
    records: list[dict] = field(default_factory=list)

    def active_records(self, *, include_failures: bool = False) -> list[dict]:
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
            elif record_type in ("message", "assistant", "tool", "summary") or (
                include_failures and record_type == "turn_failed"
            ):
                active.append(record)
        return active

    @property
    def messages(self) -> list[dict]:
        return [record for record in self.active_records() if record.get("type") == "message"]

    def latest_turn_inputs(self) -> list[dict]:
        """原始用户输入以及同一回合中依次插入的消息。"""
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

    def saved_title(self) -> str:
        answer_key = None
        for record in reversed(self.records):
            if record.get("type") != "title" or not record.get("title"):
                continue
            if record.get("source") == "llm":
                if answer_key is None:
                    pair = self.title_exchange()
                    answer_key = pair[1]["id"] if pair else ""
                if answer_key != record.get("answer_id"):
                    continue
            return record["title"]
        return ""

    def title(self, width: int | None = None) -> str:
        return (self.saved_title() or "未命名会话")[:width]

    def title_exchange(self) -> tuple[dict, dict] | None:
        """首次完整问答，不计入已撤回、中断或仅有工具输出的回合。"""
        user = answer = None
        for item in self.active_records():
            kind = item.get("type")
            if kind == "message" and item.get("role") == "user":
                if not item.get("turn_root"):
                    if user and answer:
                        return user, answer
                    user = item
                answer = None
            elif user and kind == "assistant":
                answer = (
                    item
                    if not item.get("interrupted")
                    and not item.get("tool_calls")
                    and (item.get("content") or "").strip()
                    else None
                )
        return (user, answer) if user and answer else None

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
                distilled_ids.update(record["distilled_ids"])
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
