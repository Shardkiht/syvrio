"""会话存储和记忆校验共用的凭据识别逻辑。"""

import re

# 明确赋值保留 Unicode 值；仅靠空白分隔时要求 ASCII 凭据形态，避免把中文描述当密码。
SECRET_RE = re.compile(
    r"(api[_ -]?key|access[_ -]?token|secret|password|passwd|密码|口令|验证码|私钥)"
    r"(?:\s*(?:是|为|[:：=])\s*\S{4,}|[!-~]{4,}|\s+[!-~]{4,})"
    r"|((?:api\s*)?(?:密钥|秘钥)|(?:访问)?令牌|凭据)"
    r"(?:\s*[:：=]\s*[\"'“‘]?[A-Za-z0-9_./+~-]{4,}"
    r"|\s*(?:是|为)?\s*[\"'“‘]?(?=[A-Za-z0-9_./+~-]*[0-9_./+~-])[A-Za-z0-9_./+~-]{4,})",
    re.I,
)


def contains_secret(text: str) -> bool:
    return bool(SECRET_RE.search(text))


def redact_secrets(text: str) -> str:
    """会话落盘前隐藏已识别凭据，保留同一条消息中的其他内容。"""
    return SECRET_RE.sub("[敏感信息已隐藏]", text)
