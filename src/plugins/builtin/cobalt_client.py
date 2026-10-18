"""Bounded Cobalt resolution and credential-isolated media streaming."""

from __future__ import annotations

import logging
import math
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from uuid import uuid4

import httpx

from src.plugin_system.errors import PluginError

_CHUNK_SIZE = 256 * 1024
_MAX_JSON_BYTES = 1024 * 1024
_MAX_REDIRECTS = 5
_EXTENSIONS = frozenset({
    "mp4", "webm", "mkv", "mov", "m4v", "mp3", "m4a", "ogg", "opus", "wav",
})
_QUALITY = frozenset({
    "max", "4320", "2160", "1440", "1080", "720", "480", "360", "240", "144",
})
_REDIRECTS = {301, 302, 303, 307, 308}
_NETWORK_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.ProxyError)
_PRIVATE_LOG_SCOPE: ContextVar[bool] = ContextVar("cobalt_http_log_scope", default=False)


@dataclass(frozen=True)
class CobaltMedia:
    url: str = field(repr=False)
    filename: str


class CobaltError(PluginError):
    def __init__(
        self, code: str, message: str, *, fallback_allowed: bool = False,
    ) -> None:
        self.fallback_allowed = fallback_allowed
        super().__init__(code, "download", "builtin.cobalt", message)


class _PrivateHTTPLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_LOG_SCOPE.get()


@contextmanager
def _private_http_logs():
    # Filter this invocation only; leave other clients and logging levels alone.
    token = _PRIVATE_LOG_SCOPE.set(True)
    loggers = [logging.getLogger(name) for name in (
        "httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
        "httpcore.proxy", "httpcore.socks", "httpcore._sync.connection_pool",
    )]
    # Applications can rename HTTPX's logger after import (for example httpx2).
    client_module = getattr(httpx, "_client", None)
    actual_logger = getattr(client_module, "logger", None)
    if isinstance(actual_logger, logging.Logger) and actual_logger not in loggers:
        loggers.append(actual_logger)
    log_filter = _PrivateHTTPLogFilter()
    for logger in loggers:
        logger.addFilter(log_filter)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(log_filter)
        _PRIVATE_LOG_SCOPE.reset(token)


def _validated_url(value: object, *, endpoint: bool = False) -> str:
    code = "invalid_input" if endpoint else "invalid_result"
    message = "Cobalt endpoint is invalid." if endpoint else "Cobalt returned an unsafe media URL."
    try:
        if not isinstance(value, str) or not value or len(value) > 16384:
            raise ValueError
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError
        if "\\" in value or "#" in value:
            raise ValueError
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or "@" in parts.netloc
            or parts.port == 0
            or (endpoint and "?" in value)
        ):
            raise ValueError
        # IP/private tunnel hosts are supported for explicitly configured instances.
        return value.rstrip("/") + "/" if endpoint else value
    except (ValueError, TypeError):
        raise CobaltError(code, message) from None


def _media_extension(filename: object) -> str:
    if not isinstance(filename, str) or not filename or len(filename) > 1024:
        raise CobaltError("invalid_result", "Cobalt returned an invalid media filename.")
    decoded = unquote(filename)
    if (
        any(char in decoded for char in "/\\:?\x00")
        or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
        or decoded.strip() != decoded
    ):
        raise CobaltError("invalid_result", "Cobalt returned an unsafe media filename.")
    stem, dot, extension = filename.rpartition(".")
    extension = extension.lower()
    if not stem or not dot or extension not in _EXTENSIONS:
        raise CobaltError("invalid_result", "Cobalt returned an unsupported media file type.")
    return extension


def _http_error(status: int) -> None:
    if status == 200:
        return
    if status in {401, 403}:
        raise CobaltError("access_denied", "Cobalt access was denied; check instance authorization.")
    if status == 429:
        raise CobaltError("rate_limited", "Cobalt rate limit was reached.", fallback_allowed=True)
    if 500 <= status <= 599:
        raise CobaltError("service_unavailable", "Cobalt service is unavailable.", fallback_allowed=True)
    if status in _REDIRECTS:
        raise CobaltError("invalid_result", "Cobalt API redirects are not supported; check the endpoint.")
    raise CobaltError("execution_error", "Cobalt request was rejected; check the endpoint and source.")


