"""模型下载地址校验与 URL 凭据检查。

Whisper 模型管理、桌面模型、翻译模型原来各写了一份，检查项已经漂移
（有的查控制字符和反斜杠，有的只查空白）。这里统一成最严的一套；
path 是否允许由调用方用 allow_path 区分。

“URL 里不能带凭据”这个规则也有六份实现（plugin_plan、plugin_snapshot、
三个 builtin session、cobalt），敏感 key 的定义与匹配方式各不相同，
cobalt 甚至不查 query/fragment。url_contains_credentials 把它也统一成
最严的一套：敏感 key 取三处旧实现的并集，匹配方式取子串正则。
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit


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


_CREDENTIAL_QUERY_KEYS = re.compile(
    r"(?i)(token|api[_-]?key|key|password|auth|authorization|cookie|secret)"
)


def url_contains_credentials(url: str | None) -> bool:
    """判断 url 是否含凭据或敏感成分；解析失败时按含凭据处理，不抛异常。

    敏感 query key 取三处旧实现的并集：plugin_plan 的正则、plugin_snapshot
    的正则、whisper/translator/ytdlp 的字面集合；匹配方式取最严的子串正则
    （plugin_plan/plugin_snapshot 本来就是子串匹配），query 解析用 parse_qsl
    解码后再比对，所以百分号编码的敏感 key 也拦得住。
    """
    if not url:
        return False
    try:
        parsed = urlsplit(url)
        if parsed.username is not None or parsed.password is not None:
            return True
        if parsed.fragment:
            return True
        return any(
            _CREDENTIAL_QUERY_KEYS.search(key)
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        )
    except (ValueError, TypeError, AttributeError):
        return True
