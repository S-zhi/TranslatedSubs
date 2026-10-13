"""Built-in adapter for local Whisper and the configured HTTP transcriber."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from src.core import transcriber
from src.plugin_system import (
    PluginCancelled,
    PluginContext,
    PluginError,
    PluginManifest,
    TranscribeRequest,
    TranscribeResult,
)
from src.plugin_system.contracts import PluginConfig
from src.plugin_system.errors import sanitize_message


PLUGIN_ID = "builtin.whisper"
_ALLOWED_CONFIG_KEYS = frozenset({
    "backend", "model", "service_url", "credential_ref", "timeout_seconds",
    "device", "compute_type", "download_root", "beam_size",
})
_LOCAL_BACKENDS = {"local", "local_whisper", "whisper", "faster_whisper"}
_HTTP_BACKENDS = {"http", "custom", "custom_http"}
_SENSITIVE_QUERY_KEYS = {"token", "api_key", "key", "password", "auth"}


def _canonical_backend(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized in _LOCAL_BACKENDS:
        return "local_whisper"
    if normalized in _HTTP_BACKENDS:
        return "http"
    return None


def _safe_message(error: Exception, secret: str | None) -> str:
    message = str(error)
    if secret:
        message = message.replace(secret, "[REDACTED]")
    return sanitize_message(message)


class WhisperFactory:
    stage = "transcribe"
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        stage="transcribe",
        implementation_version="1",
        operations=frozenset({"transcribe"}),
    )

    def create_session(self, config: PluginConfig, context: PluginContext):
        return WhisperSession(config, context)


class WhisperSession:
    def __init__(self, config: PluginConfig, context: PluginContext) -> None:
        self._config = dict(config)
        self._context = context
        self._closed = False
        self._secret: str | None = None

    def validate(self, request) -> None:
        if self._closed:
            raise PluginError("invalid_input", "transcribe", PLUGIN_ID, "session is closed")
        if not isinstance(request, TranscribeRequest):
            raise PluginError(
                "invalid_input", "transcribe", PLUGIN_ID,
                "expected a TranscribeRequest",
            )
        if (
            request.stage != "transcribe"
            or request.operation != "transcribe"
            or request.purpose != "transcribe"
            or self._context.purpose != request.purpose
        ):
            raise PluginError(
                "invalid_input", "transcribe", PLUGIN_ID,
                "transcription request identity does not match the invocation",
            )
        if not request.audio_path.is_file():
            raise PluginError(
                "invalid_input", "transcribe", PLUGIN_ID,
                "transcription audio input is missing",
            )
        if request.output_srt.resolve() != (
            self._context.task_dir / "original.srt"
        ).resolve():
            raise PluginError(
                "invalid_input", "transcribe", PLUGIN_ID,
                "transcription output path is invalid",
            )
        try:
            self._validate_config()
            self._resolve_request_model(request)
        except (TypeError, ValueError) as exc:
            raise PluginError(
                "invalid_input", "transcribe", PLUGIN_ID,
                sanitize_message(exc), exc,
            ) from exc

    def run(self, request) -> TranscribeResult:
        self.validate(request)
        self._context.check_cancelled()
        backend = _canonical_backend(self._config["backend"])
        secret = self._resolve_secret() if backend == "http" else None
        service = self._create_service(backend, secret)
        self._secret = secret
        model_name = self._resolve_request_model(request)

        try:
            result = transcriber.transcribe(
                request.audio_path,
                self._context.task_id,
                language=request.source_lang,
                model_name=model_name,
                backend=self._config["backend"],
                service=service,
                cancel_check=self._core_cancel_check,
                on_progress=lambda progress: self._on_progress(progress.percent),
            )
        except PluginCancelled:
            raise
        except transcriber.TranscribeCancelledError as exc:
            raise PluginCancelled("transcription cancelled") from exc
        except Exception as exc:
            self._context.check_cancelled()
            core_code = getattr(exc, "code", None)
            missing_dependency = self._caused_by_missing_dependency(exc)
            code = core_code or ("dependency_missing" if missing_dependency else "execution_error")
            message = (
                "Transcription dependency is unavailable; install the configured backend dependencies"
                if missing_dependency and not core_code
                else _safe_message(exc, self._secret)
            )
            raise PluginError(
                code,
                "transcribe",
                PLUGIN_ID,
                message,
                exc,
            ) from exc

        self._context.check_cancelled()
        if not isinstance(result, transcriber.TranscribeResult):
            raise PluginError(
                "invalid_result", "transcribe", PLUGIN_ID,
                "transcriber returned an invalid result",
            )
        output = Path(result.srt_path).resolve()
        if output != request.output_srt.resolve():
            raise PluginError(
                "invalid_result", "transcribe", PLUGIN_ID,
                "transcriber wrote subtitles to an unexpected path",
            )
        if not output.is_file() or output.stat().st_size <= 0:
            raise PluginError(
                "invalid_result", "transcribe", PLUGIN_ID,
                "transcription output is missing or empty",
            )
        return TranscribeResult(
            original_srt=output,
            segments=result.segment_count,
        )

    def close(self) -> None:
        self._secret = None
        self._closed = True

    def _validate_config(self) -> None:
        if set(self._config) - _ALLOWED_CONFIG_KEYS:
            raise ValueError("transcription config contains unsupported fields")
        if set(self._config) != _ALLOWED_CONFIG_KEYS:
            raise ValueError("transcription config is incomplete")
        backend = self._config["backend"]
        if not isinstance(backend, str) or _canonical_backend(backend) is None:
            raise ValueError("transcription backend is unsupported")
        if not isinstance(self._config["model"], str) or not self._config["model"].strip():
            raise ValueError("transcription model is invalid")
        timeout = self._config["timeout_seconds"]
        beam_size = self._config["beam_size"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
            raise ValueError("transcription timeout must be a positive integer")
        if isinstance(beam_size, bool) or not isinstance(beam_size, int) or beam_size < 1:
            raise ValueError("transcription beam size must be a positive integer")

        credential_ref = self._config["credential_ref"]
        service_url = self._config["service_url"]
        if _canonical_backend(backend) == "http":
            if not isinstance(service_url, str) or not service_url.strip():
                raise ValueError("HTTP transcription service URL is missing")
            parsed = urlsplit(service_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.fragment
                or any(
                    key.lower() in _SENSITIVE_QUERY_KEYS
                    for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
                )
            ):
                raise ValueError("transcription service URL contains unsupported credentials")
            if credential_ref not in {None, "settings:transcriber_api_key"}:
                raise ValueError("transcription credential reference is unsupported")
        else:
            if service_url is not None or credential_ref is not None:
                raise ValueError("local transcription must not declare HTTP configuration")
            if self._config["device"] != "cpu":
                raise ValueError("local transcription device must be cpu")
            if self._config["compute_type"] != "int8":
                raise ValueError("local transcription compute type must be int8")
            if self._config["download_root"] is not None:
                raise ValueError("local transcription download root is unsupported")

    def _resolve_request_model(self, request: TranscribeRequest) -> str:
        config_backend = _canonical_backend(self._config["backend"])
        request_backend = _canonical_backend(request.backend)
        model = request.model
        if ":" in model:
            prefix, selected = model.split(":", 1)
            if prefix.strip().lower() in _LOCAL_BACKENDS:
                if config_backend != "local_whisper":
                    raise ValueError("request model selects a different transcription backend")
                model = selected.strip()
        if request_backend != config_backend:
            raise ValueError("request backend does not match the frozen plan")
        if not model or model != self._config["model"]:
            raise ValueError("request model does not match the frozen plan")
        return model

    def _resolve_secret(self) -> str | None:
        ref = self._config["credential_ref"]
        if ref is None:
            return None
        try:
            secret = self._context.resolve_credential(ref)
        except (KeyError, ValueError) as exc:
            raise PluginError(
                "credential_missing", "transcribe", PLUGIN_ID,
                "transcription credential is unavailable", exc,
            ) from exc
        if not secret or not secret.strip():
            raise PluginError(
                "credential_missing", "transcribe", PLUGIN_ID,
                "transcription credential is unavailable",
            )
        return secret

    def _create_service(self, backend: str, secret: str | None):
        if backend == "http":
            return transcriber.HttpTranscriber(
                url=self._config["service_url"],
                api_key=secret,
                timeout=self._config["timeout_seconds"],
            )
        return transcriber.LocalWhisperTranscriber(
            model_name=self._config["model"],
            device="cpu",
            compute_type="int8",
            download_root=None,
            beam_size=self._config["beam_size"],
            require_model_ready=True,
        )

    def _core_cancel_check(self) -> None:
        try:
            self._context.check_cancelled()
        except PluginCancelled as exc:
            raise transcriber.TranscribeCancelledError("transcription cancelled") from exc

    @staticmethod
    def _caused_by_missing_dependency(error: BaseException) -> bool:
        visited: set[int] = set()
        current: BaseException | None = error
        while current is not None and id(current) not in visited:
            if isinstance(current, (FileNotFoundError, ImportError)):
                return True
            visited.add(id(current))
            current = current.__cause__ or current.__context__
        return False

    def _on_progress(self, percent: float | None) -> None:
        self._context.check_cancelled()
        self._context.report_progress(percent)


factory = WhisperFactory()
