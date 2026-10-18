"""Nonblocking, read-only update discovery for the macOS desktop shell.

``check()`` starts at most one daemon worker and returns ``checking`` immediately.
Callers poll the same method for a terminal result; even ``force=True`` coalesces
with a running check. No networking or DNS runs on the caller's event loop.
The service validates manifest SHA256 metadata; it does not download an installer
and therefore does not claim to verify installer bytes.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import socket
import threading
import time
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Callable, Iterable
from urllib.parse import urlsplit

import httpx


PLATFORM = "macos-arm64"
MAX_MANIFEST_BYTES = 256 * 1024
_STABLE = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_MACOS = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?")


class _InvalidManifest(ValueError):
    """Safe, locally authored diagnostic (never a remote response or URL)."""


def _stable_version(value: object) -> tuple[int, int, int]:
    match = _STABLE.fullmatch(value) if isinstance(value, str) and len(value) <= 128 else None
    if match is None:
        raise _InvalidManifest("Update versions must be stable major.minor.patch versions.")
    return tuple(int(part) for part in match.groups())


def _macos_version(value: object) -> tuple[int, int, int]:
    match = _MACOS.fullmatch(value) if isinstance(value, str) and len(value) <= 32 else None
    if match is None:
        raise _InvalidManifest("A valid macOS version is required to check compatibility.")
    return tuple(int(part or 0) for part in match.groups())


def _resolve(host: str, port: int) -> Iterable[str]:
    return {entry[4][0] for entry in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_global and not address.is_multicast and (mapped is None or _public_address(str(mapped)))


def _safe_url(value: object, resolver: Callable) -> tuple[httpx.URL, str]:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise _InvalidManifest("Update URL is missing or invalid.")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise _InvalidManifest("Update URL contains invalid characters.")
    try:
        parsed = urlsplit(value)
        url = httpx.URL(value)
        host = url.host.lower().rstrip(".")
        port = parsed.port or 443
    except (ValueError, httpx.InvalidURL):
        raise _InvalidManifest("Update URL is invalid.") from None
    if (parsed.scheme.lower() != "https" or not host or parsed.username is not None
            or parsed.password is not None or parsed.fragment or "%" in host):
        raise _InvalidManifest("Update URLs must use HTTPS without credentials or fragments.")
    if (("." not in host and ":" not in host)
            or host.endswith((".localhost", ".local", ".internal", ".home.arpa"))):
        raise _InvalidManifest("Update URLs must use public hosts.")
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = list(resolver(host, port))
    if not addresses or not all(_public_address(address) for address in addresses):
        raise _InvalidManifest("Update URLs must resolve only to public addresses.")
    return url, addresses[0]


def _installed_version() -> str:
    configured = os.environ.get("SUBTRANS_DESKTOP_VERSION")
    if configured is not None:
        return configured
    try:
        return version("translatedsubs-backend")
    except PackageNotFoundError:
        return "unknown"


class UpdateService:
    """Inject transport, DNS, clock and versions to test without external I/O.

    Successful and failed checks share a TTL to avoid repeated offline requests.
    A hung system resolver cannot be interrupted safely in Python: the caller
    still observes a timeout, and no additional worker is launched until that
    resolver returns. This bounds resource use even under repeated forced polls.
    """

    def __init__(
        self,
        manifest_url: str | None = None,
        current_version: str | None = None,
        *,
        macos_version: str | None = None,
        platform_name: str = PLATFORM,
        transport: httpx.BaseTransport | None = None,
        resolver: Callable[[str, int], Iterable[str]] = _resolve,
        clock: Callable[[], float] = time.monotonic,
        cache_ttl: float = 3600,
        timeout: float = 10,
    ) -> None:
        if not 0 < timeout <= 60 or not 0 < cache_ttl <= 86400:
            raise ValueError("Update timeout/TTL must be positive and bounded.")
        self.manifest_url = (os.environ.get("SUBTRANS_DESKTOP_UPDATE_URL", "")
                             if manifest_url is None else manifest_url)
        self.current_version = _installed_version() if current_version is None else current_version
        self._macos = platform.mac_ver()[0] if macos_version is None else macos_version
        self._platform = platform_name
        self._transport = transport
        self._resolver = resolver
        self._clock = clock
        self._ttl = cache_ttl
        self._timeout = timeout
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._deadline = 0.0
        self._expires = 0.0
        self._result = self._state("disabled" if not self.manifest_url else "checking")

    def _state(self, state: str, *, error: str | None = None, **fields) -> dict:
        return {
            "state": state,
            "current_version": self.current_version,
            "latest_version": None,
            "release_notes": None,
            "download_url": None,
            "error": error,
            **fields,
        }

    def check(self, force: bool = False) -> dict:
        """Return a snapshot; start background discovery if the cache expired."""
        with self._lock:
            now = self._clock()
            if not self.manifest_url:
                return dict(self._result)
            if self._worker is not None:
                if now >= self._deadline and self._result["state"] == "checking":
                    self._result = self._state("failed", error="Update check timed out; retry later.")
                    self._expires = now + self._ttl
                return dict(self._result)
            if not force and now < self._expires:
                return dict(self._result)
            self._result = self._state("checking")
            self._deadline = now + self._timeout
            self._worker = threading.Thread(target=self._run, name="desktop-updates", daemon=True)
            try:
                self._worker.start()
            except RuntimeError:
                self._worker = None
                self._result = self._state("failed", error="Unable to start update check; retry later.")
                self._expires = now + self._ttl
            return dict(self._result)

    def _remaining(self) -> float:
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    def _run(self) -> None:
        try:
            result = self._fetch()
        except (httpx.TimeoutException, TimeoutError):
            result = self._state("failed", error="Update check timed out; retry later.")
        except _InvalidManifest as exc:
            result = self._state("failed", error=str(exc))
        except Exception:
            # Never expose remote bodies, URL query secrets, or transport errors.
            result = self._state("failed", error="Unable to check for updates; retry later.")
        with self._lock:
            now = self._clock()
            if now >= self._deadline:
                result = self._state("failed", error="Update check timed out; retry later.")
            self._result = result
            self._expires = now + self._ttl
            self._worker = None

    def _fetch(self) -> dict:
        current = _stable_version(self.current_version)
        if self._platform != PLATFORM:
            raise _InvalidManifest("Updates are only available for macos-arm64.")
        url, address = _safe_url(self.manifest_url, self._resolver)
        # Pin the validated IP to prevent DNS rebinding between validation and
        # connection. Preserve Host and TLS certificate/SNI hostname verification.
        pinned = url.copy_with(host=address)
        with httpx.Client(transport=self._transport, trust_env=False, follow_redirects=False) as client:
            with client.stream(
                "GET", pinned,
                headers={"Host": url.netloc.decode("ascii"), "Accept": "application/json",
                         "Accept-Encoding": "identity"},
                extensions={"sni_hostname": url.host},
                timeout=httpx.Timeout(self._remaining()),
            ) as response:
                if 300 <= response.status_code < 400:
                    raise _InvalidManifest("Update manifest redirects are not permitted.")
                response.raise_for_status()
                if response.status_code != 200:
                    raise _InvalidManifest("Update manifest must return HTTP 200.")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise _InvalidManifest("Compressed update manifests are not supported.")
                length = response.headers.get("content-length")
                if length is not None and (not length.isascii() or not length.isdecimal()
                                           or int(length) > MAX_MANIFEST_BYTES):
                    raise _InvalidManifest("Update manifest exceeds the size limit or has invalid length.")
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=8192):
                    self._remaining()
                    if len(body) + len(chunk) > MAX_MANIFEST_BYTES:
                        raise _InvalidManifest("Update manifest exceeds the size limit.")
                    body.extend(chunk)
        self._remaining()
        try:
            manifest = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise _InvalidManifest("Update manifest is not valid JSON.") from None
        return self._validate_manifest(manifest, current)

    def _validate_manifest(self, manifest: object, current: tuple[int, int, int]) -> dict:
        if not isinstance(manifest, dict):
            raise _InvalidManifest("Update manifest must be an object.")
        latest = _stable_version(manifest.get("version"))
        notes = manifest.get("release_notes")
        if not isinstance(notes, str) or len(notes) > 65536:
            raise _InvalidManifest("Update release notes must be text within the size limit.")
        published = manifest.get("published_at")
        try:
            if not isinstance(published, str) or len(published) > 64:
                raise ValueError
            if datetime.fromisoformat(published.replace("Z", "+00:00")).tzinfo is None:
                raise ValueError
        except ValueError:
            raise _InvalidManifest("Update publication date must be an ISO 8601 timestamp with timezone.") from None
        platforms = manifest.get("platforms")
        entry = platforms.get(PLATFORM) if isinstance(platforms, dict) else None
        if not isinstance(entry, dict):
            raise _InvalidManifest("Update manifest has no macos-arm64 release.")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            raise _InvalidManifest("Update SHA256 must contain exactly 64 hexadecimal characters.")
        minimum = _macos_version(entry.get("minimum_macos"))
        _safe_url(entry.get("url"), self._resolver)
        self._remaining()
        fields = {"latest_version": manifest["version"], "release_notes": notes}
        if latest <= current:
            return self._state("current", **fields)
        if _macos_version(self._macos) < minimum:
            return self._state("failed", error=f"This update requires macOS {entry['minimum_macos']} or later.", **fields)
        return self._state("available", download_url=entry["url"], **fields)


_service: UpdateService | None = None
_service_lock = threading.Lock()


def get_update_service() -> UpdateService:
    """Return the process-wide service; configuration is captured on first use."""
    global _service
    with _service_lock:
        if _service is None:
            _service = UpdateService()
        return _service
