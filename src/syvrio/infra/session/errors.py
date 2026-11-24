"""会话保护和请求冲突。"""

from __future__ import annotations

from ..errors import SyvrioError


class SessionProtectedError(SyvrioError):
    """会话正在使用，或保存了正式记忆引用的原始证据。"""


class SessionDeletionChanged(SyvrioError):
    """用户看到确认提示后，相关待审条目发生了变化。"""


class ConversationError(SyvrioError):
    """与传输方式无关的预期对话冲突或提交被拒。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
