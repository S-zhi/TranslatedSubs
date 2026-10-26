"""Explicit start-only access to a pre-provisioned InstaBox machine."""

from __future__ import annotations

import json
import os
import secrets
import asyncio
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import LauncherConfig
from .provider import InstaBoxClient, ProviderError


class FixedMachineService:
    def __init__(self, config: LauncherConfig, provider: InstaBoxClient, *,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.provider = provider
        self.path = config.db_path.with_name("fixed-machine-operation.json")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._operation = self._read_operation()
        self._lock = asyncio.Lock()
        self._health = httpx.AsyncClient(timeout=httpx.Timeout(5, connect=3),
                                         follow_redirects=False, trust_env=False,
                                         transport=transport)

    def _read_operation(self) -> dict:
        try:
            value = json.loads(self.path.read_text())
            if (isinstance(value, dict) and value.get("machineId") == self.config.machine_id
                    and isinstance(value.get("key"), str) and isinstance(value.get("id"), str)):
                return value
        except (OSError, ValueError):
            pass
        return {}

    def _save(self) -> None:
        temp = self.path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self._operation, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)

    async def close(self) -> None:
        await self._health.aclose()

    @property
    def url(self) -> str:
        suffix = self.config.machine_id.removeprefix("m_")
        return f"https://8000-{suffix}.instabox.tech"

    def _public(self, status: str, *, message: str | None = None,
                ready: bool = False) -> dict:
        return {"configured": self.config.configured, "mode": "fixed",
                "machineId": self.config.machine_id, "status": status,
                "trialId": self.config.machine_id, "expiresAt": None,
                "url": self.url if ready else None, "message": message}

    async def current(self) -> dict:
        async with self._lock:
            return await self._current_unlocked()

    async def _current_unlocked(self) -> dict:
        if not self.config.configured:
            return self._public("unavailable", message="在线工作台暂未配置。")
        machine = await self.provider.machine(self.config.machine_id)
        if not machine:
            return self._public("unavailable", message="在线工作台暂不可用。")
        if self._operation.get("id"):
            try:
                operation = await self.provider.operation(self._operation["id"], wait="0s")
            except ProviderError:
                return self._public("booting")
            if operation is None:
                return self._public("booting")
            if operation.get("machineId") != self.config.machine_id:
                return self._public("unavailable", message="在线工作台暂不可用。")
            if operation and operation.get("state") == "failed":
                return self._public("failed", message="工作台启动失败，请稍后重试。")
            if operation and operation.get("state") in {"queued", "running"}:
                return self._public("booting")
            if operation.get("state") == "succeeded":
                self._operation = {}
                self._save()
                machine = await self.provider.machine(self.config.machine_id)
                if not machine:
                    return self._public("unavailable", message="在线工作台暂不可用。")
        if machine.get("state") != "running":
            if machine.get("state") in {"stopped", "paused"}:
                return self._public("idle")
            return self._public("booting")
        if self._operation.get("key"):
            # A start response may have been lost before its operation ID arrived.
            # Seeing the machine running resolves that ambiguous request.
            self._operation = {}
            self._save()
        ports = await self.provider.ports(self.config.machine_id)
        found = [entry for entry in ports.get("ports", [])
                 if isinstance(entry, dict) and entry.get("machineId") == self.config.machine_id
                 and entry.get("port") == 8000 and self._valid_port(entry.get("url"))]
        if len(found) != 1:
            return self._public("checking")
        try:
            response = await self._health.get(self.url + "/api/health",
                                              headers={"Accept": "application/json"})
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return self._public("checking")
        if response.status_code == 200 and payload == {"ok": True}:
            return self._public("ready", ready=True)
        return self._public("checking")

    async def start(self) -> dict:
        async with self._lock:
            machine = await self.provider.machine(self.config.machine_id)
            if not machine:
                return self._public("unavailable", message="在线工作台暂不可用。")
            operation = self._operation
            if operation.get("id"):
                try:
                    previous = await self.provider.operation(operation["id"], wait="0s")
                except ProviderError:
                    previous = None
                if previous and previous.get("machineId") != self.config.machine_id:
                    self._operation = {}
                    self._save()
                    raise ProviderError("invalid_response")
                if previous and previous.get("state") in {"queued", "running"}:
                    return self._public("booting")
                if previous and previous.get("state") == "succeeded":
                    self._operation = {}
                    self._save()
                    machine = await self.provider.machine(self.config.machine_id)
                    if not machine:
                        return self._public("unavailable", message="在线工作台暂不可用。")
                    if machine.get("state") == "running":
                        return await self._current_unlocked()
                    operation = self._operation
                if previous and previous.get("state") == "failed":
                    self._operation = {}
                    self._save()
                    machine = await self.provider.machine(self.config.machine_id)
                    if not machine:
                        return self._public("unavailable", message="在线工作台暂不可用。")
                    operation = self._operation
                if previous is None:
                    # Re-send with the exact same idempotency key to recover an
                    # operation whose response was lost or is not readable.
                    operation["id"] = ""
                    self._save()

            if machine.get("state") == "running":
                return await self._current_unlocked()
            if machine.get("state") not in {"stopped", "paused"} and not operation.get("key"):
                return self._public("booting")

            if not operation.get("key"):
                operation = {"id": "", "key": "fixed-start-" + secrets.token_hex(24),
                             "machineId": self.config.machine_id}
                self._operation = operation
                self._save()
            try:
                if not operation.get("id"):
                    started = await self.provider.start_machine(self.config.machine_id, operation["key"])
                    if started.get("machineId") != self.config.machine_id:
                        self._operation = {}
                        self._save()
                        raise ProviderError("invalid_response")
                    operation["id"] = started["id"]
                    self._save()
                return self._public("booting")
            except ProviderError as exc:
                # Keep the key (including an empty operation ID) after an ambiguous response.
                if not exc.uncertain:
                    self._operation = {}
                    self._save()
                    return self._public("failed", message="工作台启动失败，请稍后重试。")
                return self._public("booting")

    def _valid_port(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.hostname == self.url.removeprefix("https://")
                and parsed.netloc == parsed.hostname and parsed.path in {"", "/"}
                and not parsed.query and not parsed.fragment)
