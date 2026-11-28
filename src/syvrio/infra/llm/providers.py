"""模型供应商目录和端点规则。"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from ..config import QIANWEN_API_KEY_ENV, QIANWEN_BASE_URL, LLMConfig


@dataclass(frozen=True)
class ProviderPreset:
    key: str
    label: str
    base_url: str
    api_key_env: str


PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    ProviderPreset(
        "siliconflow",
        "硅基流动 SiliconFlow",
        "https://api.siliconflow.cn/v1",
        "SILICONFLOW_API_KEY",
    ),
    ProviderPreset(
        "dashscope",
        "阿里云百炼",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
    ),
    ProviderPreset("qianwen", "千问 AI 平台", QIANWEN_BASE_URL, QIANWEN_API_KEY_ENV),
    ProviderPreset(
        "spark",
        "讯飞星火 Spark（MaaS）",
        "https://maas-api.cn-huabei-1.xf-yun.com/v2",
        "SPARK_API_KEY",
    ),
    ProviderPreset("openai", "OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    ProviderPreset(
        "openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"
    ),
    ProviderPreset("custom", "自定义 OpenAI 兼容接口", "", "OPENAI_API_KEY"),
)


PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "siliconflow": "SiliconFlow",
    "dashscope": "百炼",
    "qianwen": "千问 AI 平台",
    "spark": "讯飞星火",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "custom": "自定义",
}


def provider_label(provider: str) -> str:
    return PROVIDER_LABELS.get(provider, provider)


def provider_base_url(config: LLMConfig, preset: ProviderPreset) -> str:
    fallback = config.base_url if config.provider == preset.key else preset.base_url
    return config.provider_urls.get(preset.key, fallback)


def normalize_provider_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "Base URL must be an HTTP(S) endpoint without credentials, query or fragment."
        )
    return url
