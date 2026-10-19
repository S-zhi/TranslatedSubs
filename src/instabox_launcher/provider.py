"""Narrow InstaBox API client; credentials never leave its fixed API origin."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx


API_ORIGIN = "https://api.instabox.sh"
PUBLIC_SUFFIXES = (".instabox.tech", ".instabox.sh")


class ProviderError(Exception):
    def __init__(self, code: str, *, uncertain: bool = False):
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


class InstaBoxClient:
    def __init__(self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.client = httpx.AsyncClient(
            base_url=API_ORIGIN,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(40, connect=10),
            follow_redirects=False, trust_env=False, transport=transport,
        )

    async def close(self) -> None:
        await self.client.aclose()

    @staticmethod
    def identifier(value: object) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
            raise ProviderError("invalid_response", uncertain=True)
        return value

    async def _request(self, method: str, path: str, *, missing: bool = False, **kwargs) -> dict | None:
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise ProviderError("connection", uncertain=True) from None
        if missing and response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            raise ProviderError("provider_unavailable", uncertain=response.status_code >= 500 or response.status_code in {408, 409})
        try:
            data = response.json()
        except (ValueError, UnicodeDecodeError):
            raise ProviderError("invalid_response", uncertain=True) from None
        if not isinstance(data, dict):
            raise ProviderError("invalid_response", uncertain=True)
        return data

    def _operation(self, data: dict) -> dict:
        self.identifier(data.get("id"))
        self.identifier(data.get("machineId"))
        if data.get("state") not in {"queued", "running", "succeeded", "failed"}:
            raise ProviderError("invalid_response", uncertain=True)
        return data

    async def create(self, payload: dict, key: str) -> dict:
        data = await self._request("POST", "/v1/machines", json=payload, headers={"Idempotency-Key": key})
        return self._operation(data)

    async def operation(self, operation_id: str) -> dict | None:
        data = await self._request("GET", f"/v1/operations/{self.identifier(operation_id)}",
                                   params={"wait": "25s"}, missing=True)
        return self._operation(data) if data else None

    async def machine(self, machine_id: str) -> dict | None:
        data = await self._request("GET", f"/v1/machines/{self.identifier(machine_id)}", missing=True)
        if data and data.get("id") != machine_id:
            raise ProviderError("invalid_response", uncertain=True)
        return data

    async def find_trial(self, launcher_id: str, trial_id: str) -> dict | None:
        cursor = None
        for _ in range(20):
            params = [("metadata", f"app:translatedsubs-trial"),
                      ("metadata", f"launcher:{launcher_id}"), ("metadata", f"trial:{trial_id}"),
                      ("limit", "100")]
            if cursor:
                params.append(("cursor", cursor))
            page = await self._request("GET", "/v1/machines", params=params)
            machines = page.get("machines")
            if not isinstance(machines, list):
                raise ProviderError("invalid_response", uncertain=True)
            for machine in machines:
                if (isinstance(machine, dict)
                        and machine.get("metadata", {}).get("trial") == trial_id
                        and machine.get("metadata", {}).get("launcher") == launcher_id
                        and machine.get("metadata", {}).get("app") == "translatedsubs-trial"):
                    self.identifier(machine.get("id"))
                    return machine
            cursor = page.get("nextCursor")
            if not cursor:
                return None
        raise ProviderError("reconciliation_incomplete", uncertain=True)

    async def execute(self, machine_id: str, cmd: str, *, envs: dict | None = None,
                      stdin: str = "", timeout: int = 30) -> str:
        data = await self._request(
            "POST", f"/v1/machines/{self.identifier(machine_id)}/exec",
            json={"cmd": cmd, "cwd": "/data/home", "envs": envs or {},
                  "stdin": stdin, "timeoutSeconds": timeout},
            timeout=httpx.Timeout(timeout + 15, connect=10),
        )
        if (type(data.get("exitCode")) is not int or data["exitCode"] != 0
                or data.get("timedOut") is not False or data.get("truncated") is not False
                or not isinstance(data.get("stdout"), str)):
            raise ProviderError("exec_failed")
        return data["stdout"].strip()

    async def publish(self, machine_id: str) -> str:
        data = await self._request("POST", f"/v1/machines/{self.identifier(machine_id)}/ports",
                                   json={"port": 8000, "name": "TranslatedSubs"})
        url = data.get("url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (not parsed or parsed.scheme != "https" or not parsed.hostname
                or not parsed.hostname.endswith(PUBLIC_SUFFIXES)
                or parsed.username or parsed.password or parsed.netloc != parsed.hostname
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or data.get("machineId") != machine_id or data.get("port") != 8000
                or data.get("id") != parsed.hostname.split(".", 1)[0]):
            raise ProviderError("invalid_public_url")
        return f"https://{parsed.hostname}/"

    async def delete(self, machine_id: str, key: str) -> dict | None:
        data = await self._request("DELETE", f"/v1/machines/{self.identifier(machine_id)}",
                                   headers={"Idempotency-Key": key}, missing=True)
        if data:
            data = self._operation(data)
            if data["machineId"] != machine_id:
                raise ProviderError("invalid_response", uncertain=True)
        return data
