"""Built-in yt-dlp download and URL probe adapter."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from src.core import downloader
from src.plugin_system import (
    DownloadRequest,
    DownloadResult,
    PluginCancelled,
    PluginContext,
    PluginError,
    PluginManifest,
    ProbeFormat,
    ProbeRequest,
    ProbeResult,
)
from src.plugin_system.contracts import PluginConfig
from src.plugin_system.errors import sanitize_message
from src.plugins.builtin.configuration import BuiltinFactory


PLUGIN_ID = "builtin.ytdlp"
_ALLOWED_CONFIG_KEYS = frozenset({
    "quality", "format_selector", "proxy", "socket_timeout", "cookies_ref",
    "merge_output_format", "retries", "concurrent_fragments",
})
_QUALITY_RE = re.compile(r"^(?:best|1080p|720p|480p|360p|audio_only|\d+p?)$", re.I)
_SENSITIVE_QUERY_KEYS = {"token", "api_key", "key", "password", "auth"}


def _contains_url_secret(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.username
            or parsed.password
            or parsed.fragment
            or any(
                key.lower() in _SENSITIVE_QUERY_KEYS
                for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
            )
        )
    except ValueError:
        return True


class YtDlpFactory(BuiltinFactory):
    stage = "download"
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        stage="download",
        implementation_version="1",
        operations=frozenset({"download"}),
        capabilities=frozenset({"probe"}),
    )

    def create_session(self, config: PluginConfig, context: PluginContext):
        return YtDlpSession(config, context)


class YtDlpSession:
    def __init__(self, config: PluginConfig, context: PluginContext) -> None:
        self._config = dict(config)
        self._context = context
        self._closed = False
        self._cookie_path: Path | None = None

    def validate(self, request) -> None:
        if self._closed:
            raise PluginError("invalid_input", "download", PLUGIN_ID, "session is closed")
        if not isinstance(request, (DownloadRequest, ProbeRequest)):
            raise PluginError(
                "invalid_input", "download", PLUGIN_ID,
                "expected a DownloadRequest or ProbeRequest",
            )
        if isinstance(request, DownloadRequest):
            if (
                request.stage != "download"
                or request.operation != "download"
                or request.purpose != "download"
                or self._context.purpose != request.purpose
            ):
                raise PluginError(
                    "invalid_input", "download", PLUGIN_ID,
                    "download request identity does not match the invocation",
                )
            if request.target_dir.resolve() != self._context.task_dir.resolve():
                raise PluginError(
                    "invalid_input", "download", PLUGIN_ID,
                    "download target directory does not match the task directory",
                )
            if request.source_stem != "source":
                raise PluginError(
                    "invalid_input", "download", PLUGIN_ID,
                    "download source stem is unsupported",
                )
            if request.quality != self._config.get("quality"):
                raise PluginError(
                    "invalid_input", "download", PLUGIN_ID,
                    "download quality does not match the frozen plan",
                )
        else:
            if not isinstance(request.url, str) or not request.url.strip():
                raise PluginError(
                    "invalid_input", "download", PLUGIN_ID,
                    "probe URL is required",
                )
            if request.cookies_from_browser is not None:
                browser = request.cookies_from_browser.strip().lower()
                if browser not in downloader.BROWSER_COOKIE_SOURCES:
                    # Preserve the core's established probe failure shape.
                    return
        try:
            self._validate_config()
        except (TypeError, ValueError) as exc:
            raise PluginError(
                "invalid_input", "download", PLUGIN_ID,
                sanitize_message(exc), exc,
            ) from exc

    def run(self, request):
        self.validate(request)
        if isinstance(request, ProbeRequest):
            return self._run_probe(request)
        return self._run_download(request)

    def close(self) -> None:
        self._cookie_path = None
        self._closed = True

    def _validate_config(self) -> None:
        if set(self._config) - _ALLOWED_CONFIG_KEYS:
            raise ValueError("download config contains unsupported fields")
        required = _ALLOWED_CONFIG_KEYS
        if not required.issubset(self._config):
            raise ValueError("download config is incomplete")
        quality = self._config["quality"]
        if not isinstance(quality, str) or not _QUALITY_RE.fullmatch(quality):
            raise ValueError("download quality is invalid")
        selector = self._config["format_selector"]
        if selector is not None and not isinstance(selector, str):
            raise ValueError("download format selector is invalid")
        proxy = self._config["proxy"]
        if proxy is not None:
            if not isinstance(proxy, str):
                raise ValueError("download proxy is invalid")
            parsed = urlsplit(proxy)
            if (
                parsed.scheme not in {"http", "https", "socks4", "socks4a", "socks5"}
                or not parsed.netloc
                or _contains_url_secret(proxy)
            ):
                raise ValueError("download proxy must not contain embedded credentials")
        if self._config["cookies_ref"] not in {None, "settings:cookies_file"}:
            raise ValueError("download cookie reference is unsupported")
        timeout = self._config["socket_timeout"]
        retries = self._config["retries"]
        fragments = self._config["concurrent_fragments"]
        merge_format = self._config["merge_output_format"]
        for key, value in (
            ("socket_timeout", timeout),
            ("retries", retries),
            ("concurrent_fragments", fragments),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"download {key} must be a positive integer")
        if not isinstance(merge_format, str) or not merge_format.strip():
            raise ValueError("download merge output format is invalid")

    def _resolve_cookies(self) -> Path | None:
        ref = self._config["cookies_ref"]
        if ref is None:
            self._cookie_path = None
            return None
        try:
            raw_path = self._context.resolve_credential(ref)
        except (KeyError, ValueError) as exc:
            raise PluginError(
                "credential_missing", "download", PLUGIN_ID,
                "download cookie file reference is unavailable", exc,
            ) from exc
        if not raw_path:
            raise PluginError(
                "credential_missing", "download", PLUGIN_ID,
                "download cookie file reference is unavailable",
            )
        self._cookie_path = Path(raw_path)
        return self._cookie_path

    def _run_download(self, request: DownloadRequest) -> DownloadResult:
        cookies = self._resolve_cookies()
        try:
            core_result = downloader.download_video(
                request.url,
                self._context.task_id,
                on_progress=lambda progress: self._on_progress(progress.percent),
                cookies_file=cookies,
                format_selector=self._config["format_selector"],
                quality=request.quality,
                proxy=self._config["proxy"],
                socket_timeout=self._config["socket_timeout"],
                merge_output_format=self._config["merge_output_format"],
                retries=self._config["retries"],
                concurrent_fragments=self._config["concurrent_fragments"],
                cancel_check=self._context.check_cancelled,
            )
        except PluginCancelled:
            raise
        except Exception as exc:
            self._context.check_cancelled()
            message = str(exc)
            if cookies is not None:
                message = message.replace(str(cookies), "[REDACTED]")
            raise PluginError(
                getattr(exc, "code", "execution_error"),
                "download",
                PLUGIN_ID,
                sanitize_message(message),
                exc,
            ) from exc

        self._context.check_cancelled()
        path = Path(core_result.video_path).resolve()
        if (
            path.parent != self._context.task_dir.resolve()
            or not path.name.startswith(f"{request.source_stem}.")
            or not path.is_file()
            or path.stat().st_size <= 0
        ):
            raise PluginError(
                "invalid_result", "download", PLUGIN_ID,
                "downloaded source artifact is missing or invalid",
            )
        return DownloadResult(
            source_path=path,
            title=core_result.title,
            duration_seconds=core_result.duration,
        )

    def _run_probe(self, request: ProbeRequest) -> ProbeResult:
        cookies = self._resolve_cookies()
        selector = self._config["format_selector"]
        if not selector:
            selector = downloader.resolve_download_format(
                quality=self._config["quality"],
                default_format="bv*+ba/b",
            )
        core_result = downloader.probe_video(
            request.url,
            cookies_file=cookies,
            cookies_from_browser=request.cookies_from_browser,
            format_selector=selector,
            force_refresh=request.force_refresh,
            proxy=self._config["proxy"],
            socket_timeout=self._config["socket_timeout"],
        )
        formats = tuple(
            ProbeFormat(
                formatId=str(item.get("formatId") or ""),
                ext=str(item.get("ext") or ""),
                resolution=str(item.get("resolution") or ""),
                height=item.get("height"),
                width=item.get("width"),
                fps=item.get("fps"),
                vcodec=item.get("vcodec"),
                acodec=item.get("acodec"),
                filesize=item.get("filesize"),
                tbr=item.get("tbr"),
                note=item.get("note"),
                type=item.get("type") if item.get("type") in {"video", "audio"} else "video",
            )
            for item in core_result.formats
        )
        return ProbeResult(
            ok=core_result.ok,
            title=core_result.title,
            extractor=core_result.extractor,
            duration=core_result.duration,
            formats_count=core_result.formats_count,
            webpage_url=core_result.webpage_url,
            reason=core_result.reason,
            detail=core_result.detail,
            cached=core_result.cached,
            language=core_result.language,
            available_qualities=tuple(core_result.available_qualities),
            formats=formats,
            thumbnail=core_result.thumbnail,
            uploader=core_result.uploader,
        )

    def _on_progress(self, percent: float | None) -> None:
        self._context.check_cancelled()
        self._context.report_progress(percent)


factory = YtDlpFactory()
