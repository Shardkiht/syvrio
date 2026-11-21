"""Names used by callers of the embedded Web application."""

from ...backend.api.channel import APIChannel as WebBackend
from ...backend.api.channel import APIError as WebError

__all__ = ["WebBackend", "WebError"]
