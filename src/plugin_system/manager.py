"""Runtime validation and per-invocation plugin session lifecycle."""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Mapping
from types import MappingProxyType
from pathlib import Path
from typing import Any

from src.plugin_system.context import PluginContext
from src.plugin_system.contracts import (
    AudioExtractRequest,
    AudioExtractResult,
    DownloadRequest,
    DownloadResult,
    DubMuxRequest,
    DubMuxResult,
    PluginConfig,
    ProbeRequest,
    ProbeFormat,
    ProbeResult,
    Request,
    Result,
    SubtitleRenderRequest,
    SubtitleRenderResult,
    TTSRequest,
    TTSResult,
    TranscribeRequest,
    TranscribeResult,
    TranslateRequest,
    TranslateResult,
    VocalSeparateRequest,
    VocalSeparateResult,
)
from src.plugin_system.errors import PluginCancelled, PluginError
from src.plugin_system.manifest import PluginManifest
from src.plugin_system.registry import PluginRegistry, RegisteredPlugin


_TASK_OPERATIONS: dict[type[Any], tuple[str, str, type[Any]]] = {
    DownloadRequest: ("download", "download", DownloadResult),
    AudioExtractRequest: ("audio_extract", "audio_extract", AudioExtractResult),
    VocalSeparateRequest: ("vocal_separate", "vocal_separate", VocalSeparateResult),
    TranscribeRequest: ("transcribe", "transcribe", TranscribeResult),
    TranslateRequest: ("translate", "translate", TranslateResult),
    TTSRequest: ("tts", "tts", TTSResult),
    SubtitleRenderRequest: ("render", "subtitle", SubtitleRenderResult),
    DubMuxRequest: ("render", "dub_mux", DubMuxResult),
}
_PURPOSES: dict[type[Any], frozenset[str]] = {
    DownloadRequest: frozenset({"download"}),
    AudioExtractRequest: frozenset({"extract.asr", "extract.dubbing"}),
    VocalSeparateRequest: frozenset({"separate.asr", "separate.dubbing"}),
    TranscribeRequest: frozenset({"transcribe"}),
    TranslateRequest: frozenset({"translate"}),
    TTSRequest: frozenset({"tts"}),
    SubtitleRenderRequest: frozenset({"render.subtitle"}),
    DubMuxRequest: frozenset({"render.dub_mux"}),
}
_REQUEST_PATH_FIELDS: dict[type[Any], tuple[str, ...]] = {
    DownloadRequest: ("target_dir",),
    AudioExtractRequest: ("source_path", "output_path"),
    VocalSeparateRequest: ("audio_path", "vocal_path", "background_path"),
    TranscribeRequest: ("audio_path", "output_srt"),
    TranslateRequest: ("original_srt", "output_srt"),
    TTSRequest: ("translated_srt", "original_srt", "output_audio"),
    SubtitleRenderRequest: ("source_video", "translated_srt", "output_video"),
    DubMuxRequest: (
        "base_video", "source_audio", "speech_audio", "background_audio",
        "vocal_audio", "mixed_audio", "output_video",
    ),
}
_REQUEST_INPUT_FIELDS: dict[type[Any], tuple[str, ...]] = {
    DownloadRequest: (),
    AudioExtractRequest: ("source_path",),
    VocalSeparateRequest: ("audio_path",),
    TranscribeRequest: ("audio_path",),
    TranslateRequest: ("original_srt",),
    TTSRequest: ("translated_srt", "original_srt"),
    SubtitleRenderRequest: ("source_video", "translated_srt"),
    DubMuxRequest: ("base_video", "source_audio", "speech_audio", "background_audio", "vocal_audio"),
}

_ARTIFACT_NAMES = {
    "audio.wav", "tts_source.wav", "vocal.wav", "background.wav",
    "vocal_stem.wav", "original.srt", "translated.srt", "dubbed.wav",
    "dubbed_mix.wav", "output_hard.mp4", "output_soft.mp4",
    "output_dubbed.mp4",
}


