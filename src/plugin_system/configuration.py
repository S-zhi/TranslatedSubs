"""Small scalar configuration descriptors shared by plugins and settings forms."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlsplit


def validate_fields(
    config: Mapping[str, Any], fields: Mapping[str, dict[str, Any]], *, partial: bool = True
) -> dict[str, Any]:
    if not isinstance(config, Mapping) or set(config) - set(fields):
        raise ValueError("插件配置包含不支持的字段")
    result = dict(config)
    for key, spec in fields.items():
        if key not in result:
            if not partial and "default" in spec:
                result[key] = spec["default"]
            elif not partial and spec.get("required"):
                raise ValueError(f"插件配置缺少字段: {key}")
            else:
                continue
        value = result[key]
        if value is None and spec.get("nullable", False):
            continue
        kind = spec.get("type", "string")
        valid = {
            "string": isinstance(value, str),
            "integer": type(value) is int,
            "number": type(value) in {int, float} and math.isfinite(value),
            "boolean": isinstance(value, bool),
        }.get(kind, False)
        if not valid:
            raise ValueError(f"插件配置字段类型错误: {key}")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"插件配置字段取值不支持: {key}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value < spec.get("minimum", -math.inf) or value > spec.get("maximum", math.inf):
                raise ValueError(f"插件配置字段超出范围: {key}")
        if isinstance(value, str):
            if len(value) > spec.get("maxLength", 4096) or (spec.get("required") and not value.strip()):
                raise ValueError(f"插件配置字段内容无效: {key}")
            if spec.get("format") == "url" and value:
                parsed = urlsplit(value)
                secret_keys = {"token", "api_key", "apikey", "key", "password", "auth", "authorization", "cookie"}
                if parsed.username is not None or parsed.password is not None or parsed.fragment or any(
                    key.lower() in secret_keys for key, _ in parse_qsl(parsed.query)
                ):
                    raise ValueError(f"插件配置 URL 不得包含凭据: {key}")
                if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
                    raise ValueError(f"插件配置 URL 无效: {key}")
    return result