def _upstream_error(error: object) -> None:
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        raise CobaltError("invalid_result", "Cobalt returned a malformed error response.")
    code = error["code"].removeprefix("error.")
    if code.startswith(("api.auth.", "api.content.paid", "api.youtube.drm")) or code in {
        "api.content.video.private", "api.content.video.age", "api.content.video.region",
    }:
        raise CobaltError("access_denied", "Cobalt cannot access this protected or restricted source.")
    if code in {"api.rate_exceeded", "api.fetch.rate", "api.capacity"}:
        raise CobaltError("service_unavailable", "Cobalt is temporarily unavailable.", fallback_allowed=True)
    if code in {
        "api.service.unsupported", "api.service.disabled", "api.service.audio_not_supported",
        "api.link.unsupported", "api.youtube.no_matching_format",
    }:
        raise CobaltError("unsupported_source", "Cobalt does not support this source or format.", fallback_allowed=True)
    raise CobaltError("execution_error", "Cobalt could not resolve this source.")


def _positive_length(value: str | None, *, strict: bool = False) -> int | None:
    if value is None:
        return None
    if len(value) <= 20 and re.fullmatch(r"[0-9]+", value):
        length = int(value)
        if length > 0:
            return length
    if strict:
        raise CobaltError("invalid_result", "Cobalt returned an invalid media length.")
    return None


def _checked_chunks(response, check_cancelled):
    # Do not buffer until 256 KiB before checking cancellation on slow streams.
    for incoming in response.iter_bytes():
        check_cancelled()
        for offset in range(0, len(incoming), _CHUNK_SIZE):
            check_cancelled()
            yield incoming[offset:offset + _CHUNK_SIZE]


def _check_media_prefix(prefix: bytes, content_type: str, extension: str) -> None:
    # Binary box lengths can contain '<', '{', or '['; never strip their NUL bytes.
    document_type = (
        content_type.startswith("text/") or "json" in content_type
        or "html" in content_type or "xml" in content_type
    )
    valid = False
    if extension in {"mp4", "mov", "m4v", "m4a"} and len(prefix) >= 12:
        size = int.from_bytes(prefix[:4], "big")
        atom = prefix[4:8]
        if atom == b"ftyp":
            valid = size >= 16 or (
                size == 1 and len(prefix) >= 24
                and int.from_bytes(prefix[8:16], "big") >= 24
            )
        elif extension == "mov" and atom in {b"moov", b"mdat", b"wide", b"free", b"skip"}:
            # Older QuickTime files can precede ftyp or omit it entirely.
            valid = size == 0 or size >= 8 or (
                size == 1 and len(prefix) >= 16
                and int.from_bytes(prefix[8:16], "big") >= 16
            )
    elif extension in {"webm", "mkv"}:
        valid = len(prefix) >= 5 and prefix.startswith(b"\x1a\x45\xdf\xa3")
    elif extension in {"ogg", "opus"}:
        valid = len(prefix) >= 27 and prefix.startswith(b"OggS\x00")
    elif extension == "wav":
        valid = len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WAVE"
    elif extension == "mp3":
        valid = (
            len(prefix) >= 10 and prefix.startswith(b"ID3")
            and prefix[3] in {2, 3, 4} and prefix[4] != 255
            and all(value < 128 for value in prefix[6:10])
        ) or (
            len(prefix) >= 4 and prefix[0] == 255 and prefix[1] & 224 == 224
            and (prefix[1] >> 3) & 3 != 1 and (prefix[1] >> 1) & 3 == 1
            and prefix[2] >> 4 != 15 and (prefix[2] >> 2) & 3 != 3
        )
    if document_type or not valid:
        raise CobaltError("invalid_result", "Cobalt returned invalid media content.")


