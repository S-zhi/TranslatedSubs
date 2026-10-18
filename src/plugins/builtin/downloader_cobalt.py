"""Cobalt download/probe sessions with explicitly opted-in yt-dlp fallback."""

from __future__ import annotations

import logging
import re
import unicodedata
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from src.core import downloader
from src.plugin_system import (
    DownloadRequest,
    DownloadResult,
    PluginCancelled,
    PluginContext,
    PluginError,
    PluginManifest,
    ProbeRequest,
    ProbeResult,
)
from src.plugin_system.contracts import PluginConfig
from src.plugins.builtin.cobalt_client import CobaltClient, CobaltError
from src.plugins.builtin.cobalt_configuration import (
    COBALT_CONFIG_SCHEMA,
    PLUGIN_ID,
    YTDLP_CONFIG_KEYS,
    preflight_cobalt,
    validate_cobalt_config,
)
from src.plugins.builtin.downloader_ytdlp import YtDlpFactory

logger = logging.getLogger(__name__)
_HEIGHTS = frozenset({"4320", "2160", "1440", "1080", "720", "480", "360", "240", "144"})
_EXPLICIT_ERRORS = frozenset({"invalid_input", "credential_missing", "dependency_missing"})
_FALLBACK_CODES = _EXPLICIT_ERRORS | {
    "execution_error", "invalid_result", "cleanup_error", "plugin_unavailable", "version_mismatch",
}


def _quality_options(quality: str) -> tuple[str, str]:
    normalized = quality.lower()
    if normalized == "best":
        return "max", "auto"
    if normalized == "audio_only":
        return "max", "audio"
    height = normalized.removesuffix("p")
    if height in _HEIGHTS:
        return height, "auto"
    raise PluginError("invalid_input", "download", PLUGIN_ID, "Cobalt download quality is unsupported")


def _filename_title(filename: str) -> str:
    """Use only a printable filename stem, never a remote path or media URL."""
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem = Path(basename).stem
    stem = "".join(char for char in stem if not unicodedata.category(char).startswith("C"))
    return re.sub(r'[<>:"/\\|?*]', "_", stem).strip(" .")[:160] or "source"


class CobaltFactory:
    """Catalog and preflight work without constructing an HTTP client."""

    stage = "download"
    config_schema_version = 1
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        stage="download",
        implementation_version="1",
        operations=frozenset({"download"}),
        capabilities=frozenset({"probe"}),
    )

    def describe(self) -> dict:
        return {
            "name": "Cobalt 下载",
            "description": (
                "通过自托管或已授权的 Cobalt 服务下载；API Key 在运行设置中配置；"
                "探测不提供格式列表，可显式开启 yt-dlp 回退"
            ),
            "config_schema": deepcopy(COBALT_CONFIG_SCHEMA),
            "bindings": [],
            "config_schema_version": self.config_schema_version,
        }

    def validate_config(self, config, *, purpose=None, partial=True):
        return validate_cobalt_config(config, purpose=purpose, partial=partial)

    def preflight(self, config, *, operation, purpose, source_lang="", target_lang=""):
        return preflight_cobalt(
            config, operation=operation, purpose=purpose,
            source_lang=source_lang, target_lang=target_lang,
        )

    def create_session(self, config: PluginConfig, context: PluginContext):
        return CobaltSession(config, context)


