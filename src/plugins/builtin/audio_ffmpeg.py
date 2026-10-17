"""Built-in FFmpeg audio extraction plugin."""

from __future__ import annotations

from pathlib import Path

from src.core import audio_extractor
from src.plugin_system import (
    AudioExtractRequest,
    AudioExtractResult,
    PluginCancelled,
    PluginContext,
    PluginError,
    PluginManifest,
)
from src.plugin_system.contracts import PluginConfig
from src.plugins.builtin.configuration import BuiltinFactory


PLUGIN_ID = "builtin.ffmpeg_audio"
_ALLOWED_CONFIG_KEYS = frozenset({"sample_rate", "channels"})
_OUTPUTS_BY_PURPOSE = {
    "extract.asr": "audio.wav",
    "extract.dubbing": "tts_source.wav",
}


class AudioFFmpegFactory(BuiltinFactory):
    """Lightweight factory that creates one stateless extraction session per call."""

    stage = "audio_extract"
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        stage="audio_extract",
        implementation_version="1",
        operations=frozenset({"audio_extract"}),
    )

    def create_session(self, config: PluginConfig, context: PluginContext):
        return AudioFFmpegSession(config, context)


class AudioFFmpegSession:
    def __init__(self, config: PluginConfig, context: PluginContext) -> None:
        self._config = dict(config)
        self._context = context
        self._closed = False

    def validate(self, request: AudioExtractRequest) -> None:
        if self._closed:
            raise self._error("invalid_input", "audio extraction session is closed")
        if type(request) is not AudioExtractRequest:
            raise self._error("invalid_input", "expected an AudioExtractRequest")
        if (
            request.stage != "audio_extract"
            or request.operation != "audio_extract"
            or not isinstance(request.purpose, str)
            or request.purpose not in _OUTPUTS_BY_PURPOSE
            or self._context.purpose != request.purpose
        ):
            raise self._error("invalid_input", "audio extraction identity is invalid")
        try:
            task_root = self._context.task_dir.resolve(strict=True)
            source_path = request.source_path.resolve(strict=True)
            source_path.relative_to(task_root)
            if not request.output_path.is_absolute() or request.output_path.is_symlink():
                raise ValueError("output path is not a regular task path")
            output_path = request.output_path.resolve()
            output_path.relative_to(task_root)
        except (OSError, ValueError, RuntimeError) as exc:
            raise self._error("invalid_input", "audio extraction path is invalid", exc) from exc
        if output_path.parent != task_root or source_path == output_path:
            raise self._error("invalid_input", "audio extraction paths are invalid")
        if not request.source_path.is_file():
            raise self._error("invalid_input", "source video is missing")
        expected_output = self._context.task_dir / _OUTPUTS_BY_PURPOSE[request.purpose]
        if request.output_path != expected_output:
            raise self._error("invalid_input", "audio extraction output path is invalid")
        unknown = set(self._config) - _ALLOWED_CONFIG_KEYS
        if unknown or set(self._config) != _ALLOWED_CONFIG_KEYS:
            raise self._error("invalid_input", "audio extraction config is incomplete or unsupported")
        for key in ("sample_rate", "channels"):
            value = self._config[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise self._error("invalid_input", "audio extraction config is invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (request.sample_rate, request.channels)
        ):
            raise self._error("invalid_input", "audio request format is invalid")
        if (
            request.sample_rate != self._config["sample_rate"]
            or request.channels != self._config["channels"]
        ):
            raise self._error("invalid_input", "audio request differs from its frozen config")

    def run(self, request: AudioExtractRequest) -> AudioExtractResult:
        self.validate(request)
        self._context.check_cancelled()
        output_existed = request.output_path.exists()

        def report_progress(progress: audio_extractor.AudioProgress) -> None:
            self._context.check_cancelled()
            self._context.report_progress(progress.percent)

        try:
            result = audio_extractor.extract_audio(
                request.source_path,
                self._context.task_id,
                on_progress=report_progress,
                sample_rate=request.sample_rate,
                channels=request.channels,
                output_path=request.output_path,
            )
            self._context.check_cancelled()
        except PluginCancelled:
            self._discard_new_output(request.output_path, output_existed)
            raise
        except Exception as exc:
            self._discard_new_output(request.output_path, output_existed)
            try:
                self._context.check_cancelled()
            except PluginCancelled:
                raise
            raise self._error("execution_error", "audio extraction failed", exc) from exc

        if (
            result.audio_path != request.output_path
            or result.sample_rate != request.sample_rate
            or result.channels != request.channels
        ):
            self._discard_new_output(request.output_path, output_existed)
            raise self._error("invalid_result", "audio extraction returned unexpected metadata")
        return AudioExtractResult(
            audio_path=result.audio_path,
            sample_rate=result.sample_rate,
            channels=result.channels,
            duration_seconds=result.duration,
        )

    def close(self) -> None:
        self._closed = True

    def _discard_new_output(self, output_path: Path, output_existed: bool) -> None:
        if not output_existed:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _error(
        self, code: str, message: str, cause: Exception | None = None
    ) -> PluginError:
        return PluginError(code, "audio_extract", PLUGIN_ID, message, cause)


factory = AudioFFmpegFactory()
