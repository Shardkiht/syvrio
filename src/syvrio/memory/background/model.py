"""常驻记忆任务使用的结构化模型调用。"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import date

from pydantic import BaseModel

from ...infra.config import Config
from ...infra.errors import SyvrioError
from ...infra.llm.client import (
    complete,
    effective_context_window,
    limit_output_tokens,
    model_metadata,
    provider_reasoning_levels,
)
from ...infra.llm.tokens import estimate_tokens
from ...infra.security.secrets import contains_secret
from ..models import ProfileSummaryResponse, ReflectionResponse
from ..temporal import resolve_relative

MAX_DREAM_OUTPUT_TOKENS = 16_000
MESSAGE_OVERHEAD = 64


def encode_payload(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class RequestBudget:
    window: int
    output: int
    minimum_output: int

    @classmethod
    def for_task(cls, settings: Config, schema: type[BaseModel]) -> RequestBudget:
        limits = (
            settings.dream
            if schema in (ProfileSummaryResponse, ReflectionResponse)
            else settings.chat
        )
        window = effective_context_window(settings.llm, limits.context_window)
        output = 4000 if schema is ProfileSummaryResponse or settings.llm.is_spark else 1800
        return cls(window, output, 2048 if settings.llm.is_spark else 256)

    def available_output(self, prompt: str, content: str) -> int:
        return self.window - estimate_tokens(prompt + content) - MESSAGE_OVERHEAD

    @property
    def reserved_output(self) -> int:
        return max(self.minimum_output, min(self.output, self.window // 4))

    def fits(self, prompt: str, payload: dict) -> bool:
        return self.available_output(prompt, encode_payload(payload)) >= self.reserved_output


def fitting_parts(text: str, fits: Callable[[str], bool]) -> Iterator[str]:
    """按完整请求的预算切分原文，优先在行末断开，每个字符都进入一个片段。"""
    start = 0
    while start < len(text):
        # 指数扩展探测范围，避免每个请求都重新编码整份剩余资料。
        low = 0
        high = min(2048, len(text) - start)
        while fits(text[start : start + high]):
            low = high
            if start + high == len(text):
                yield text[start:]
                return
            high = min(high * 2, len(text) - start)
        while low + 1 < high:
            middle = (low + high) // 2
            if fits(text[start : start + middle]):
                low = middle
            else:
                high = middle
        if not low:
            raise ValueError("Model context is too small for the background prompt and metadata.")
        end = start + low
        newline = text.rfind("\n", start, end)
        if newline >= start and fits(text[start : newline + 1]):
            end = newline + 1
        yield text[start:end]
        start = end


def dream_output_ceiling(config) -> int:
    metadata = model_metadata(config.litellm_model())
    return limit_output_tokens(metadata, MAX_DREAM_OUTPUT_TOKENS)


class DreamOutputTruncated(SyvrioError):
    def __init__(self, used_limit: int, next_limit: int):
        super().__init__("The model reached its output limit before finishing this Dream batch.")
        self.used_limit = used_limit
        self.next_limit = next_limit


def source_calendar(source: dict, text: str) -> dict:
    local = source.get("original_local_date")
    if not local:
        return {}
    day = date.fromisoformat(local)
    terms = (
        "前天",
        "昨天",
        "今天",
        "现在",
        "明天",
        "后天",
        "去年",
        "今年",
        "明年",
        "上个月",
        "这个月",
        "本月",
        "下个月",
    )
    return {
        "original_local_date": local,
        "relative_dates": {
            term: value for term in terms if term in text and (value := resolve_relative(term, day))
        },
    }


def ask(
    settings: Config,
    prompt: str,
    payload: dict,
    schema: type[BaseModel],
    *,
    output_limit: int | None = None,
    api_key: str | None = None,
):
    config = replace(settings.llm, num_retries=0)
    content = encode_payload(payload)
    limits = RequestBudget.for_task(settings, schema)
    dream = schema in (ProfileSummaryResponse, ReflectionResponse)
    available = limits.available_output(prompt, content)
    if dream:
        available = min(available, dream_output_ceiling(config))
        if (
            output_limit
            and not config.is_spark
            and config.reasoning_effort
            not in {
                "low",
                "minimal",
                "none",
                "disabled",
            }
        ):
            if "low" in (provider_reasoning_levels(config) or ()):
                config.reasoning_effort = "low"
    budget = min(output_limit or limits.output, available)
    if budget < limits.minimum_output:
        raise ValueError("Background task exceeds the model context window.")
    response = complete(
        config,
        [{"role": "system", "content": prompt}, {"role": "user", "content": content}],
        response_format={"type": "json_object"},
        max_tokens=budget,
        api_key=api_key,
    )
    choice = response.choices[0]
    if dream and getattr(choice, "finish_reason", None) == "length":
        # 模型可能在生成 JSON 前就将输出额度全部用于思考。
        raise DreamOutputTruncated(budget, min(budget * 4, available))
    text = (choice.message.content or "").strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.partition("\n")[2].removesuffix("```").strip()
    if contains_secret(text):
        raise ValueError("Background response contains credentials.")
    return schema.model_validate_json(text)
