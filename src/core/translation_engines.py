"""OpenAI-compatible / Anthropic-compatible 翻译协议适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class TranslationEngineError(RuntimeError):
    def __init__(self, message: str, *, code: str = "engine_error"):
        super().__init__(message)
        self.code = code


@dataclass
class EngineClient:
    api_type: str
    base_url: str
    model: str
    api_key: str
    timeout: int = 60
    temperature: float = 0.3
    default_max_tokens: int = 4096

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        import httpx
        token_limit = int(max_tokens or self.default_max_tokens)

        try:
            if self.api_type == "openai_compatible":
                url = self.base_url.rstrip("/")
                if not url.endswith("/chat/completions"):
                    url += "/chat/completions"
                response = httpx.post(
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                        "temperature": self.temperature,
                        "stream": False,
                        "max_tokens": token_limit,
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                return str(data["choices"][0]["message"]["content"])

            if self.api_type == "anthropic_compatible":
                url = self.base_url.rstrip("/")
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
                        "max_tokens": token_limit,
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
    def _number(name: str, fallback: float) -> float:
        value = getattr(config, name, None)
        if value is None and isinstance(config, dict):
            value = config.get(name)
        try:
            return float(value if value is not None else fallback)
        except (TypeError, ValueError):
            return fallback

    raw_max = getattr(config, "max_tokens", None)
    if raw_max is None and isinstance(config, dict):
        raw_max = config.get("max_tokens", config.get("maxTokens"))
    try:
        default_max_tokens = max(1, int(raw_max or 4096))
    except (TypeError, ValueError):
        default_max_tokens = 4096
    return EngineClient(
        api_type=api_type, base_url=base_url, model=model,
        api_key=str(api_key).strip(), timeout=int(timeout),
        temperature=max(0.0, min(2.0, _number("temperature", 0.3))),
        default_max_tokens=default_max_tokens,
    )
