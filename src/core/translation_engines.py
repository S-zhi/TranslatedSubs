"""OpenAI-compatible / Anthropic-compatible 翻译协议适配器。"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


class TranslationEngineError(RuntimeError):
    def __init__(self, message: str, *, code: str = "engine_error"):
        super().__init__(message)
        self.code = code


FORBIDDEN_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
)


def _check_ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise TranslationEngineError(f"禁止访问私有/回环/链路本地 IP 地址 ({ip})", code="ssrf_blocked")


def validate_base_url(url: str) -> str:
    """校验翻译引擎 base_url 的协议与安全性，防止 SSRF。"""
    if not url or not isinstance(url, str) or not url.strip():
        raise TranslationEngineError("URL 不能为空", code="invalid_base_url")

    cleaned_url = url.strip().rstrip("/")
    try:
        parsed = urlparse(cleaned_url)
    except Exception as exc:
        raise TranslationEngineError(f"无效的 URL 格式: {exc}", code="invalid_base_url") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise TranslationEngineError("只支持 HTTP 或 HTTPS 协议", code="invalid_base_url")

    host = parsed.hostname
    if not host:
        raise TranslationEngineError("URL 缺少有效的主机名", code="invalid_base_url")

    host_lower = host.lower().strip("[]")

    if host_lower == "localhost" or any(host_lower.endswith(suffix) for suffix in FORBIDDEN_HOST_SUFFIXES):
        raise TranslationEngineError("禁止访问回环或内部局域网主机", code="ssrf_blocked")

    # 尝试解析为 IP 地址
    try:
        ip_obj = ipaddress.ip_address(host_lower)
        _check_ip_allowed(ip_obj)
        return cleaned_url
    except ValueError:
        pass

    # 若为域名，则通过 DNS 解析校验 IP
    try:
        addrs = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise TranslationEngineError(f"无法解析主机名 '{host}'", code="invalid_base_url") from exc

    if not addrs:
        raise TranslationEngineError(f"未能获取主机名 '{host}' 的 IP 地址", code="invalid_base_url")

    for addr in addrs:
        ip_str = addr[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            _check_ip_allowed(ip_obj)
        except ValueError:
            continue

    return cleaned_url


@dataclass
class EngineClient:
    api_type: str
    base_url: str
    model: str
    api_key: str
    timeout: int = 60

    def complete(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        import httpx

        validated_url = validate_base_url(self.base_url)
        try:
            if self.api_type == "openai_compatible":
                url = validated_url.rstrip("/")
                if not url.endswith("/chat/completions"):
                    url += "/chat/completions"
                response = httpx.post(
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                        "temperature": 0.3,
                        "stream": False,
                        "max_tokens": max_tokens,
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                return str(data["choices"][0]["message"]["content"])

            if self.api_type == "anthropic_compatible":
                url = validated_url.rstrip("/")
                if not url.endswith("/messages"):
                    url += "/v1/messages" if not url.endswith("/v1") else "/messages"
                response = httpx.post(
                    url,
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "system": system,
                        "messages": [{"role": "user", "content": user}],
                        "max_tokens": max_tokens,
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                content = data.get("content") or []
                return "".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            code = "unauthorized" if status in (401, 403) else "rate_limited" if status == 429 else "upstream_error"
            raise TranslationEngineError(f"上游接口返回 HTTP {status}", code=code) from exc
        except httpx.RequestError as exc:
            raise TranslationEngineError("连接翻译引擎超时或失败", code="network_error") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise TranslationEngineError("翻译引擎返回格式无法解析", code="invalid_response") from exc

        raise TranslationEngineError("不支持的翻译协议类型", code="unsupported_api_type")


def make_engine_client(config: Any, timeout: int | None = None) -> EngineClient:
    api_type = getattr(config, "api_type", None) or config.get("api_type")
    base_url = getattr(config, "base_url", None) or config.get("base_url")
    model = getattr(config, "model", None) or config.get("model")
    api_key = getattr(config, "api_key", None) or config.get("api_key")
    if timeout is None:
        timeout = getattr(config, "timeout", None) or (config.get("timeout") if isinstance(config, dict) else 60) or 60
    if not (api_key and str(api_key).strip()):
        raise TranslationEngineError("未配置 API Key", code="missing_api_key")
    validated_url = validate_base_url(str(base_url))
    return EngineClient(api_type=api_type, base_url=validated_url, model=model, api_key=str(api_key).strip(), timeout=int(timeout))
