"""Built-in adapter for the frozen translation engine selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from src.core import translator
from src.plugin_system import (
    PluginCancelled,
    PluginContext,
    PluginError,
    PluginManifest,
    TranslateRequest,
    TranslateResult,
)
from src.plugin_system.contracts import PluginConfig
from src.plugin_system.errors import sanitize_message
from src.store.translation_engine_store import LOCAL_TRANSLATION_MODEL


PLUGIN_ID = "builtin.translation_engine"
_ALLOWED_CONFIG_KEYS = frozenset({
    "engine_id", "api_type", "base_url", "model", "credential_ref",
    "batch_size", "local_batch_size", "timeout_seconds",
})
_ENGINE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe_message(error: Exception, secret: str | None) -> str:
    message = str(error)
    if secret:
        message = message.replace(secret, "[REDACTED]")
    return sanitize_message(message)


@dataclass(frozen=True)
class _EngineSnapshot:
    api_type: str
    base_url: str
    model: str
    api_key: str | None
    timeout: int


class TranslatorEngineFactory:
    """Lightweight factory; it does not load a model or resolve credentials."""

    stage = "translate"
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        stage="translate",
        implementation_version="1",
        operations=frozenset({"translate"}),
    )

    def create_session(self, config: PluginConfig, context: PluginContext):
        return TranslatorEngineSession(config, context)


class TranslatorEngineSession:
    def __init__(self, config: PluginConfig, context: PluginContext) -> None:
        self._config = dict(config)
        self._context = context
        self._closed = False
        self._secret: str | None = None

    def validate(self, request) -> None:
        if self._closed:
            raise PluginError(
                "invalid_input", "translate", PLUGIN_ID,
                "translation session is closed",
            )
        if not isinstance(request, TranslateRequest):
            raise PluginError(
                "invalid_input", "translate", PLUGIN_ID,
                "expected a TranslateRequest",
            )
        if request.stage != "translate" or request.operation != "translate":
            raise PluginError(
                "invalid_input", "translate", PLUGIN_ID,
                "request stage or operation is invalid",
            )
        if request.purpose != "translate" or self._context.purpose != request.purpose:
            raise PluginError(
                "invalid_input", "translate", PLUGIN_ID,
                "request purpose does not match the invocation",
            )
        if request.mode not in {"mono", "bilingual"}:
            raise PluginError(
                "invalid_input", "translate", PLUGIN_ID,
                "translation mode is invalid",
            )
        if not request.original_srt.is_file():
            raise PluginError(
                "invalid_input", "translate", PLUGIN_ID,
                "original subtitle file is missing",
            )
        expected_output = self._context.task_dir / "translated.srt"
        if request.output_srt.resolve() != expected_output.resolve():
            raise PluginError(
                "invalid_input", "translate", PLUGIN_ID,
                "translated subtitle output path is invalid",
            )
        try:
            self._validate_config()
        except (TypeError, ValueError) as exc:
            raise PluginError(
                "invalid_input", "translate", PLUGIN_ID,
                str(exc), exc,
            ) from exc

    def run(self, request) -> TranslateResult:
        self.validate(request)
        self._context.check_cancelled()
        snapshot = self._resolve_engine_snapshot()
        try:
            result = translator.translate_srt(
                request.original_srt,
                self._context.task_id,
                request.source_lang,
                request.target_lang,
                mode=request.mode,
                api_key=snapshot.api_key,
                engine_config=snapshot,
                cancel_check=self._context.check_cancelled,
                batch_size=self._config["batch_size"],
                local_batch_size=self._config["local_batch_size"],
                on_progress=lambda progress: self._on_progress(progress.percent),
            )
        except PluginCancelled:
            raise
        except Exception as exc:
            self._context.check_cancelled()
            code = getattr(exc, "code", "execution_error")
            raise PluginError(
                code,
                "translate",
                PLUGIN_ID,
                _safe_message(exc, self._secret),
                exc,
            ) from exc

        self._context.check_cancelled()
        if not isinstance(result, translator.TranslateResult):
            raise PluginError(
                "invalid_result", "translate", PLUGIN_ID,
                "translator returned an invalid result",
            )
        actual_path = Path(result.srt_path).resolve()
        if actual_path != request.output_srt.resolve():
            raise PluginError(
                "invalid_result", "translate", PLUGIN_ID,
                "translator wrote subtitles to an unexpected path",
            )
        if not actual_path.is_file() or actual_path.stat().st_size <= 0:
            raise PluginError(
                "invalid_result", "translate", PLUGIN_ID,
                "translated subtitle output is missing or empty",
            )
        return TranslateResult(
            translated_srt=actual_path,
            segments=result.count,
            bilingual=result.bilingual,
        )

    def close(self) -> None:
        self._closed = True
        self._secret = None

    def _validate_config(self) -> None:
        unknown = set(self._config) - _ALLOWED_CONFIG_KEYS
        if unknown:
            raise ValueError("translation plugin config contains unsupported fields")
        required = {
            "engine_id", "api_type", "base_url", "model", "credential_ref",
            "batch_size", "local_batch_size", "timeout_seconds",
        }
        if not required.issubset(self._config):
            raise ValueError("translation plugin config is incomplete")
        engine_id = self._config["engine_id"]
        if not isinstance(engine_id, str) or not _ENGINE_ID_RE.fullmatch(engine_id):
            raise ValueError("translation engine id is invalid")
        api_type = self._config["api_type"]
        if api_type not in {
            "local_ct2", "openai_compatible", "anthropic_compatible"
        }:
            raise ValueError("translation API type is unsupported")
        for key in ("base_url", "model"):
            if not isinstance(self._config[key], str):
                raise ValueError(f"translation {key} must be a string")
        if api_type == "local_ct2" and self._config["model"] != LOCAL_TRANSLATION_MODEL:
            raise ValueError("local translation model is not supported")
        if api_type != "local_ct2":
            parsed = urlsplit(self._config["base_url"])
            sensitive_query = {"token", "api_key", "key", "password", "auth"}
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.fragment
                or any(
                    key.lower() in sensitive_query
                    for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
                )
            ):
                raise ValueError("translation base URL contains unsupported credentials")
        for key in ("batch_size", "local_batch_size", "timeout_seconds"):
            value = self._config[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"translation {key} must be a positive integer")
        expected_ref = f"translation_engine:{engine_id}:api_key"
        credential_ref = self._config["credential_ref"]
        if api_type == "local_ct2":
            if credential_ref is not None:
                raise ValueError("local translation must not declare a credential")
        elif credential_ref != expected_ref:
            raise ValueError("translation credential reference does not match engine id")

    def _resolve_engine_snapshot(self) -> _EngineSnapshot:
        api_type = self._config["api_type"]
        secret = None
        if api_type != "local_ct2":
            ref = self._config["credential_ref"]
            try:
                secret = self._context.resolve_credential(ref)
            except (KeyError, ValueError) as exc:
                raise PluginError(
                    "credential_missing", "translate", PLUGIN_ID,
                    "translation credential is unavailable", exc,
                ) from exc
            if not secret or not secret.strip():
                raise PluginError(
                    "credential_missing", "translate", PLUGIN_ID,
                    "translation credential is unavailable",
                )
        self._secret = secret
        return _EngineSnapshot(
            api_type=api_type,
            base_url=self._config["base_url"],
            model=self._config["model"],
            api_key=secret,
            timeout=self._config["timeout_seconds"],
        )

    def _on_progress(self, percent: float | None) -> None:
        self._context.check_cancelled()
        self._context.report_progress(percent)


factory = TranslatorEngineFactory()
