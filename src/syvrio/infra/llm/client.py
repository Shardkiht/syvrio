from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from functools import lru_cache
from threading import Event, Lock
from typing import Protocol
from urllib.parse import urlsplit

from ..config import SPARK_THINKING_MODES, LLMConfig
from ..errors import SyvrioError
from ..observability.latency import mark_model, track_completion


class ChatModelError(SyvrioError, RuntimeError):
    pass


class GenerationCancelled(ChatModelError):
    """用户主动停止了本次生成，不应重试。"""


ModelCatalog = dict[str, tuple[str, ...] | None]
_MODEL_CACHE: dict[tuple[str, str, bytes], tuple[float, ModelCatalog]] = {}
_MODEL_FETCH_LOCKS: dict[tuple[str, str, bytes], Lock] = {}
_MODEL_LOCK = Lock()


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
    return (
        text if isinstance(text, str) and text else alias if isinstance(alias, str) else ""
    ), metadata


def _reasoning_route(config: LLMConfig) -> tuple[str, str]:
    """只有已验证支持相应请求格式的接口才能接收会话思考内容。"""
    provider = config.provider.strip().lower()
    endpoint = (config.base_url or "").rstrip("/")
    url = urlsplit(endpoint)
    if provider == "deepseek" and (
        not endpoint or (url.scheme == "https" and url.hostname == "api.deepseek.com")
    ):
        policy = "history"
    elif config.uses_qianwen_api:
        model = config.model.removeprefix("openai/")
        if model in {"qwen4-max", "qwen3.8-max", "qwen3.8-max-0902"}:
            policy = "history"
        elif model == "glm-5.3":
            # GLM 默认清除之前回合的思考内容，但保留当前工具调用链。
            policy = "turn"
        else:
            return "", ""
    elif (
        provider == "dashscope"
        and url.scheme == "https"
        and url.hostname in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"}
        and config.litellm_model() == "dashscope/deepseek-v4.1-flash-1017"
    ):
        # 对兼容接口仅发送当前活动工具调用链的思考内容。
        policy = "turn"
    else:
        return "", ""
    source = hashlib.sha256(
        f"{provider}\n{endpoint}\n{config.litellm_model()}".encode()
    ).hexdigest()
    return source, policy


def chat_history(config: LLMConfig | None, messages: list[dict]) -> tuple[list[dict], str]:
    """预算估算和实际发送共用同一套模型历史重放规则。"""
    source, policy = _reasoning_route(config) if config else ("", "")
    last_user = max(
        (
            i
            for i, msg in enumerate(messages)
            if msg["role"] == "user" and not msg.get("_tool_wrap_up")
        ),
        default=-1,
    )
    private = {
        "reasoning_content",
        "reasoning_source",
        "reasoning",
        "reasoning_details",
        "thinking_blocks",
        "_tool_wrap_up",
    }
    clean = [{k: v for k, v in msg.items() if k not in private} for msg in messages]
    primary = [
        {**msg, "reasoning_content": original["reasoning_content"]}
        if source
        and msg["role"] == "assistant"
        and original.get("reasoning_source") == source
        and original.get("reasoning_content")
        and (policy == "history" or index > last_user)
        else msg
        for index, (msg, original) in enumerate(zip(clean, messages, strict=True))
    ]
    return primary, source


def _litellm():

    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    import litellm

    litellm.suppress_debug_info = True
    litellm.telemetry = False
    return litellm


def _request_api_key(config: LLMConfig) -> str:
    key = config.api_key()
    if key:
        return key
    if config.requires_api_key():
        raise ChatModelError(
            "LLM API key is not configured. Save it in Settings or run syvrio setup."
        )
    # 匿名本地模型显式传入占位值，避免 SDK 自动读取进程中的供应商密钥。
    return "local"


