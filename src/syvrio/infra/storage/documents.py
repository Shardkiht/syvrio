from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from ..errors import SyvrioError

TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".text",
        ".log",
        ".json",
        ".jsonl",
        ".ndjson",
        ".csv",
        ".tsv",
        ".yaml",
        ".yml",
        ".xml",
    }
)
DOCUMENT_SUFFIXES = frozenset(
    {".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".html", ".htm", ".epub", ".msg", ".ipynb"}
)
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | DOCUMENT_SUFFIXES
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024


class DocumentError(SyvrioError, ValueError):
    """文件无法转换为可导入的文本。"""


def read_document(path: Path, *, content: bytes | None = None) -> str:
    """读取用户指定文件；复杂格式交给本地 MarkItDown，不启用插件或云端转换。"""
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise DocumentError(
            f"不支持的文件格式：{suffix or '无扩展名'}。请导出为 TXT、Markdown 或常见文档格式。"
        )
    try:
        if (path.stat().st_size if content is None else len(content)) > MAX_DOCUMENT_BYTES:
            raise DocumentError("文件超过 20 MiB，请拆分后导入。")
        if suffix in TEXT_SUFFIXES:
            raw = path.read_bytes() if content is None else content
            # 只凭 BOM 识别 UTF-16，避免把无 BOM 的普通文本误判成 UTF-16。
            encodings = (
                ("utf-16",)
                if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
                else ("utf-8-sig", "gb18030")
            )
            text = None
            for encoding in encodings:
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeError:
                    continue
            if text is None or "\0" in text:
                raise DocumentError("无法解码文本，请转换为 UTF-8 后重试。")
        else:
            try:
                from markitdown import MarkItDown
            except ImportError as exc:
                raise DocumentError(
                    "文档解析依赖未安装：开发环境运行 uv sync --extra documents；工具安装使用 uv tool install 'syvrio[documents]'（新包发布后）。"
                ) from exc
            converter = MarkItDown(enable_plugins=False)
            if content is None:
                text = converter.convert(str(path)).text_content
            else:
                # 上传文件名仅提供扩展名；转换器永远只打开这次上传的本地副本。
                with TemporaryDirectory() as folder:
                    copy = Path(folder) / ("document" + suffix)
                    copy.write_bytes(content)
                    text = converter.convert(str(copy)).text_content
    except DocumentError:
        raise
    except Exception as exc:
        # 不回显解析器异常里的文档片段或内部路径。
        raise DocumentError("文件读取或解析失败，请检查文件是否损坏、加密或缺少解析依赖。") from exc
    if not text.strip():
        raise DocumentError("未提取到文字；扫描件或图片需先做 OCR，再导入文本。")
    return text
