"""常驻记忆任务使用的结构化模型调用。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

from pydantic import BaseModel

from ..errors import SyvrioError
from ..infra.config import Config
from ..infra.secrets import contains_secret
from ..infra.tokens import estimate_tokens
from ..llm.client import (
    complete,
    effective_context_window,
    model_metadata,
    provider_reasoning_levels,
)
from ..memory.models import ProfileSummaryResponse, ReflectionResponse
from ..memory.temporal import resolve_relative

MAX_DREAM_OUTPUT_TOKENS = 16_000
MESSAGE_OVERHEAD = 64


def encode_payload(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def dream_output_ceiling(config) -> int:
    metadata = model_metadata(config.litellm_model())
    maximum = metadata.get("max_output_tokens") or metadata.get("max_tokens")
    if isinstance(maximum, int) and maximum > 0:
        return min(MAX_DREAM_OUTPUT_TOKENS, maximum)
    return MAX_DREAM_OUTPUT_TOKENS


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
):
    config = replace(settings.llm, num_retries=0)
    content = encode_payload(payload)
    default_limit = 4000 if schema is ProfileSummaryResponse or config.is_spark else 1800
    dream = schema in (ProfileSummaryResponse, ReflectionResponse)
    budget_settings = settings.dream if dream else settings.chat
    window = effective_context_window(config, budget_settings.context_window)
    available = window - estimate_tokens(prompt + content) - MESSAGE_OVERHEAD
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
    budget = min(output_limit or default_limit, available)
    if budget < (2048 if config.is_spark else 256):
        raise ValueError("Background task exceeds the model context window.")
    response = complete(
        config,
        [{"role": "system", "content": prompt}, {"role": "user", "content": content}],
        response_format={"type": "json_object"},
        max_tokens=budget,
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
