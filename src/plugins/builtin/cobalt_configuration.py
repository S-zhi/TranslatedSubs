"""Safe, scalar configuration for the optional Cobalt downloader."""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from src.plugin_system import PluginError
from src.plugin_system.configuration import validate_fields

PLUGIN_ID = "builtin.cobalt"
YTDLP_CONFIG_KEYS = frozenset({
    "quality", "format_selector", "proxy", "socket_timeout", "cookies_ref",
    "merge_output_format", "retries", "concurrent_fragments",
})
COBALT_CONFIG_SCHEMA = {
    "endpoint": {
        "type": "string", "label": "Cobalt 服务地址", "format": "url",
        "required": True, "maxLength": 2048,
    },
    "socket_timeout": {
        "type": "integer", "label": "连接超时（秒）", "minimum": 1,
        "maximum": 300, "default": 30,
    },
    "access_mode": {
        "type": "string", "label": "访问方式", "enum": ["anonymous", "api_key"],
        "default": "anonymous",
    },
    "fallback_policy": {
        "type": "string", "label": "失败后切换", "enum": ["none", "ytdlp"],
        "default": "none",
    },
    "filename_style": {
        "type": "string", "label": "文件名风格",
        "enum": ["classic", "pretty", "basic", "nerdy"], "default": "basic",
    },
    "audio_format": {
        "type": "string", "label": "音频格式",
        "enum": ["best", "mp3", "ogg", "wav", "opus"], "default": "best",
    },
}
_QUALITY_RE = re.compile(r"^(?:best|audio_only|(?:4320|2160|1440|1080|720|480|360|240|144)p?)$", re.I)
_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks4a", "socks5"})


def _endpoint(value: str) -> str:
    # Do not include a rejected endpoint in errors: URLs may contain credentials.
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and "?" not in value and "#" not in value and "\\" not in value
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            and parsed.port != 0  # Also rejects malformed and out-of-range ports.
        )
    except ValueError as exc:
        raise ValueError("cobalt endpoint must be an HTTP(S) URL without credentials") from exc
    if not valid:
        raise ValueError("cobalt endpoint must be an HTTP(S) URL without credentials")
    return value.rstrip("/") + "/"


def _validate_legacy(config: Mapping[str, Any]) -> None:
    missing = YTDLP_CONFIG_KEYS - config.keys()
    if missing:
        raise ValueError("cobalt download config is missing frozen download fields")
    quality = config["quality"]
    if not isinstance(quality, str) or not _QUALITY_RE.fullmatch(quality):
        raise ValueError("cobalt download quality is unsupported")
    selector = config["format_selector"]
    if selector is not None and (not isinstance(selector, str) or len(selector) > 4096):
        raise ValueError("cobalt download format selector is invalid")
    proxy = config["proxy"]
    if proxy is not None:
        if not isinstance(proxy, str) or len(proxy) > 4096:
            raise ValueError("cobalt download proxy is invalid")
        try:
            parsed = urlsplit(proxy)
            valid = (
                parsed.scheme in _PROXY_SCHEMES and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment and "\\" not in proxy
                and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in proxy)
                and parsed.port != 0
            )
        except ValueError as exc:
            raise ValueError("cobalt download proxy is invalid") from exc
        if not valid:
            raise ValueError("cobalt download proxy is invalid")
    if config["cookies_ref"] is not None and config["cookies_ref"] != "settings:cookies_file":
        raise ValueError("cobalt download cookie reference is unsupported")
    for key, minimum, maximum in (("retries", 0, 100), ("concurrent_fragments", 1, 32)):
        value = config[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"cobalt download {key} is invalid")
    merge_format = config["merge_output_format"]
    if not isinstance(merge_format, str) or not merge_format.strip() or len(merge_format) > 4096:
        raise ValueError("cobalt download merge output format is invalid")


def validate_cobalt_config(
    config: Mapping[str, Any], *, purpose: str | None = None, partial: bool = True,
) -> dict[str, Any]:
    """Validate editable instance fields or freeze a complete download plan.

    The plan includes the eight original yt-dlp settings to preserve explicit
    fallback behavior. It stores a reference to the Cobalt secret, never its value.
    """
    if not isinstance(config, Mapping):
        raise ValueError("cobalt configuration must be an object")
    if purpose not in {None, "download"}:
        raise ValueError("cobalt only supports download")
    allowed = set(COBALT_CONFIG_SCHEMA)
    if purpose == "download":
        allowed.update(YTDLP_CONFIG_KEYS)
        allowed.add("credential_ref")
    if set(config) - allowed:
        raise ValueError("cobalt configuration contains unsupported fields")
    # Instance updates replace the complete override map. A partial schema only
    # leaves optional fields unset; every saved Cobalt instance needs an endpoint.
    if "endpoint" not in config:
        raise ValueError("cobalt endpoint is required")

    editable = {key: config[key] for key in COBALT_CONFIG_SCHEMA if key in config}
    if isinstance(editable.get("endpoint"), str):
        editable["endpoint"] = _endpoint(editable["endpoint"])
    validated = validate_fields(editable, COBALT_CONFIG_SCHEMA, partial=partial if purpose is None else False)
    if purpose is None:
        return validated

    _validate_legacy(config)
    expected_ref = "settings:cobalt_api_key" if validated["access_mode"] == "api_key" else None
    if "credential_ref" in config and config["credential_ref"] != expected_ref:
        raise ValueError("cobalt credential reference does not match the access mode")
    return {
        **{key: config[key] for key in YTDLP_CONFIG_KEYS},
        **validated,
        "credential_ref": expected_ref,
    }


def preflight_cobalt(
    config: Mapping[str, Any], *, operation: str, purpose: str,
    source_lang: str = "", target_lang: str = "",
) -> None:
    """Check local dependencies without contacting the Cobalt endpoint."""
    if operation != "download" or purpose != "download":
        raise PluginError("invalid_input", "download", PLUGIN_ID, "cobalt only supports download")
    try:
        validated = validate_cobalt_config(config, purpose=purpose)
    except (TypeError, ValueError) as exc:
        raise PluginError("invalid_input", "download", PLUGIN_ID, str(exc)) from exc
    proxy = validated["proxy"]
    if proxy is not None and urlsplit(proxy).scheme not in {"http", "https"}:
        raise PluginError("invalid_input", "download", PLUGIN_ID, "Cobalt 仅支持 HTTP(S) 代理")
    try:
        available = importlib.util.find_spec("httpx") is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        available = False
    if not available:
        raise PluginError("dependency_missing", "download", PLUGIN_ID, "缺少运行依赖：httpx")
    if validated["fallback_policy"] == "ytdlp":
        if validated["retries"] < 1:
            raise PluginError(
                "invalid_input", "download", PLUGIN_ID,
                "启用 yt-dlp 备用下载时，请将运行时 download_retries 设置为至少 1",
            )
        from src.plugins.builtin.downloader_ytdlp import YtDlpFactory

        legacy = {key: validated[key] for key in YTDLP_CONFIG_KEYS}
        YtDlpFactory().preflight(
            legacy, operation=operation, purpose=purpose,
            source_lang=source_lang, target_lang=target_lang,
        )
