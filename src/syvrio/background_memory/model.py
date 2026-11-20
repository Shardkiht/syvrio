"""Structured model calls used by resident memory tasks."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

from pydantic import BaseModel

from ..infra.secrets import contains_secret
from ..infra.tokens import estimate_tokens
from ..llm.client import complete
from ..memory.models import ProfileSummaryResponse
from ..memory.temporal import resolve_relative


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


def ask(service, prompt: str, payload: dict, schema: type[BaseModel]):
    config = replace(service.config.llm, num_retries=0)
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    limit = 4000 if schema is ProfileSummaryResponse or config.is_spark else 1800
    budget = min(limit, service.config.chat.context_window - estimate_tokens(prompt + content) - 64)
    if budget < (2048 if config.is_spark else 256):
        raise ValueError("Background task exceeds the model context window.")
    response = complete(
        config,
        [{"role": "system", "content": prompt}, {"role": "user", "content": content}],
        response_format={"type": "json_object"},
        max_tokens=budget,
    )
    text = (response.choices[0].message.content or "").strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.partition("\n")[2].removesuffix("```").strip()
    if contains_secret(text):
        raise ValueError("Background response contains credentials.")
    return schema.model_validate_json(text)
