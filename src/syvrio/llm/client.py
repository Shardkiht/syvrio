from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from threading import Event
from typing import Protocol
from urllib.parse import urlsplit

from ..infra.config import LLMConfig
from ..infra.latency import mark_model, track_completion


class ChatModelError(RuntimeError):
    pass


class GenerationCancelled(ChatModelError):
    """The user stopped the current generation; never retry it."""


_MODEL_CACHE: dict[tuple[str, str, bytes], tuple[float, list[str]]] = {}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cost_usd += other.cost_usd


@dataclass
class AssistantTurn:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    reasoning_content: str = ""
    reasoning_fragments: list[dict] = field(default_factory=list)
    reasoning_source: str = ""
    reasoning_seconds: float | None = None


def _reasoning_fields(message) -> tuple[str, dict]:
    text = _field(message, "reasoning_content")
    alias = _field(message, "reasoning")
    metadata = {}
    for key, value in (
        ("reasoning", alias),
        ("reasoning_details", _field(message, "reasoning_details")),
        ("thinking_blocks", _field(message, "thinking_blocks")),
    ):
        if isinstance(value, (list, dict)) and value:
            metadata[key] = value
    return (text if isinstance(text, str) and text else alias if isinstance(alias, str) else ""), metadata


def _reasoning_route(config: LLMConfig) -> tuple[str, str]:
    """Only verified request shapes can receive session reasoning."""
    provider = config.provider.strip().lower()
    endpoint = (config.base_url or "").rstrip("/")
    url = urlsplit(endpoint)
    if provider == "deepseek" and (
        not endpoint or (url.scheme == "https" and url.hostname == "api.deepseek.com")
    ):
        policy = "history"
    elif (
        provider == "dashscope"
        and url.scheme == "https"
        and url.hostname in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"}
        and config.litellm_model() == "dashscope/deepseek-v4.1-flash-1017"
    ):
        # Limit the compatible endpoint to the active tool chain.
        policy = "turn"
    else:
        return "", ""
    source = hashlib.sha256(f"{provider}\n{endpoint}\n{config.litellm_model()}".encode()).hexdigest()
    return source, policy


def _chat_history(config: LLMConfig, messages: list[dict]) -> tuple[list[dict], list[dict], str]:
    source, policy = _reasoning_route(config)
    last_user = max((i for i, msg in enumerate(messages)
                     if msg["role"] == "user" and not msg.get("_tool_wrap_up")), default=-1)
    private = {"reasoning_content", "reasoning_source", "reasoning", "reasoning_details",
               "thinking_blocks", "_tool_wrap_up"}
    clean = [
        {k: v for k, v in msg.items() if k not in private}
        for msg in messages
    ]
    primary = [
        {**msg, "reasoning_content": original["reasoning_content"]}
        if source and msg["role"] == "assistant"
        and original.get("reasoning_source") == source and original.get("reasoning_content")
        and (policy == "history" or index > last_user)
        else msg
        for index, (msg, original) in enumerate(zip(clean, messages, strict=True))
    ]
    return primary, clean, source


def _litellm():
    import os

    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    import litellm

    litellm.suppress_debug_info = True
    litellm.telemetry = False
    return litellm


def complete(
    config: LLMConfig,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    response_format: dict | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    fallback_messages: list[dict] | None = None,
):
    """统一的 LiteLLM 调用入口；provider/base_url/retry/fallback 都由配置决定。"""
    litellm = _litellm()
    kwargs: dict = {
        "model": config.litellm_model(),
        "messages": messages,
        "api_key": config.api_key(),
        "timeout": config.timeout,
        "num_retries": config.num_retries,
        "temperature": config.temperature if temperature is None else temperature,
    }
    if config.base_url:
        kwargs["api_base"] = config.base_url
    if config.reasoning_effort:
        kwargs["reasoning_effort"] = config.reasoning_effort
    if tools:
        kwargs["tools"] = tools
    if response_format:
        kwargs["response_format"] = response_format
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if config.fallbacks:
        kwargs["fallbacks"] = config.fallbacks
    _adapt_spark_params(config, kwargs)
    _strip_fallback_reasoning(kwargs, fallback_messages)
    mark_model("request")
    with track_completion():
        return litellm.completion(**kwargs)


