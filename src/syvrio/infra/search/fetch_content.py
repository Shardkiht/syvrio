"""将提取的正文转换为统一的 FetchResult，避免暴露原始 HTML。"""

import re
from html.parser import HTMLParser

from .base import (
    FetchQuery,
    FetchResult,
    ProviderError,
    ProviderErrorKind,
    truncate_fetch_content,
)

_HTML = re.compile(
    r"<!doctype\s+html\b|<!--|</?(?:html|head|body|main|article|section|div|p|br|h[1-6]|"
    r"ul|ol|li|table|tr|td|span|a|img|strong|em|b|i|pre|code|blockquote|"
    r"script|style|template|iframe|noscript)\b[^>]*>",
    re.IGNORECASE,
)


class _TextParser(HTMLParser):
    _hidden = {"head", "script", "style", "template", "iframe", "noscript"}
    _block = {"p", "br", "div", "li", "section", "article", "tr", "h1", "h2", "h3"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self._hidden:
            self.hidden.append(tag)
        elif not self.hidden and tag in self._block:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        elif not self.hidden and tag in self._block:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def extract_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    if _HTML.search(value):
        parser = _TextParser()
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    return value.strip()


def extracted_result(
    provider: str,
    query: FetchQuery,
    *,
    url: object,
    title: object,
    content: str,
    excerpts_only: bool = False,
) -> FetchResult:
    if not content:
        raise ProviderError(provider, ProviderErrorKind.BAD_RESPONSE, "响应没有网页正文")
    final_url = url.strip() if isinstance(url, str) else ""
    final_url = final_url or query.url
    content, truncated = truncate_fetch_content(content, query.max_chars)
    return FetchResult(
        url=query.url,
        final_url=final_url,
        title=extract_text(title) or final_url,
        content=content,
        truncated=truncated or excerpts_only,
        content_kind="excerpts" if excerpts_only else "page",
    )