class CobaltSession:
    def __init__(self, config: PluginConfig, context: PluginContext) -> None:
        self._config = MappingProxyType(dict(config))
        self._context = context
        self._closed = False

    def validate(self, request) -> None:
        if self._closed:
            raise self._error("invalid_input", "Cobalt session is closed")
        if not isinstance(request, (DownloadRequest, ProbeRequest)):
            raise self._error("invalid_input", "expected a DownloadRequest or ProbeRequest")
        if self._context.purpose != "download":
            raise self._error("invalid_input", "Cobalt invocation purpose must be download")
        if not isinstance(request.url, str) or not request.url.strip():
            raise self._error("invalid_input", "Cobalt requires an HTTP(S) source URL")
        try:
            parsed = urlsplit(request.url)
            valid_url = (
                parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and not any(char.isspace() or ord(char) < 32 for char in request.url)
            )
            parsed.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise self._error("invalid_input", "Cobalt requires an HTTP(S) source URL without credentials")
        if isinstance(request, ProbeRequest) and request.cookies_from_browser is not None:
            raise self._error(
                "invalid_input", "Cobalt does not support browser-cookie probes; select yt-dlp for this mode",
            )
        try:
            validated = validate_cobalt_config(self._config, purpose="download", partial=False)
        except (TypeError, ValueError) as exc:
            raise self._error("invalid_input", str(exc), exc) from exc
        self._config = MappingProxyType(validated)
        _quality_options(self._config["quality"])
        if isinstance(request, DownloadRequest):
            if (request.stage, request.operation, request.purpose) != ("download",) * 3:
                raise self._error("invalid_input", "download request identity does not match the invocation")
            if request.quality != self._config["quality"]:
                raise self._error("invalid_input", "download quality does not match the frozen plan")
            if request.source_stem != "source":
                raise self._error("invalid_input", "download source stem is unsupported")
            try:
                valid_target = (
                    isinstance(request.target_dir, Path)
                    and request.target_dir.is_absolute()
                    and request.target_dir.resolve() == self._context.task_dir.resolve()
                )
            except (OSError, RuntimeError):
                valid_target = False
            if not valid_target:
                raise self._error("invalid_input", "download target directory does not match the task directory")

    def run(self, request):
        self.validate(request)
        self._context.check_cancelled()
        try:
            result = self._run_cobalt(request)
        except CobaltError as exc:
            self._context.check_cancelled()
            logger.warning("Cobalt operation failed; provider=cobalt code=%s", exc.code)
            if exc.code in _EXPLICIT_ERRORS:
                raise
            if (
                self._config["fallback_policy"] == "ytdlp" and exc.fallback_allowed
                and not hasattr(exc, "cleanup_error")
            ):
                logger.warning("Cobalt unavailable; switching provider=cobalt -> yt-dlp (explicit fallback)")
                return self._run_fallback(request, exc)
            if isinstance(request, ProbeRequest):
                return ProbeResult(
                    ok=False, extractor="cobalt", webpage_url=request.url,
                    reason=exc.code, detail=exc.message,
                )
            raise
        self._context.check_cancelled()
        logger.info("Cobalt operation completed; provider=cobalt")
        return result

    def close(self) -> None:
        # Each run owns and closes its HTTP client/child session before returning.
        self._closed = True

    def _resolve_api_key(self) -> str | None:
        reference = self._config["credential_ref"]
        if reference is None:
            return None
        try:
            key = self._context.resolve_credential(reference)
        except (KeyError, ValueError) as exc:
            raise self._error("credential_missing", "Cobalt API key is unavailable in runtime settings", exc) from exc
        if not isinstance(key, str) or not key.strip():
            raise self._error("credential_missing", "Cobalt API key is unavailable in runtime settings")
        return key

    @contextmanager
    def _closing(self, resource, provider: str):
        primary = None
        try:
            yield resource
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                resource.close()
            except Exception as cleanup_error:
                logger.warning("Download resource cleanup failed; provider=%s", provider)
                if primary is not None:
                    setattr(primary, "cleanup_error", cleanup_error)
                if isinstance(cleanup_error, PluginCancelled):
                    raise
                if primary is None:
                    self._context.check_cancelled()
                    raise self._error("cleanup_error", f"{provider} resource cleanup failed", cleanup_error) from cleanup_error

    def _run_cobalt(self, request):
        api_key = self._resolve_api_key()
        self._context.check_cancelled()
        client = CobaltClient(
            endpoint=self._config["endpoint"], api_key=api_key,
            proxy=self._config["proxy"], socket_timeout=self._config["socket_timeout"],
        )
        with self._closing(client, "cobalt"):
            self._context.check_cancelled()
            if isinstance(request, ProbeRequest):
                media = self._resolve(client, request.url)
                self._context.check_cancelled()
                return ProbeResult(
                    ok=True, title=_filename_title(media.filename), extractor="cobalt",
                    webpage_url=request.url, duration=None, formats_count=0,
                    available_qualities=(), formats=(),
                )
            # This context has ended before run() can create a yt-dlp child.
            with downloader._download_limiter.slot():
                self._context.check_cancelled()
                media = self._resolve(client, request.url)
                path = client.download(
                    media, target_dir=request.target_dir, source_stem=request.source_stem,
                    check_cancelled=self._context.check_cancelled, on_progress=self._on_progress,
                )
                self._context.check_cancelled()
            path = Path(path)
            if (
                path.is_symlink() or path.resolve().parent != self._context.task_dir.resolve()
                or not path.name.startswith("source.") or not path.is_file()
                or path.stat().st_size <= 0
            ):
                raise self._error("invalid_result", "Cobalt source artifact is missing or invalid")
            return DownloadResult(
                source_path=path.resolve(), title=_filename_title(media.filename), duration_seconds=None,
            )

    def _resolve(self, client: CobaltClient, url: str):
        video_quality, download_mode = _quality_options(self._config["quality"])
        return client.resolve(
            url, video_quality=video_quality, download_mode=download_mode,
            filename_style=self._config["filename_style"], audio_format=self._config["audio_format"],
            check_cancelled=self._context.check_cancelled,
        )

    def _run_fallback(self, request, cobalt_error: CobaltError):
        self._context.check_cancelled()
        legacy = {key: self._config[key] for key in YTDLP_CONFIG_KEYS}
        try:
            child = YtDlpFactory().create_session(legacy, self._context)
            with self._closing(child, "yt-dlp"):
                self._context.check_cancelled()
                child.validate(request)
                self._context.check_cancelled()
                result = child.run(request)
                self._context.check_cancelled()
                expected = ProbeResult if isinstance(request, ProbeRequest) else DownloadResult
                if type(result) is not expected:
                    raise self._error("invalid_result", "yt-dlp fallback returned an invalid result")
            self._context.check_cancelled()
        except PluginCancelled:
            raise
        except Exception as exc:
            self._context.check_cancelled()
            code = getattr(exc, "code", "execution_error")
            if code not in _FALLBACK_CODES:
                code = "execution_error"
            logger.warning("Download fallback failed; provider=yt-dlp code=%s", code)
            message = (
                f"Cobalt failed: {cobalt_error.message}; yt-dlp fallback failed ({code}). "
                "Check source access, cookies, and the frozen yt-dlp configuration."
            )
            if isinstance(request, ProbeRequest) and code not in _EXPLICIT_ERRORS:
                return ProbeResult(
                    ok=False, extractor="yt-dlp (Cobalt fallback)", webpage_url=request.url,
                    reason="Cobalt and yt-dlp probe failed", detail=message,
                )
            combined = self._error(code, message, exc)
            combined.cobalt_error = cobalt_error
            if hasattr(exc, "cleanup_error"):
                combined.cleanup_error = exc.cleanup_error
            raise combined from exc
        if isinstance(result, ProbeResult):
            if not result.ok:
                logger.warning("Download probe failed; provider=yt-dlp (Cobalt fallback)")
                return ProbeResult(
                    ok=False, extractor="yt-dlp (Cobalt fallback)", webpage_url=request.url,
                    reason="Cobalt and yt-dlp probe failed",
                    detail=(
                        f"Cobalt failed: {cobalt_error.message}; yt-dlp could not probe this source. "
                        "Check source access, cookies, and the frozen yt-dlp configuration."
                    ),
                )
            result = replace(result, extractor=f"yt-dlp (Cobalt fallback; {result.extractor or 'unknown extractor'})")
        logger.info("Download operation completed; provider=yt-dlp (Cobalt fallback)")
        return result

    def _on_progress(self, percent: float | None) -> None:
        self._context.check_cancelled()
        self._context.report_progress(percent)

    @staticmethod
    def _error(code: str, message: str, cause=None) -> PluginError:
        return PluginError(code, "download", PLUGIN_ID, message, cause)


factory = CobaltFactory()