def _adapt_spark_params(config: LLMConfig, kwargs: dict) -> None:
    if not config.is_spark:
        return
    # MaaS 固定采样参数，thinking 不等于 OpenAI reasoning_effort；JSON 由提示词约束。
    for name in ("temperature", "reasoning_effort", "response_format", "stream_options"):
        kwargs.pop(name, None)
    if config.spark_thinking:
        kwargs["extra_body"] = {"thinking": {"type": config.spark_thinking}}
        if kwargs.get("fallbacks"):
            # LiteLLM inherits extra_body on fallback requests unless explicitly overridden.
            kwargs["fallbacks"] = [
                {"model": model, "extra_body": None} for model in kwargs["fallbacks"]
            ]
    if "max_tokens" in kwargs:
        kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")


def _strip_fallback_reasoning(kwargs: dict, messages: list[dict] | None) -> None:
    if messages is not None and kwargs.get("fallbacks"):
        kwargs["fallbacks"] = [
            {**(item if isinstance(item, dict) else {"model": item}), "messages": messages}
            for item in kwargs["fallbacks"]
        ]


def _primary_response(response, fallbacks: Sequence[str]) -> bool:
    if not fallbacks:
        return True
    hidden = _field(response, "_hidden_params") or {}
    headers = hidden.get("additional_headers") or {}
    # A missing provenance marker is ambiguous when fallback routing is enabled.
    return headers.get("x-litellm-attempted-fallbacks") in (0, "0")


