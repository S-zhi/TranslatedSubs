"""Built-in Kokoro timeline speech synthesis plugin."""

from __future__ import annotations

import math
import wave
from pathlib import Path

from src.core import tts as tts_core
from src.plugin_system import (
    PluginCancelled,
    PluginContext,
    PluginError,
    PluginManifest,
    TTSRequest,
    TTSResult,
)
from src.plugin_system.contracts import PluginConfig


PLUGIN_ID = "builtin.kokoro"
_REQUIRED_CONFIG_KEYS = frozenset({"voice", "target_lang", "bilingual"})
_ALLOWED_CONFIG_KEYS = _REQUIRED_CONFIG_KEYS | {"speed", "model_id", "output_format"}


class KokoroFactory:
    """Lightweight factory; model dependencies load only when a session runs."""

    stage = "tts"
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        stage="tts",
        implementation_version="1",
        operations=frozenset({"tts"}),
    )

    def create_session(self, config: PluginConfig, context: PluginContext):
        return KokoroSession(config, context)


class KokoroSession:
    def __init__(self, config: PluginConfig, context: PluginContext) -> None:
        self._config = dict(config)
        self._context = context
        self._closed = False

    def validate(self, request: TTSRequest) -> None:
        if self._closed:
            raise self._error("invalid_input", "TTS session is closed")
        if type(request) is not TTSRequest:
            raise self._error("invalid_input", "expected a TTSRequest")
        if (
            request.stage != "tts"
            or request.operation != "tts"
            or request.purpose != "tts"
            or self._context.purpose != request.purpose
        ):
            raise self._error("invalid_input", "TTS invocation identity is invalid")
        if request.output_audio != self._context.task_dir / "dubbed.wav":
            raise self._error("invalid_input", "TTS output path is invalid")
        if not request.translated_srt.is_file() or not request.original_srt.is_file():
            raise self._error("invalid_input", "subtitle input is missing")
        unknown = set(self._config) - _ALLOWED_CONFIG_KEYS
        if unknown or not _REQUIRED_CONFIG_KEYS.issubset(self._config):
            raise self._error("invalid_input", "TTS config is incomplete or unsupported")
        voice = self._config["voice"]
        target_lang = self._config["target_lang"]
        bilingual = self._config["bilingual"]
        if not isinstance(voice, str) or not voice.strip():
            raise self._error("invalid_input", "TTS voice config is invalid")
        if not isinstance(target_lang, str) or not target_lang.strip():
            raise self._error("invalid_input", "TTS target language config is invalid")
        if not isinstance(bilingual, bool):
            raise self._error("invalid_input", "TTS bilingual config must be boolean")
        speed = self._config.get("speed", 1.0)
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or not 0 < speed <= 4:
            raise self._error("invalid_input", "TTS speed config is invalid")
        model_id = self._config.get("model_id")
        if model_id is not None and not isinstance(model_id, str):
            raise self._error("invalid_input", "TTS model config is invalid")
        if self._config.get("output_format", "wav") != "wav":
            raise self._error("invalid_input", "TTS output format is unsupported")
        if (
            request.voice != voice
            or request.target_lang != target_lang
            or request.bilingual != bilingual
        ):
            raise self._error("invalid_input", "TTS request differs from its frozen config")
        duration = request.duration_seconds
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration < 0
        ):
            raise self._error("invalid_input", "TTS duration is invalid")

    def run(self, request: TTSRequest) -> TTSResult:
        self.validate(request)
        self._context.check_cancelled()
        output_existed = request.output_audio.exists()
        voice = self._resolve_voice(request.target_lang, request.voice)
        try:
            result_path = tts_core.generate_timeline_audio(
                request.translated_srt,
                request.original_srt,
                request.output_audio,
                target_lang=request.target_lang,
                voice=voice,
                speed=self._config.get("speed", 1.0),
                model_id=self._config.get("model_id"),
                output_format=self._config.get("output_format", "wav"),
                bilingual=request.bilingual,
                duration=request.duration_seconds,
                cancel_check=self._context.check_cancelled,
                on_progress=self._context.report_progress,
                task_id=self._context.task_id,
            )
            self._context.check_cancelled()
        except PluginCancelled:
            self._discard_new_output(request.output_audio, output_existed)
            raise
        except Exception as exc:
            self._discard_new_output(request.output_audio, output_existed)
            try:
                self._context.check_cancelled()
            except PluginCancelled:
                raise
            if self._contains_cancelled(exc):
                raise PluginCancelled("plugin invocation cancelled") from exc
            if isinstance(exc, PluginError):
                raise
            code = (
                "dependency_missing"
                if self._contains_exception(exc, ImportError)
                else "execution_error"
            )
            raise self._error(code, "speech audio generation failed", exc) from exc

        if Path(result_path) != request.output_audio:
            self._discard_new_output(request.output_audio, output_existed)
            raise self._error("invalid_result", "TTS returned an unexpected audio path")
        try:
            with wave.open(str(request.output_audio), "rb") as audio_file:
                sample_rate = audio_file.getframerate()
                channels = audio_file.getnchannels()
                sample_width = audio_file.getsampwidth()
                frame_count = audio_file.getnframes()
        except (OSError, EOFError, wave.Error) as exc:
            self._discard_new_output(request.output_audio, output_existed)
            raise self._error("invalid_result", "TTS did not produce a valid WAV file", exc) from exc
        if (
            sample_rate != tts_core.SAMPLE_RATE
            or channels != 1
            or sample_width != 2
            or frame_count <= 0
            or request.output_audio.stat().st_size <= 0
        ):
            self._discard_new_output(request.output_audio, output_existed)
            raise self._error("invalid_result", "TTS WAV metadata is invalid")
        return TTSResult(
            speech_audio=request.output_audio,
            duration_seconds=frame_count / sample_rate,
        )

    def close(self) -> None:
        self._closed = True

    def _resolve_voice(self, target_lang: str, voice: str) -> str:
        if voice != "auto":
            return voice
        return "zf_001" if target_lang.lower().startswith("zh") else "af_maple"

    def _contains_cancelled(self, error: BaseException) -> bool:
        current: BaseException | None = error
        visited: set[int] = set()
        try:
            from src.service.orchestrator import PipelineCancelledError
        except ImportError:
            PipelineCancelledError = ()  # type: ignore[assignment,misc]
        while current is not None and id(current) not in visited:
            if isinstance(current, PluginCancelled) or (
                PipelineCancelledError and isinstance(current, PipelineCancelledError)
            ):
                return True
            visited.add(id(current))
            current = current.__cause__ or current.__context__
        return False

    def _contains_exception(
        self, error: BaseException, exception_type: type[BaseException]
    ) -> bool:
        current: BaseException | None = error
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            if isinstance(current, exception_type):
                return True
            visited.add(id(current))
            current = current.__cause__ or current.__context__
        return False

    def _discard_new_output(self, output_path: Path, output_existed: bool) -> None:
        if not output_existed:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _error(
        self, code: str, message: str, cause: Exception | None = None
    ) -> PluginError:
        return PluginError(code, "tts", PLUGIN_ID, message, cause)


factory = KokoroFactory()
