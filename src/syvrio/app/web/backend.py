"""调用嵌入式网页应用时使用的名称。"""

from ...backend.api.channel import APIChannel as WebBackend
from ...backend.api.channel import APIError as WebError

__all__ = ["WebBackend", "WebError"]
