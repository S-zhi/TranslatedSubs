"""模型下载地址校验：无凭据的 HTTPS 地址。

Whisper 模型管理、桌面模型、翻译模型原来各写了一份，检查项已经漂移
（有的查控制字符和反斜杠，有的只查空白）。这里统一成最严的一套；
path 是否允许由调用方用 allow_path 区分。
"""

from __future__ import annotations

from urllib.parse import urlsplit


def is_valid_https_endpoint(url: str | None, *, allow_path: bool) -> bool:
    """判断 url 是不是无凭据的 HTTPS 地址；任何异常都返回 False，不抛异常。"""
    if url is None:
        return False
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        if parsed.port is not None and parsed.port <= 0:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        if parsed.query or parsed.fragment:
            return False
        if not allow_path and parsed.path not in ("", "/"):
            return False
        if any(c.isspace() or ord(c) < 32 for c in url):
            return False
        return "\\" not in url
    except (ValueError, TypeError, AttributeError):
        return False
