"""业务层 HTTP 客户端。

MCP Server 不直接访问业务层的 SQLite、线程池或核心 pipeline，只通过这里调用
现有 FastAPI API。这样业务服务可以独立部署、重启和扩容。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

import httpx


def _read_timeout() -> float:
    raw = os.getenv("SUBTRANS_MCP_TIMEOUT", "15")
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 15.0


@dataclass(frozen=True)
class BusinessApiConfig:
    """MCP 侧配置，不包含 DeepSeek 等业务密钥。"""

    base_url: str = "http://127.0.0.1:8000"
    timeout: float = 15.0
    api_token: str | None = None

    @classmethod
    def from_env(cls) -> "BusinessApiConfig":
        return cls(
            base_url=(
                os.getenv("SUBTRANS_API_BASE_URL", "http://127.0.0.1:8000")
                .strip()
                .rstrip("/")
            ),
            timeout=_read_timeout(),
            api_token=os.getenv("SUBTRANS_API_TOKEN") or None,
        )


class BusinessApiError(RuntimeError):
    """业务 API 调用失败，携带可供 MCP 消费的稳定错误码。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        task_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.task_id = task_id

    def to_result(self) -> dict[str, Any]:
        result = {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
        }
        if self.task_id:
            result["task_id"] = self.task_id
        return result


class BusinessApiClient:
    """调用业务 FastAPI 的最小客户端。"""

    def __init__(
        self,
        config: BusinessApiConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config or BusinessApiConfig.from_env()
        self._transport = transport

    @property
    def base_url(self) -> str:
        return self.config.base_url

    def absolute_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if self.config.api_token:
            headers["Authorization"] = f"Bearer {self.config.api_token}"
        if extra_headers:
            headers.update(extra_headers)

        request_kwargs: dict[str, Any] = {"headers": headers}
        if payload is not None:
            request_kwargs["json"] = dict(payload)
        if params is not None:
            request_kwargs["params"] = dict(params)

        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    self.absolute_url(path),
                    **request_kwargs,
                )
        except httpx.TimeoutException as exc:
            raise BusinessApiError(
                "BUSINESS_TIMEOUT",
                "业务服务请求超时，请稍后重试",
            ) from exc
        except httpx.HTTPError as exc:
            raise BusinessApiError(
                "BUSINESS_UNAVAILABLE",
                "无法连接业务服务，请确认 FastAPI 服务已启动",
            ) from exc

        try:
            data = response.json()
        except ValueError:
            data = None

        if response.is_error:
            detail = data.get("detail") if isinstance(data, dict) else None
            if response.status_code == 409 and isinstance(detail, dict):
                task_id = detail.get("taskId")
                if task_id:
                    raise BusinessApiError(
                        "TASK_ALREADY_RUNNING",
                        f"{detail.get('message', '该 URL 已有任务正在处理')}（task_id: {task_id}）",
                        status_code=response.status_code,
                        task_id=task_id,
                    )
            message = str(detail or "业务服务返回错误")[:500]
            if response.status_code == 404:
                code = "TASK_NOT_FOUND" if "/tasks/" in path else "BUSINESS_NOT_FOUND"
            elif response.status_code == 409:
                code = "TASK_NOT_READY"
            elif response.status_code == 400:
                code = "INVALID_ARGUMENT"
            elif response.status_code >= 500:
                code = "BUSINESS_FAILURE"
            else:
                code = "BUSINESS_API_ERROR"
            raise BusinessApiError(code, message, status_code=response.status_code)

        return data

    async def readiness(self) -> dict[str, Any]:
        return await self._request("GET", "/api/health/ready")

    async def probe_video(self, url: str) -> dict[str, Any]:
        return await self._request("POST", "/api/tasks/probe", payload={"url": url})

    async def create_task(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/tasks", payload=payload, extra_headers={"X-Task-Origin": "mcp"}
        )

    async def list_tasks(
        self,
        limit: int = 50,
        offset: int = 0,
        before_id: str | None = None,
        after_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        params["origin"] = "mcp"
        if before_id:
            params["before_id"] = before_id
        if after_id:
            params["after_id"] = after_id
        data = await self._request("GET", "/api/tasks", params=params)
        return data if isinstance(data, list) else []

    async def get_task(self, task_id: str) -> dict[str, Any]:
        data = await self._request("GET", f"/api/tasks/{task_id}")
        return data if isinstance(data, dict) else {}

    async def retry_task(self, task_id: str) -> dict[str, Any]:
        data = await self._request("POST", f"/api/tasks/{task_id}/retry")
        return data if isinstance(data, dict) else {}