def embed_texts(config: LLMConfig, texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """调用 embedding 接口；返回顺序与输入一致。"""
    litellm = _litellm()
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        kwargs: dict = {
            "model": config.litellm_model(),
            "input": batch,
            "api_key": config.api_key(),
            "timeout": config.timeout,
            "num_retries": config.num_retries,
        }
        if config.base_url:
            kwargs["api_base"] = config.base_url
        response = litellm.embedding(**kwargs)
        items = _field(response, "data") or []
        vectors.extend(
            [float(value) for value in _field(item, "embedding") or []] for item in items
        )
    return vectors


class LiteLLMEmbedder:
    """API 后端 Embedder；满足 retrieval.Embedder 协议，不反向依赖 retrieval。"""

    def __init__(self, config: LLMConfig, batch_size: int = 32):
        self.config = config
        self.batch_size = batch_size

    @property
    def model_id(self) -> str:
        return self.config.litellm_model()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return embed_texts(self.config, list(texts), batch_size=self.batch_size)

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


def _field(value, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


@lru_cache(maxsize=128)
def model_metadata(model: str) -> dict:
    """读取 LiteLLM 本地模型元数据；未知模型返回空字典。"""
    litellm = _litellm()
    try:
        return dict(litellm.get_model_info(model))
    except Exception:
        return {}


@lru_cache(maxsize=128)
def supported_params(model: str) -> tuple[str, ...]:
    """读取 LiteLLM 认为该模型支持的 OpenAI 参数。"""
    if model.startswith("openai/spark-"):
        return ("tools", "tool_choice", "stream", "max_tokens", "max_completion_tokens")
    # 自定义 OpenAI 兼容端点统一用 openai/ 前缀；LiteLLM 对未知 id 会回退成
    # 通用 OpenAI 参数表，不能据此判断供应商真的支持 reasoning_effort 等能力。
    if model.startswith("openai/") and "/" in model[len("openai/"):]:
        return ()
    litellm = _litellm()
    try:
        return tuple(litellm.get_supported_openai_params(model=model) or ())
    except Exception:
        return ()


def warm_model_metadata(model: str) -> None:
    """预热 LiteLLM 导入与当前模型元数据；纯本地读取，不访问网络或凭据。"""
    try:
        model_metadata(model)
        supported_params(model)
    except Exception:
        # 预热只影响首次打开速度，失败留到实际使用时再报错。
        pass


def fetch_provider_models(
    config: LLMConfig,
    timeout: float = 5.0,
    ttl: float = 12 * 3600,
    refresh: bool = False,
    *,
    api_key: str | None = None,
) -> list[str]:
    """向 OpenAI 兼容端点请求真实模型列表；带进程内 12 小时 TTL 缓存。"""
    api_key = api_key or config.api_key()
    key = _model_cache_key(config, api_key)
    now = time.monotonic()
    cached = _MODEL_CACHE.get(key)
    if cached and not refresh:
        max_age = ttl if cached[1] else 30.0
        if now - cached[0] < max_age:
            return list(cached[1])

    models = _fetch_provider_models_uncached(config, timeout, api_key)
    _MODEL_CACHE[key] = (now, models)
    return list(models)


def _model_cache_key(config: LLMConfig, api_key: str | None) -> tuple[str, str, bytes]:
    return config.provider, config.base_url, hashlib.sha256((api_key or "").encode()).digest()


def cached_provider_models(config: LLMConfig) -> list[str] | None:
    """返回当前进程里最近一次成功获取的模型列表，不发起网络请求。"""
    cached = _MODEL_CACHE.get(_model_cache_key(config, config.api_key()))
    return list(cached[1]) if cached and cached[1] else None


def clear_model_cache() -> None:
    _MODEL_CACHE.clear()


def _fetch_provider_models_uncached(config: LLMConfig, timeout: float, api_key: str | None) -> list[str]:
    if not config.base_url or (not api_key and config.requires_api_key()):
        return []
    try:
        from openai import OpenAI

        with OpenAI(api_key=api_key or "local", base_url=config.base_url, timeout=timeout,
                    max_retries=0) as client:
            return list(dict.fromkeys(
                model.id for model in client.models.list().data if model.id and model.id.strip()
            ))
    except Exception:
        return []


def _usage(response, litellm) -> Usage:
    usage = _field(response, "usage")
    if usage is None:
        return Usage()
    cost = 0.0
    try:
        cost = float(litellm.completion_cost(completion_response=response) or 0.0)
    except Exception:
        cost = 0.0
    return Usage(
        prompt_tokens=int(_field(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(_field(usage, "completion_tokens", 0) or 0),
        cost_usd=cost,
    )


def _tool_calls(message) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, call in enumerate(_field(message, "tool_calls", None) or []):
        function = _field(call, "function")
        raw_arguments = _field(function, "arguments", "{}")
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError as exc:
                raise ChatModelError("Tool call returned malformed JSON arguments.") from exc
        else:
            arguments = raw_arguments
        name = _field(function, "name")
        if not isinstance(arguments, dict) or not name:
            raise ChatModelError("Tool call is missing a name or object arguments.")
        calls.append(
            ToolCall(
                id=_field(call, "id") or f"call-{index}",
                name=name,
                arguments=arguments,
            )
        )
    return calls


class ChatModel(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> AssistantTurn: ...


class LiteLLMChatModel:
    def __init__(self, config: LLMConfig):
        self.config = config

    def chat(
        self, messages: list[dict], tools: list[dict], on_delta: Callable[[str], None] | None = None,
        cancel: Event | None = None, *, on_reasoning: Callable[[str], None] | None = None,
    ) -> AssistantTurn:
        if self.config.requires_api_key() and not self.config.api_key():
            raise ChatModelError(
                f"LLM API key is not configured. Set {self.config.api_key_env} "
                "or run syvrio setup to save it to system credentials."
            )
        litellm = _litellm()
        primary, clean, source = _chat_history(self.config, messages)
        fallback_messages = clean if primary != clean else None
        source = source if tools else ""
        if on_delta is None and on_reasoning is None:
            kwargs = {"fallback_messages": fallback_messages} if fallback_messages is not None else {}
            response = complete(self.config, primary, tools=tools or None, **kwargs)
            choice = response.choices[0]
            message = choice.message
            reasoning, metadata = _reasoning_fields(message)
            calls = _tool_calls(message)
            if calls and _field(choice, "finish_reason") in {"length", "content_filter"}:
                raise ChatModelError("Model stopped before its tool calls completed.")
            return AssistantTurn(
                content=_field(message, "content") or "",
                tool_calls=calls,
                usage=_usage(response, litellm),
                reasoning_content=reasoning,
                reasoning_fragments=[metadata] if metadata else [],
                reasoning_source=source if reasoning and _primary_response(response, self.config.fallbacks) else "",
            )
        parser = _StreamAccumulator(on_delta, litellm, on_reasoning, source)
        return self._chat_stream(primary, tools or None, parser, litellm, cancel, fallback_messages)

    def _chat_stream(self, messages, tools, parser, litellm, cancel, fallback_messages) -> AssistantTurn:
        """流式：文本 delta 实时回调，tool_calls 增量累积，结束时返回聚合结果。"""
        kwargs: dict = {
            "model": self.config.litellm_model(),
            "messages": messages,
            "api_key": self.config.api_key(),
            "timeout": self.config.timeout,
            "num_retries": self.config.num_retries,
            "temperature": self.config.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.config.base_url:
            kwargs["api_base"] = self.config.base_url
        if self.config.reasoning_effort:
            kwargs["reasoning_effort"] = self.config.reasoning_effort
        if tools:
            kwargs["tools"] = tools
        if self.config.fallbacks:
            kwargs["fallbacks"] = self.config.fallbacks
        _adapt_spark_params(self.config, kwargs)
        _strip_fallback_reasoning(kwargs, fallback_messages)

        if cancel is not None:
            return asyncio.run(self._cancellable_stream(kwargs, parser, litellm, cancel))
        mark_model("request")
        stream = litellm.completion(**kwargs)
        if not _primary_response(stream, self.config.fallbacks):
            parser.source = ""
        mark_model("stream_open")
        try:
            for chunk in stream:
                parser.feed(chunk)
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        return parser.finish()

    async def _cancellable_stream(self, kwargs, parser, litellm, cancel: Event) -> AssistantTurn:
        # Async transport cancellation also interrupts waiting for the first token.
        async def consume() -> AssistantTurn:
            mark_model("request")
            stream = await litellm.acompletion(**kwargs)
            if not _primary_response(stream, self.config.fallbacks):
                parser.source = ""
            mark_model("stream_open")
            try:
                async for chunk in stream:
                    if cancel.is_set():
                        raise GenerationCancelled("Stopped by user")
                    parser.feed(chunk)
                return parser.finish()
            finally:
                await stream.aclose()

        if cancel.is_set():
            raise GenerationCancelled("Stopped by user")
        task = asyncio.create_task(consume())
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.05)
                if cancel.is_set():
                    raise GenerationCancelled("Stopped by user")
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class _StreamAccumulator:
    def __init__(self, on_delta, litellm, on_reasoning=None, source: str = "") -> None:
        self.on_delta = on_delta
        self.on_reasoning = on_reasoning
        self.source = source
        self.litellm = litellm
        self.content_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.reasoning_fragments: list[dict] = []
        self.reasoning_started: float | None = None
        self.reasoning_seconds: float | None = None
        self.finish_reason: str | None = None
        self.tool_acc: dict[int, dict] = {}
        self.tool_indices: dict[str, int] = {}
        self.usage = Usage()

    def feed(self, chunk) -> None:
        mark_model("first_chunk")
        chunk_usage = _field(chunk, "usage")
        if chunk_usage is not None:
            try:
                cost = float(self.litellm.completion_cost(completion_response=chunk) or 0.0)
            except Exception:
                cost = 0.0
            self.usage = Usage(
                prompt_tokens=int(_field(chunk_usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(_field(chunk_usage, "completion_tokens", 0) or 0),
                cost_usd=cost,
            )
        choices = _field(chunk, "choices") or []
        if not choices:
            return
        self.finish_reason = _field(choices[0], "finish_reason") or self.finish_reason
        delta = _field(choices[0], "delta")
        if delta is None:
            return
        reasoning, metadata = _reasoning_fields(delta)
        if metadata:
            self.reasoning_fragments.append(metadata)
        if reasoning:
            mark_model("first_reasoning")
            if self.reasoning_started is None:
                self.reasoning_started = time.monotonic()
            self.reasoning_parts.append(reasoning)
            if self.on_reasoning is not None:
                self.on_reasoning(reasoning)
        text = _field(delta, "content") or ""
        tool_calls = _field(delta, "tool_calls") or []
        if text.strip() or tool_calls or _field(choices[0], "finish_reason"):
            self._finish_reasoning()
        if text:
            mark_model("first_text")
            self.content_parts.append(text)
            if self.on_delta is not None:
                self.on_delta(text)
        if tool_calls:
            mark_model("first_tool")
        for tc in tool_calls:
            index = _field(tc, "index")
            tc_id = _field(tc, "id")
            if index is None:
                if tc_id:
                    index = self.tool_indices.get(tc_id)
                    if index is None:
                        index = max(self.tool_acc, default=-1) + 1
                elif len(tool_calls) == 1 and len(self.tool_acc) <= 1:
                    index = next(iter(self.tool_acc), 0)
                else:
                    raise ChatModelError("Streaming tool fragment is missing its index and ID.")
            if type(index) is not int or index < 0:
                raise ChatModelError("Streaming tool call has an invalid index.")
            acc = self.tool_acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if tc_id:
                if acc["id"] not in ("", tc_id) or self.tool_indices.get(tc_id, index) != index:
                    raise ChatModelError("Streaming tool call changed its identity.")
                acc["id"] = tc_id
                self.tool_indices[tc_id] = index
            function = _field(tc, "function")
            if function is not None:
                name = _field(function, "name")
                if name:
                    acc["name"] = name
                arguments = _field(function, "arguments")
                if arguments is not None and arguments != "":
                    previous = acc["arguments"]
                    if isinstance(arguments, str) and isinstance(previous, str):
                        acc["arguments"] += arguments
                    elif isinstance(arguments, dict) and previous == "":
                        acc["arguments"] = arguments
                    else:
                        raise ChatModelError("Streaming tool call returned inconsistent arguments.")

    def _finish_reasoning(self) -> None:
        if self.reasoning_started is not None and self.reasoning_seconds is None:
            self.reasoning_seconds = time.monotonic() - self.reasoning_started

    def finish(self) -> AssistantTurn:
        if self.tool_acc and self.finish_reason in {"length", "content_filter"}:
            raise ChatModelError("Model stopped before its tool calls completed.")
        tool_calls: list[ToolCall] = []
        for _, acc in sorted(self.tool_acc.items()):
            raw = acc["arguments"]
            try:
                arguments = raw if isinstance(raw, dict) else json.loads(raw or "{}")
            except json.JSONDecodeError as exc:
                raise ChatModelError("Streaming tool call returned malformed JSON arguments.") from exc
            if not isinstance(arguments, dict) or not acc["name"]:
                raise ChatModelError("Streaming tool call is missing a name or object arguments.")
            tool_calls.append(
                ToolCall(id=acc["id"] or f"call-{len(tool_calls)}", name=acc["name"], arguments=arguments)
            )
        self._finish_reasoning()
        return AssistantTurn(
            content="".join(self.content_parts), tool_calls=tool_calls, usage=self.usage,
            reasoning_content="".join(self.reasoning_parts), reasoning_fragments=self.reasoning_fragments,
            reasoning_source=self.source if self.reasoning_parts else "", reasoning_seconds=self.reasoning_seconds,
        )