class CobaltClient:
    def __init__(
        self, *, endpoint, api_key=None, proxy=None, socket_timeout=30,
    ) -> None:
        self._endpoint = _validated_url(endpoint, endpoint=True)
        self._closed = False
        self._api_client: httpx.Client | None = None
        self._media_client: httpx.Client | None = None
        if (
            isinstance(socket_timeout, bool)
            or not isinstance(socket_timeout, (int, float))
            or not math.isfinite(socket_timeout)
            or not 0 < socket_timeout <= 300
        ):
            raise CobaltError("invalid_input", "Cobalt timeout must be between 1 and 300 seconds.")
        if api_key is not None and (
            not isinstance(api_key, str) or not api_key
            or any(ord(char) <= 32 or ord(char) >= 127 for char in api_key)
        ):
            raise CobaltError("credential_missing", "Cobalt API key is missing or invalid.")
        self._api_key = api_key
        self._proxy = self._validate_proxy(proxy)
        self._timeout = httpx.Timeout(socket_timeout)
        try:
            self._api_client = self._new_client()
            self._media_client = self._new_client()
        except ImportError:
            self.close()
            raise CobaltError(
                "dependency_missing", "Cobalt HTTP client dependencies are unavailable; check the HTTPX installation.",
            ) from None
        except (ValueError, httpx.InvalidURL):
            self.close()
            raise CobaltError("invalid_input", "Cobalt HTTP client configuration is invalid.") from None

    @staticmethod
    def _validate_proxy(proxy):
        if proxy is None:
            return None
        try:
            if not isinstance(proxy, str) or any(char.isspace() for char in proxy):
                raise ValueError
            parts = urlsplit(proxy)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname or "@" in parts.netloc
                or parts.query or parts.fragment or parts.port == 0
                or "\\" in proxy
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise CobaltError(
                "invalid_input", "Cobalt supports HTTP(S) proxies only; choose an HTTP proxy without embedded credentials.",
            ) from None
        return proxy

    def _new_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self._timeout, proxy=self._proxy, trust_env=False,
            follow_redirects=False, headers={"Accept-Encoding": "identity"},
        )

    def _check_open(self) -> None:
        if self._closed:
            raise CobaltError("invalid_input", "Cobalt client is closed.")

    def resolve(
        self, url, *, video_quality, download_mode, filename_style,
        audio_format, check_cancelled,
    ) -> CobaltMedia:
        self._check_open()
        check_cancelled()
        try:
            # Page fragments identify playback positions, not media HTTP targets.
            source_url = url.split("#", 1)[0] if isinstance(url, str) else url
            source_url = _validated_url(source_url)
        except CobaltError:
            raise CobaltError("invalid_input", "Cobalt source URL is invalid.") from None
        if (
            not isinstance(video_quality, str) or video_quality not in _QUALITY
            or download_mode not in {"auto", "audio", "mute"}
            or filename_style not in {"classic", "pretty", "basic", "nerdy"}
            or audio_format not in {"best", "mp3", "ogg", "wav", "opus"}
        ):
            raise CobaltError("invalid_input", "Cobalt request options are invalid.")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Api-Key {self._api_key}"
        payload = {
            "url": source_url, "videoQuality": video_quality, "downloadMode": download_mode,
            "filenameStyle": filename_style, "audioFormat": audio_format,
            "localProcessing": "disabled",
        }
        try:
            with _private_http_logs():
                self._api_client.cookies.clear()
                with self._api_client.stream(
                    "POST", self._endpoint, json=payload, headers=headers,
                    follow_redirects=False,
                ) as response:
                    check_cancelled()
                    http_failure = response.status_code == 429 or 500 <= response.status_code <= 599
                    if response.status_code not in {200, 400} and not http_failure:
                        _http_error(response.status_code)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise CobaltError("invalid_result", "Cobalt returned an unsupported API encoding.")
                    body = bytearray()
                    for chunk in _checked_chunks(response, check_cancelled):
                        check_cancelled()
                        body.extend(chunk)
                        if len(body) > _MAX_JSON_BYTES:
                            raise CobaltError("invalid_result", "Cobalt response is too large.")
                    check_cancelled()
                    try:
                        result = httpx.Response(200, content=bytes(body)).json()
                    except (ValueError, UnicodeError):
                        if http_failure:
                            _http_error(response.status_code)
                        raise CobaltError("invalid_result", "Cobalt returned invalid JSON.") from None
                    if http_failure:
                        # An explicit restriction wins over a retryable HTTP status.
                        if isinstance(result, dict) and result.get("status") == "error":
                            error = result.get("error")
                            if isinstance(error, dict) and isinstance(error.get("code"), str):
                                _upstream_error(error)
                        _http_error(response.status_code)
                    if response.status_code == 400 and (
                        not isinstance(result, dict) or result.get("status") != "error"
                    ):
                        raise CobaltError("invalid_result", "Cobalt returned an invalid error response.")
        except _NETWORK_ERRORS:
            check_cancelled()
            raise CobaltError("service_unavailable", "Cobalt connection failed or timed out.", fallback_allowed=True) from None
        except (httpx.HTTPError, httpx.InvalidURL):
            check_cancelled()
            raise CobaltError("invalid_result", "Cobalt returned an invalid HTTP response.") from None
        return self._parse_result(result)

    @staticmethod
    def _parse_result(result) -> CobaltMedia:
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise CobaltError("invalid_result", "Cobalt returned an invalid result.")
        status = result["status"]
        if status in {"picker", "local-processing"}:
            raise CobaltError(
                "unsupported_response", "Cobalt requires an unsupported multi-item or local-processing workflow.",
                fallback_allowed=True,
            )
        if status == "error":
            _upstream_error(result.get("error"))
        if status not in {"tunnel", "redirect"}:
            raise CobaltError("invalid_result", "Cobalt returned an unknown result type.")
        media_url = _validated_url(result.get("url"))
        _media_extension(result.get("filename"))
        return CobaltMedia(media_url, result["filename"])

    def download(
        self, media, *, target_dir, source_stem, check_cancelled, on_progress,
    ) -> Path:
        self._check_open()
        check_cancelled()
        if not isinstance(media, CobaltMedia) or source_stem != "source":
            raise CobaltError("invalid_input", "Cobalt download target is invalid.")
        media_url = _validated_url(media.url)
        extension = _media_extension(media.filename)
        root = Path(target_dir).absolute()
        destination = root / f"source.{extension}"
        temp = root / f".cobalt-{uuid4().hex}.part"
        owned_temp = False
        published_identity = None
        completed = False
        try:
            self._check_path(root, destination)
            root.mkdir(parents=True, exist_ok=True)
            self._check_path(root, destination)
            with temp.open("xb") as output:
                owned_temp = True
                self._stream_media(
                    media_url, output=output, check_cancelled=check_cancelled,
                    on_progress=on_progress, extension=extension,
                )
                output.flush()
                identity = os.fstat(output.fileno())
            check_cancelled()
            self._check_path(root, destination)
            os.replace(temp, destination)
            owned_temp = False
            published_identity = (identity.st_dev, identity.st_ino)
            check_cancelled()
            on_progress(100.0)
            check_cancelled()
            completed = True
            return destination
        except OSError:
            raise CobaltError("execution_error", "Cobalt could not write the media file; check the task directory.") from None
        finally:
            if owned_temp:
                temp.unlink(missing_ok=True)
            # Only remove this call's artifact after a post-publish failure.
            if published_identity is not None and not completed:
                try:
                    current = destination.lstat()
                    if (current.st_dev, current.st_ino) == published_identity:
                        destination.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _check_path(root: Path, destination: Path) -> None:
        if root.is_symlink() or root.resolve() != root or destination.is_symlink():
            raise CobaltError("invalid_result", "Cobalt download path must not contain symlinks or escape the task directory.")
        if destination.parent != root or (destination.exists() and not destination.is_file()):
            raise CobaltError("invalid_result", "Cobalt download destination is unsafe.")

    def _stream_media(self, url, *, output, check_cancelled, on_progress, extension) -> None:
        try:
            with _private_http_logs():
                for hop in range(_MAX_REDIRECTS + 1):
                    check_cancelled()
                    self._media_client.cookies.clear()
                    with self._media_client.stream(
                        "GET", url, follow_redirects=False,
                        headers={"Accept": "*/*", "Accept-Encoding": "identity"},
                    ) as response:
                        check_cancelled()
                        if response.status_code in _REDIRECTS:
                            location = response.headers.get("location")
                            if not location or hop == _MAX_REDIRECTS:
                                raise CobaltError("invalid_result", "Cobalt media redirect limit or destination is invalid.")
                            if any(char.isspace() or ord(char) < 32 for char in location):
                                raise CobaltError("invalid_result", "Cobalt returned an unsafe media redirect.")
                            try:
                                url = _validated_url(urljoin(url, location))
                            except ValueError:
                                raise CobaltError("invalid_result", "Cobalt returned an unsafe media redirect.") from None
                            continue
                        _http_error(response.status_code)
                        self._copy_media(response, output, check_cancelled, on_progress, extension)
                        return
        except _NETWORK_ERRORS:
            check_cancelled()
            raise CobaltError("service_unavailable", "Cobalt media connection failed or timed out.", fallback_allowed=True) from None
        except (httpx.HTTPError, httpx.InvalidURL):
            check_cancelled()
            raise CobaltError("invalid_result", "Cobalt media transfer was incomplete or invalid.") from None

    @staticmethod
    def _copy_media(response, output, check_cancelled, on_progress, extension) -> None:
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise CobaltError("invalid_result", "Cobalt returned an unsupported media encoding.")
        length = _positive_length(response.headers.get("content-length"), strict=True)
        estimate = length or _positive_length(response.headers.get("estimated-content-length"))
        content_type = response.headers.get("content-type", "").lower()
        count = 0
        prefix = bytearray()
        on_progress(0.0 if estimate else None)
        for chunk in _checked_chunks(response, check_cancelled):
            check_cancelled()
            if not chunk:
                continue
            if len(prefix) < 32:
                prefix.extend(chunk[:32 - len(prefix)])
            count += len(chunk)
            if length is not None and count > length:
                raise CobaltError("invalid_result", "Cobalt media length does not match the response.")
            output.write(chunk)
            on_progress(min(99.0, 100.0 * count / estimate) if estimate else None)
            check_cancelled()
        if not count or (length is not None and count != length):
            raise CobaltError("invalid_result", "Cobalt media is empty or truncated.")
        _check_media_prefix(bytes(prefix), content_type, extension)
        check_cancelled()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._api_key = None
        try:
            if self._api_client is not None:
                self._api_client.close()
        finally:
            if self._media_client is not None:
                self._media_client.close()