class _InvocationContext:
    """Pass through host services while suppressing callbacks after completion."""

    def __init__(self, context: PluginContext) -> None:
        self._context = context
        self._active = True
        self._lock = threading.RLock()

    @property
    def task_id(self) -> str:
        return self._context.task_id

    @property
    def invocation_id(self) -> str:
        return self._context.invocation_id

    @property
    def task_dir(self) -> Path:
        return self._context.task_dir

    @property
    def purpose(self) -> str:
        return self._context.purpose

    def check_cancelled(self) -> None:
        self._context.check_cancelled()

    def report_progress(self, percent: float | None) -> None:
        with self._lock:
            if self._active:
                self._context.report_progress(percent)

    def register_process(self, proc: Any) -> None:
        self._context.register_process(proc)

    def unregister_process(self, proc: Any) -> None:
        self._context.unregister_process(proc)

    def resolve_credential(self, ref: str) -> str:
        return self._context.resolve_credential(ref)

    def finish(self) -> None:
        with self._lock:
            self._active = False


class PluginManager:
    """Runs one plugin call per isolated session and validates its output."""

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry

    def catalog(self) -> list[dict[str, Any]]:
        entries = []
        for registered in self._registry.entries():
            manifest = registered.manifest
            describe = getattr(registered.factory, "describe", None)
            details = describe() if callable(describe) else {}
            entries.append({
                "plugin_id": manifest.plugin_id,
                "implementation_version": manifest.implementation_version,
                "stage": manifest.stage,
                "operations": sorted(manifest.operations),
                "capabilities": sorted(manifest.capabilities),
                "name": manifest.plugin_id,
                "description": "",
                "config_schema": {},
                "config_schema_version": 1,
                "bindings": [],
                **details,
            })
        return entries

    def validate_config(
        self, plugin_id: str, implementation_version: str, config: PluginConfig,
        *, purpose: str | None = None, partial: bool = True,
    ) -> dict[str, Any]:
        registered = self._resolve(plugin_id, implementation_version, None)
        frozen = self._freeze_config(config, registered.manifest)
        validator = getattr(registered.factory, "validate_config", None)
        if callable(validator):
            try:
                return dict(validator(frozen, purpose=purpose, partial=partial))
            except (TypeError, ValueError) as exc:
                raise self._error("invalid_input", registered.manifest, str(exc), exc) from exc
        if purpose is None and config:
            raise self._error("invalid_input", registered.manifest, "plugin has no editable configuration schema")
        return dict(frozen)

    def preflight(
        self, plugin_id: str, implementation_version: str, *, operation: str,
        purpose: str, config: PluginConfig, source_lang: str = "", target_lang: str = "",
    ) -> None:
        registered = self._resolve(plugin_id, implementation_version, None)
        manifest = registered.manifest
        valid = any(
            contract[0] == manifest.stage and contract[1] == operation and purpose in _PURPOSES[request_type]
            for request_type, contract in _TASK_OPERATIONS.items()
        )
        if not valid or operation not in manifest.operations:
            raise self._error("invalid_input", manifest, "preflight operation does not match the plugin")
        frozen = self._freeze_config(config, manifest)
        preflight = getattr(registered.factory, "preflight", None)
        if not callable(preflight):
            raise self._error("plugin_unavailable", manifest, "plugin does not support dependency preflight")
        try:
            preflight(frozen, operation=operation, purpose=purpose,
                      source_lang=source_lang, target_lang=target_lang)
        except PluginError:
            raise
        except (ValueError, TypeError) as exc:
            raise self._error("invalid_input", manifest, str(exc), exc) from exc
        except Exception as exc:
            raise self._error("dependency_missing", manifest, "plugin dependency preflight failed", exc) from exc

    def run(
        self,
        plugin_id: str,
        implementation_version: str,
        request: Request,
        config: PluginConfig,
        context: PluginContext,
    ) -> Result:
        context.check_cancelled()
        request_type = type(request)
        contract = _TASK_OPERATIONS.get(request_type)
        if contract is None:
            raise PluginError("invalid_input", "download", plugin_id, "unsupported request type")
        registered = self._resolve(
            plugin_id, implementation_version, None,
            expected_stage=contract[0],
        )
        stage, operation, _ = contract
        if (
            request.stage != stage
            or request.operation != operation
            or registered.manifest.stage != stage
            or operation not in registered.manifest.operations
            or not isinstance(request.purpose, str)
            or request.purpose not in _PURPOSES[request_type]
            or request.purpose != context.purpose
        ):
            raise self._error(
                "invalid_input", registered.manifest,
                "request, context, and plugin manifest do not match",
            )
        self._validate_request_fields(request, registered.manifest)
        self._validate_request_paths(request, context, registered.manifest)
        frozen_config = self._freeze_config(config, registered.manifest)
        return self._invoke(registered, request, frozen_config, context, probe=False)

    def run_probe(
        self,
        plugin_id: str,
        implementation_version: str,
        request: ProbeRequest,
        config: PluginConfig,
        context: PluginContext,
    ) -> ProbeResult:
        context.check_cancelled()
        if type(request) is not ProbeRequest:
            raise PluginError("invalid_input", "download", plugin_id, "invalid probe request")
        if not isinstance(request.url, str) or not request.url.strip():
            raise PluginError("invalid_input", "download", plugin_id, "invalid probe request")
        if not isinstance(request.force_refresh, bool) or (
            request.cookies_from_browser is not None
            and not isinstance(request.cookies_from_browser, str)
        ):
            raise PluginError("invalid_input", "download", plugin_id, "invalid probe request")
        registered = self._resolve(
            plugin_id, implementation_version, "probe", expected_stage="download"
        )
        if context.purpose != "download":
            raise self._error("invalid_input", registered.manifest, "invalid probe context")
        frozen_config = self._freeze_config(config, registered.manifest)
        result = self._invoke(registered, request, frozen_config, context, probe=True)
        return result  # type: ignore[return-value]

    def _resolve(
        self,
        plugin_id: str,
        version: str,
        required_capability: str | None,
        *,
        expected_stage: str = "download",
    ) -> RegisteredPlugin:
        if not isinstance(plugin_id, str) or not plugin_id.strip() or not isinstance(version, str) or not version.strip():
            safe_plugin_id = plugin_id if isinstance(plugin_id, str) else "unknown"
            raise PluginError("invalid_input", expected_stage, safe_plugin_id, "plugin identity is invalid")
        try:
            registered = self._registry.resolve(plugin_id, version)
        except KeyError as exc:
            raise PluginError(
                "plugin_unavailable", expected_stage, plugin_id,
                "plugin is not registered", exc,
            ) from exc
        except ValueError as exc:
            manifest = self._registry.get_manifest(plugin_id)
            stage = manifest.stage if manifest is not None else "download"
            raise PluginError(
                "version_mismatch", stage, plugin_id,
                "plugin implementation version is unavailable", exc,
            ) from exc
        if required_capability and required_capability not in registered.manifest.capabilities:
            raise self._error(
                "plugin_unavailable", registered.manifest,
                "plugin does not provide the requested capability",
            )
        return registered

    def _invoke(
        self,
        registered: RegisteredPlugin,
        request: Request | ProbeRequest,
        config: PluginConfig,
        context: PluginContext,
        *,
        probe: bool,
    ) -> Result | ProbeResult:
        manifest = registered.manifest
        invocation_context = _InvocationContext(context)
        session: Any = None
        primary: BaseException | None = None
        cleanup_error: Exception | None = None
        result: Result | ProbeResult | None = None
        try:
            context.check_cancelled()
            session = registered.factory.create_session(config, invocation_context)
            context.check_cancelled()
            if not callable(getattr(session, "validate", None)) or not callable(
                getattr(session, "run", None)
            ):
                raise self._error("plugin_unavailable", manifest, "plugin session is invalid")
            session.validate(request)
            context.check_cancelled()
            result = session.run(request)
            context.check_cancelled()
            if probe:
                self._validate_probe_result(result, manifest)
            else:
                self._validate_result(request, result, context, manifest)
            context.check_cancelled()
        except PluginCancelled as exc:
            primary = exc
        except PluginError as exc:
            primary = exc
        except Exception as exc:
            primary = self._error(
                "execution_error", manifest,
                "plugin invocation failed", exc,
            )
        finally:
            invocation_context.finish()
            if session is not None:
                close = getattr(session, "close", None)
                if callable(close):
                    try:
                        close()
                    except PluginCancelled as exc:
                        cleanup_error = exc
                    except Exception as exc:  # preserve the primary failure
                        cleanup_error = exc
                else:
                    cleanup_error = RuntimeError("plugin session has no close method")

        # Cancellation observed during close has priority over every other outcome.
        if primary is not None and cleanup_error is not None:
            # Keep the original code/cause authoritative, while retaining
            # cleanup diagnostics for internal callers without serializing them.
            setattr(primary, "cleanup_error", cleanup_error)
        if isinstance(cleanup_error, PluginCancelled):
            raise cleanup_error
        try:
            context.check_cancelled()
        except PluginCancelled as exc:
            if cleanup_error is not None:
                setattr(exc, "cleanup_error", cleanup_error)
            raise exc
        if isinstance(primary, PluginCancelled):
            raise primary
        if primary is not None:
            raise primary
        if cleanup_error is not None:
            raise self._error(
                "cleanup_error", manifest, "plugin session cleanup failed", cleanup_error
            ) from cleanup_error
        if result is None:
            raise self._error("invalid_result", manifest, "plugin returned no result")
        return result

    def _validate_request_paths(
        self, request: Request, context: PluginContext, manifest: PluginManifest
    ) -> None:
        for field_name in _REQUEST_PATH_FIELDS[type(request)]:
            value = getattr(request, field_name)
            if value is None and field_name in {
                "vocal_path", "background_path", "background_audio", "vocal_audio"
            }:
                continue
            if not isinstance(value, Path) or not value.is_absolute():
                raise self._error("invalid_input", manifest, "request paths must be absolute paths")
            self._safe_task_path(value, context, manifest, "request path")
        if isinstance(request, DownloadRequest):
            if request.target_dir.resolve() != context.task_dir.resolve():
                raise self._error("invalid_input", manifest, "download target must be the task directory")
            if request.source_stem != "source":
                raise self._error("invalid_input", manifest, "download source stem is reserved")
        if isinstance(request, VocalSeparateRequest) and not (
            request.vocal_path or request.background_path
        ):
            raise self._error("invalid_input", manifest, "at least one stem must be requested")
        outputs = self._request_outputs(request)
        output_paths: set[Path] = set()
        for path, expected_name in outputs:
            if path.name != expected_name or path.parent.resolve() != context.task_dir.resolve():
                raise self._error(
                    "invalid_input", manifest, "request output path does not use its reserved task target"
                )
            normalized = path.resolve()
            if normalized in output_paths:
                raise self._error("invalid_input", manifest, "request output paths overlap")
            output_paths.add(normalized)
        input_paths = {
            getattr(request, field_name).resolve()
            for field_name in _REQUEST_INPUT_FIELDS[type(request)]
            if getattr(request, field_name) is not None
        }
        if output_paths & input_paths:
            raise self._error("invalid_input", manifest, "request input and output paths overlap")

    def _request_outputs(self, request: Request) -> tuple[tuple[Path, str], ...]:
        if isinstance(request, AudioExtractRequest):
            return ((request.output_path, "audio.wav" if request.purpose == "extract.asr" else "tts_source.wav"),)
        if isinstance(request, VocalSeparateRequest):
            outputs = []
            if request.vocal_path is not None:
                outputs.append((request.vocal_path, "vocal.wav" if request.purpose == "separate.asr" else "vocal_stem.wav"))
            if request.background_path is not None:
                outputs.append((request.background_path, "background.wav"))
            return tuple(outputs)
        if isinstance(request, TranscribeRequest):
            return ((request.output_srt, "original.srt"),)
        if isinstance(request, TranslateRequest):
            return ((request.output_srt, "translated.srt"),)
        if isinstance(request, TTSRequest):
            return ((request.output_audio, "dubbed.wav"),)
        if isinstance(request, SubtitleRenderRequest):
            name = request.output_video.name
            expected = name if re.fullmatch(r"\.reburn-[a-f0-9]{32}\.mp4", name) else f"output_{request.mode}.mp4"
            return ((request.output_video, expected),)
        if isinstance(request, DubMuxRequest):
            return ((request.mixed_audio, "dubbed_mix.wav"), (request.output_video, "output_dubbed.mp4"))
        return ()

    def _validate_request_fields(self, request: Request, manifest: PluginManifest) -> None:
        non_empty_fields = {
            DownloadRequest: ("url", "quality"),
            TranscribeRequest: ("source_lang", "backend", "model"),
            TranslateRequest: ("source_lang", "target_lang"),
            TTSRequest: ("target_lang", "voice"),
        }.get(type(request), ())
        if any(not isinstance(getattr(request, name), str) or not getattr(request, name).strip() for name in non_empty_fields):
            raise self._error("invalid_input", manifest, "request contains an empty required value")
        if isinstance(request, AudioExtractRequest):
            if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (request.sample_rate, request.channels)):
                raise self._error("invalid_input", manifest, "audio format values must be positive integers")
        elif isinstance(request, TranslateRequest):
            if not isinstance(request.mode, str) or request.mode not in {"mono", "bilingual"}:
                raise self._error("invalid_input", manifest, "translation mode is invalid")
        elif isinstance(request, TTSRequest):
            if not isinstance(request.bilingual, bool):
                raise self._error("invalid_input", manifest, "TTS bilingual flag must be boolean")
            self._validate_optional_number(request.duration_seconds, manifest, "invalid_input")
        elif isinstance(request, SubtitleRenderRequest):
            if not isinstance(request.mode, str) or request.mode not in {"hard", "soft"}:
                raise self._error("invalid_input", manifest, "subtitle render mode is invalid")
        elif isinstance(request, DubMuxRequest):
            if not isinstance(request.original_voice_mode, str) or request.original_voice_mode not in {"keep", "lower", "replace"}:
                raise self._error("invalid_input", manifest, "original voice mode is invalid")

    def _freeze_config(self, config: PluginConfig, manifest: PluginManifest) -> PluginConfig:
        if not isinstance(config, Mapping):
            raise self._error("invalid_input", manifest, "plugin config must be a mapping")
        copied = dict(config)
        for key, value in copied.items():
            if not isinstance(key, str) or not (
                value is None or isinstance(value, (str, bool, int))
                or isinstance(value, float) and math.isfinite(value)
            ):
                raise self._error("invalid_input", manifest, "plugin config contains an invalid value")
        return MappingProxyType(copied)

    def _validate_result(
        self,
        request: Request,
        result: object,
        context: PluginContext,
        manifest: PluginManifest,
    ) -> None:
        contract = _TASK_OPERATIONS[type(request)]
        expected_result = contract[2]
        if type(result) is not expected_result:
            raise self._error("invalid_result", manifest, "plugin returned the wrong result type")
        artifact_fields: tuple[str, ...]
        if isinstance(result, DownloadResult):
            artifact_fields = ("source_path",)
        elif isinstance(result, AudioExtractResult):
            artifact_fields = ("audio_path",)
        elif isinstance(result, VocalSeparateResult):
            artifact_fields = ("vocal_path", "background_path")
        elif isinstance(result, TranscribeResult):
            artifact_fields = ("original_srt",)
        elif isinstance(result, TranslateResult):
            artifact_fields = ("translated_srt",)
        elif isinstance(result, TTSResult):
            artifact_fields = ("speech_audio",)
        elif isinstance(result, SubtitleRenderResult):
            artifact_fields = ("video_path",)
        else:
            artifact_fields = ("mixed_audio", "video_path")
        for field_name in artifact_fields:
            path = getattr(result, field_name)
            if path is None and isinstance(result, VocalSeparateResult):
                continue
            if not isinstance(path, Path):
                raise self._error("invalid_result", manifest, "artifact paths must be pathlib paths")
        if isinstance(request, DownloadRequest) and isinstance(result, DownloadResult):
            if result.title is not None and not isinstance(result.title, str):
                raise self._error("invalid_result", manifest, "download title metadata is invalid")
            if (
                result.source_path.parent.resolve() != request.target_dir.resolve()
                or not result.source_path.name.startswith("source.")
                or not result.source_path.suffix
            ):
                raise self._error("invalid_result", manifest, "download result path does not match its target")
            artifacts = (result.source_path,)
            self._validate_optional_number(result.duration_seconds, manifest)
        elif isinstance(request, AudioExtractRequest) and isinstance(result, AudioExtractResult):
            if result.audio_path != request.output_path or result.sample_rate != request.sample_rate or result.channels != request.channels:
                raise self._error("invalid_result", manifest, "audio extraction result does not match its request")
            expected_name = "audio.wav" if request.purpose == "extract.asr" else "tts_source.wav"
            self._require_name(result.audio_path, expected_name, manifest)
            artifacts = (result.audio_path,)
            if (
                isinstance(result.sample_rate, bool)
                or not isinstance(result.sample_rate, int)
                or isinstance(result.channels, bool)
                or not isinstance(result.channels, int)
                or result.sample_rate <= 0
                or result.channels <= 0
            ):
                raise self._error("invalid_result", manifest, "audio metadata is invalid")
            self._validate_optional_number(result.duration_seconds, manifest)
        elif isinstance(request, VocalSeparateRequest) and isinstance(result, VocalSeparateResult):
            if request.vocal_path is not None and result.vocal_path != request.vocal_path:
                raise self._error("invalid_result", manifest, "vocal stem path does not match its request")
            if request.background_path is not None and result.background_path != request.background_path:
                raise self._error("invalid_result", manifest, "background stem path does not match its request")
            artifacts = tuple(path for path in (result.vocal_path, result.background_path) if path is not None)
            if request.vocal_path is not None:
                self._require_name(request.vocal_path, "vocal.wav" if request.purpose == "separate.asr" else "vocal_stem.wav", manifest)
            if request.background_path is not None:
                self._require_name(request.background_path, "background.wav", manifest)
            if request.vocal_path is not None and result.vocal_path is None:
                raise self._error("invalid_result", manifest, "requested vocal stem is missing")
            if request.background_path is not None and result.background_path is None:
                raise self._error("invalid_result", manifest, "requested background stem is missing")
        elif isinstance(request, TranscribeRequest) and isinstance(result, TranscribeResult):
            if result.original_srt != request.output_srt:
                raise self._error("invalid_result", manifest, "transcription result path does not match its request")
            self._require_name(result.original_srt, "original.srt", manifest)
            artifacts = (result.original_srt,)
            if result.segments is not None and (
                isinstance(result.segments, bool)
                or not isinstance(result.segments, int)
                or result.segments < 0
            ):
                raise self._error("invalid_result", manifest, "transcription segment count is invalid")
        elif isinstance(request, TranslateRequest) and isinstance(result, TranslateResult):
            if (
                result.translated_srt != request.output_srt
                or not isinstance(result.bilingual, bool)
                or result.bilingual != (request.mode == "bilingual")
            ):
                raise self._error("invalid_result", manifest, "translation result does not match its request")
            self._require_name(result.translated_srt, "translated.srt", manifest)
            artifacts = (result.translated_srt,)
            if isinstance(result.segments, bool) or not isinstance(result.segments, int) or result.segments < 0:
                raise self._error("invalid_result", manifest, "translation segment count is invalid")
        elif isinstance(request, TTSRequest) and isinstance(result, TTSResult):
            if result.speech_audio != request.output_audio:
                raise self._error("invalid_result", manifest, "TTS result path does not match its request")
            self._require_name(result.speech_audio, "dubbed.wav", manifest)
            artifacts = (result.speech_audio,)
            self._validate_optional_number(result.duration_seconds, manifest)
        elif isinstance(request, SubtitleRenderRequest) and isinstance(result, SubtitleRenderResult):
            if result.video_path != request.output_video:
                raise self._error("invalid_result", manifest, "render result path does not match its request")
            expected_name = self._request_outputs(request)[0][1]
            self._require_name(result.video_path, expected_name, manifest)
            artifacts = (result.video_path,)
        elif isinstance(request, DubMuxRequest) and isinstance(result, DubMuxResult):
            if result.video_path != request.output_video or result.mixed_audio != request.mixed_audio:
                raise self._error("invalid_result", manifest, "dub mux result paths do not match their request")
            self._require_name(result.video_path, "output_dubbed.mp4", manifest)
            self._require_name(result.mixed_audio, "dubbed_mix.wav", manifest)
            artifacts = (result.mixed_audio, result.video_path)
        else:  # exact result types above make this a defensive guard.
            raise self._error("invalid_result", manifest, "plugin result is invalid")

        for path in artifacts:
            self._validate_artifact(path, context, manifest)

    def _validate_probe_result(self, result: object, manifest: PluginManifest) -> None:
        if type(result) is not ProbeResult:
            raise self._error("invalid_result", manifest, "probe returned the wrong result type")
        if not isinstance(result.ok, bool) or not isinstance(result.cached, bool):
            raise self._error("invalid_result", manifest, "probe flags are invalid")
        for name in (
            "title", "extractor", "webpage_url", "reason", "detail", "language",
            "thumbnail", "uploader",
        ):
            value = getattr(result, name)
            if value is not None and not isinstance(value, str):
                raise self._error("invalid_result", manifest, "probe metadata is invalid")
        if (
            isinstance(result.formats_count, bool)
            or not isinstance(result.formats_count, int)
            or result.formats_count < 0
        ):
            raise self._error("invalid_result", manifest, "probe format count is invalid")
        self._validate_optional_number(result.duration, manifest)
        if not isinstance(result.available_qualities, tuple) or any(
            not isinstance(quality, str) for quality in result.available_qualities
        ):
            raise self._error("invalid_result", manifest, "probe quality metadata is invalid")
        if not isinstance(result.formats, tuple):
            raise self._error("invalid_result", manifest, "probe formats are invalid")
        for item in result.formats:
            if type(item) is not ProbeFormat:
                raise self._error("invalid_result", manifest, "probe format entry is invalid")
            if any(
                not isinstance(getattr(item, name), str)
                for name in ("formatId", "ext", "resolution")
            ):
                raise self._error("invalid_result", manifest, "probe format metadata is invalid")
            if item.type not in {"video", "audio"}:
                raise self._error("invalid_result", manifest, "probe format type is invalid")
            for name in ("vcodec", "acodec", "note"):
                value = getattr(item, name)
                if value is not None and not isinstance(value, str):
                    raise self._error("invalid_result", manifest, "probe format metadata is invalid")
            for name in ("height", "width", "filesize"):
                value = getattr(item, name)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                ):
                    raise self._error("invalid_result", manifest, "probe format dimensions are invalid")
            self._validate_optional_number(item.fps, manifest)
            self._validate_optional_number(item.tbr, manifest)

    def _validate_artifact(
        self, path: Path, context: PluginContext, manifest: PluginManifest
    ) -> None:
        resolved = self._safe_task_path(path, context, manifest, "artifact path", strict=True)
        if resolved.parent != context.task_dir.resolve(strict=True):
            raise self._error("invalid_result", manifest, "artifact must be in the task directory root")
        if path.is_symlink() or not resolved.is_file() or resolved.stat().st_size <= 0:
            raise self._error("invalid_result", manifest, "required artifact is not a non-empty regular file")
        if path.suffix.lower() == ".srt":
            try:
                content = resolved.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                raise self._error("invalid_result", manifest, "subtitle artifact has invalid encoding", exc) from exc
            if not re.search(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}", content):
                raise self._error("invalid_result", manifest, "subtitle artifact has no valid cue timing")
        elif path.suffix.lower() == ".wav":
            try:
                with resolved.open("rb") as artifact:
                    header = artifact.read(12)
            except OSError as exc:
                raise self._error("invalid_result", manifest, "audio artifact cannot be read", exc) from exc
            if len(header) < 12 or header[:4] not in {b"RIFF", b"RF64"} or header[8:12] != b"WAVE":
                raise self._error("invalid_result", manifest, "audio artifact is not a WAV file")
        elif path.suffix.lower() == ".mp4":
            try:
                with resolved.open("rb") as artifact:
                    header = artifact.read(8)
            except OSError as exc:
                raise self._error("invalid_result", manifest, "video artifact cannot be read", exc) from exc
            if len(header) < 8 or header[4:8] != b"ftyp":
                raise self._error("invalid_result", manifest, "video artifact is not an MP4 file")

    def _safe_task_path(
        self,
        path: Path,
        context: PluginContext,
        manifest: PluginManifest,
        label: str,
        *,
        strict: bool = False,
    ) -> Path:
        try:
            root = context.task_dir.resolve(strict=True)
            resolved = path.resolve(strict=strict)
            resolved.relative_to(root)
        except (OSError, ValueError, RuntimeError) as exc:
            raise self._error("invalid_input" if label == "request path" else "invalid_result", manifest, f"{label} escapes the task directory", exc) from exc
        if path.name in _ARTIFACT_NAMES and path.name != resolved.name:
            raise self._error("invalid_result", manifest, "reserved artifact path resolves through a symlink")
        return resolved

    def _require_name(self, path: Path, name: str, manifest: PluginManifest) -> None:
        if path.name != name:
            raise self._error("invalid_result", manifest, "artifact does not use its reserved target name")

    def _validate_optional_number(
        self, value: float | None, manifest: PluginManifest, code: str = "invalid_result"
    ) -> None:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0
        ):
            raise self._error(code, manifest, "numeric metadata is invalid")

    def _error(
        self,
        code: str,
        manifest: PluginManifest,
        message: str,
        cause: Exception | None = None,
    ) -> PluginError:
        return PluginError(code, manifest.stage, manifest.plugin_id, message, cause)