def complete(
    config: LLMConfig,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    response_format: dict | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
):
    """统一的 LiteLLM 调用入口；provider/base_url/retry 都由配置决定。"""
    api_key = _request_api_key(config)
    litellm = _litellm()
    kwargs: dict = {
        "model": config.litellm_model(),
        "messages": messages,
        "api_key": api_key,
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
    _adapt_spark_params(config, kwargs)
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
    if "max_tokens" in kwargs:
        kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")


def embed_texts(config: LLMConfig, texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """调用 embedding 接口；返回顺序与输入一致。"""
    if not texts:
        return []
    api_key = _request_api_key(config)
    litellm = _litellm()
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        kwargs: dict = {
            "model": config.litellm_model(),
            "input": batch,
            "api_key": api_key,
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


def effective_context_window(llm: LLMConfig, configured: int) -> int:
    """按模型声明的输入容量约束配置的上下文预算。"""
    try:
        maximum = int(model_metadata(llm.litellm_model()).get("max_input_tokens") or 0)
    except (ValueError, TypeError):
        maximum = 0
    return min(configured, maximum) if maximum > 0 else configured


def limit_output_tokens(metadata: dict, requested: int) -> int:
    """按模型声明的生成上限约束本次请求，兼容旧元数据的 max_tokens 字段。"""
    for key in ("max_output_tokens", "max_tokens"):
        try:
            limit = int(metadata.get(key))
        except (TypeError, ValueError):
            continue
        if limit > 0:
            return min(limit, requested)
    return requested


@lru_cache(maxsize=128)
def supported_params(model: str) -> tuple[str, ...]:
    """读取 LiteLLM 认为该模型支持的 OpenAI 参数。"""
    if model.startswith("openai/spark-"):
        return ("tools", "tool_choice", "stream", "max_tokens", "max_completion_tokens")
    # 自定义 OpenAI 兼容端点统一用 openai/ 前缀；LiteLLM 对未知 id 会回退成
    # 通用 OpenAI 参数表，不能据此判断供应商真的支持 reasoning_effort 等能力。
    if model.startswith("openai/") and "/" in model[len("openai/") :]:
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
    return list(_provider_catalog(config, timeout, ttl, refresh, api_key))


def provider_reasoning_levels(
    config: LLMConfig, *, api_key: str | None = None
) -> tuple[str, ...] | None:
    """返回当前模型声明的可选档位，未提供列表时返回 None。"""
    levels = _provider_catalog(config, timeout=3.0, api_key=api_key).get(config.model)
    if (
        levels is None
        and config.uses_qianwen_api
        and config.model.removeprefix("openai/") == "glm-5.3"
    ):
        return ("low", "high", "max")
    return levels


def reasoning_values(config: LLMConfig, *, api_key: str | None = None) -> tuple[str, ...]:
    """各入口共用厂商声明、协议预设和展示顺序。"""
    levels = provider_reasoning_levels(config, api_key=api_key)
    if config.is_spark:
        if levels is None:
            return SPARK_THINKING_MODES
        return tuple(value for value in SPARK_THINKING_MODES if value in levels)
    if levels is None:
        return (
            ("", "low", "medium", "high")
            if "reasoning_effort" in supported_params(config.litellm_model())
            else ()
        )
    if not levels:
        return ()
    order = (
        "auto",
        "",
        "none",
        "disabled",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
        "enabled",
    )
    values = ("", *(level for level in levels if level != "default"))
    return tuple(
        sorted(values, key=lambda value: order.index(value) if value in order else len(order))
    )


def _provider_catalog(
    config: LLMConfig,
    timeout: float,
    ttl: float = 12 * 3600,
    refresh: bool = False,
    api_key: str | None = None,
) -> ModelCatalog:
    api_key = api_key or config.api_key()
    key = _model_cache_key(config, api_key)
    with _MODEL_LOCK:
        if key not in _MODEL_FETCH_LOCKS:
            _MODEL_FETCH_LOCKS[key] = Lock()
        fetch_lock = _MODEL_FETCH_LOCKS[key]
    with fetch_lock:
        cached = _MODEL_CACHE.get(key)
        if cached and not refresh and time.monotonic() - cached[0] < (ttl if cached[1] else 30):
            return cached[1]
        catalog = _fetch_provider_models_uncached(config, timeout, api_key)
        _MODEL_CACHE[key] = (time.monotonic(), catalog)
        return catalog


def _model_cache_key(config: LLMConfig, api_key: str | None) -> tuple[str, str, bytes]:
    return config.provider, config.base_url, hashlib.sha256((api_key or "").encode()).digest()


def cached_provider_models(config: LLMConfig) -> list[str] | None:
    """返回当前进程里最近一次成功获取的模型列表，不发起网络请求。"""
    cached = _MODEL_CACHE.get(_model_cache_key(config, config.api_key()))
    return list(cached[1]) if cached and cached[1] else None


def clear_model_cache() -> None:
    _MODEL_CACHE.clear()


def _advertised_levels(model, *, spark: bool) -> tuple[str, ...] | None:
    if spark:
        # 星火通过 thinking.type 控制思考，有别于 reasoning_effort。
        declared = _field(_field(model, "thinking"), "supported_types")
        if not isinstance(declared, list):
            return None
        return tuple(dict.fromkeys(mode for mode in declared if isinstance(mode, str)))
    declared = _field(_field(model, "effort"), "supported_levels")
    if declared is None:
        declared = _field(_field(model, "reasoning"), "supported_efforts")
    if not isinstance(declared, list) or any(
        not isinstance(level, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", level)
        for level in declared
    ):
        return None
    return tuple(dict.fromkeys(declared))


def _fetch_provider_models_uncached(
    config: LLMConfig, timeout: float, api_key: str | None
) -> ModelCatalog:
    if not config.base_url or (not api_key and config.requires_api_key()):
        return {}
    try:
        from openai import OpenAI

        with OpenAI(
            api_key=api_key or "local", base_url=config.base_url, timeout=timeout, max_retries=0
        ) as client:
            return {
                model.id: _advertised_levels(model, spark=config.is_spark)
                for model in client.models.list().data
                if isinstance(model.id, str) and model.id.strip()
            }
    except Exception:
        return {}


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
        self,
        messages: list[dict],
        tools: list[dict],
        on_delta: Callable[[str], None] | None = None,
        cancel: Event | None = None,
        *,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> AssistantTurn:
        litellm = _litellm()
        primary, source = chat_history(self.config, messages)
        if any(isinstance(msg.get("content"), list) for msg in primary):
            info = model_metadata(self.config.litellm_model())
            if info.get("supports_vision") is False:
                raise ChatModelError(
                    "This model cannot read images. Select a vision-capable model."
                )
        # 千问 Max 在普通追问时也需要之前助手消息中的思考内容。
        if not tools and not self.config.uses_qianwen_api:
            source = ""
        if on_delta is None and on_reasoning is None:
            response = complete(self.config, primary, tools=tools or None)
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
                reasoning_source=source if reasoning else "",
            )
        parser = _StreamAccumulator(on_delta, litellm, on_reasoning, source)
        return self._chat_stream(primary, tools or None, parser, litellm, cancel)

    def _chat_stream(self, messages, tools, parser, litellm, cancel) -> AssistantTurn:
        """流式：文本 delta 实时回调，tool_calls 增量累积，结束时返回聚合结果。"""
        kwargs: dict = {
            "model": self.config.litellm_model(),
            "messages": messages,
            "api_key": _request_api_key(self.config),
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
        _adapt_spark_params(self.config, kwargs)

        if cancel is not None:
            return asyncio.run(self._cancellable_stream(kwargs, parser, litellm, cancel))
        mark_model("request")
        stream = litellm.completion(**kwargs)
        mark_model("stream_open")
        try:
            for chunk in stream:
                parser.feed(chunk)
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        return parser.finish()

    async def _cancellable_stream(self, request, parser, litellm, cancel: Event) -> AssistantTurn:
        # 取消异步传输也能打断等待首个词元的过程。
        async def consume() -> AssistantTurn:
            async with AsyncExitStack() as resources:
                kwargs = request
                if (
                    getattr(litellm, "aclient_session", None) is None
                    and self.config.litellm_model().startswith("openai/")
                    and request.get("api_key")
                ):
                    from litellm.llms.openai.common_utils import BaseOpenAILLM
                    from openai import AsyncOpenAI

                    http_client = BaseOpenAILLM.get_async_http_client()
                    resources.push_async_callback(http_client.aclose)
                    client = AsyncOpenAI(
                        api_key=request["api_key"],
                        base_url=request.get("api_base"),
                        timeout=request["timeout"],
                        max_retries=request["num_retries"],
                        http_client=http_client,
                    )
                    kwargs = {**request, "client": client}
                mark_model("request")
                stream = await litellm.acompletion(**kwargs)
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
                raise ChatModelError(
                    "Streaming tool call returned malformed JSON arguments."
                ) from exc
            if not isinstance(arguments, dict) or not acc["name"]:
                raise ChatModelError("Streaming tool call is missing a name or object arguments.")
            tool_calls.append(
                ToolCall(
                    id=acc["id"] or f"call-{len(tool_calls)}", name=acc["name"], arguments=arguments
                )
            )
        self._finish_reasoning()
        return AssistantTurn(
            content="".join(self.content_parts),
            tool_calls=tool_calls,
            usage=self.usage,
            reasoning_content="".join(self.reasoning_parts),
            reasoning_fragments=self.reasoning_fragments,
            reasoning_source=self.source if self.reasoning_parts else "",
            reasoning_seconds=self.reasoning_seconds,
        )
