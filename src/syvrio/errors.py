"""Syvrio 共用的异常类型。

用户可触达路径中，Syvrio 主动抛出的异常均继承 SyvrioError。
同时保留原有基类，使现有的 except ValueError 和 except RuntimeError 等处理继续有效。
边界层捕获 SyvrioError，转换为用户可读消息。
其他异常由 except Exception 兜底，先记录原始错误，再转换为退出状态或错误响应。
"""

from __future__ import annotations


class SyvrioError(Exception):
    """Syvrio 在用户可达路径主动报告的失败所共用的基类。"""


class SessionProtectedError(SyvrioError):
    """会话正在使用，或保存了正式记忆引用的原始证据。"""


class SessionDeletionChanged(SyvrioError):
    """用户看到确认提示后，相关待审条目发生了变化。"""


class ConversationError(SyvrioError):
    """与传输方式无关的预期对话冲突或提交被拒。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
