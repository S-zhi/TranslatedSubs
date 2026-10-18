"""FFmpeg-backed render operations for the v1 pipeline plugin contract."""

from __future__ import annotations

import re
from pathlib import Path

from src.config import output_video_filename, settings
from src.core import ffmpeg_utils, subtitle_burner
from src.plugin_system import (
    DubMuxRequest,
    DubMuxResult,
    PluginCancelled,
    PluginContext,
    PluginError,
    PluginManifest,
    SubtitleRenderRequest,
    SubtitleRenderResult,
)
from src.plugin_system.contracts import PluginConfig
from src.plugin_system.errors import sanitize_message
from src.plugins.builtin.configuration import BuiltinFactory


PLUGIN_ID = "builtin.ffmpeg_render"


class FFmpegRenderFactory(BuiltinFactory):
    stage = "render"
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        stage="render",
        implementation_version="1",
        operations=frozenset({"subtitle", "dub_mux"}),
    )

    def create_session(self, config: PluginConfig, context: PluginContext):
        return FFmpegRenderSession(config, context)


class FFmpegRenderSession:
    def __init__(self, config: PluginConfig, context: PluginContext) -> None:
        self._config = dict(config)
        self._context = context
        self._closed = False

    def validate(self, request) -> None:
        if self._closed:
            raise self._error("invalid_input", "render session is closed")
        valid_subtitle = isinstance(request, SubtitleRenderRequest)
        valid_dub = isinstance(request, DubMuxRequest)
        if not valid_subtitle and not valid_dub:
            raise self._error("invalid_input", "expected a render request")
        expected_op = "subtitle" if valid_subtitle else "dub_mux"
        if (
            request.stage != "render"
            or request.operation != expected_op
            or request.purpose != f"render.{expected_op}"
            or self._context.purpose != request.purpose
        ):
            raise self._error("invalid_input", "render request identity does not match invocation")
        expected_config = (
            {"mode": request.mode}
            if valid_subtitle
            else {"original_voice_mode": request.original_voice_mode}
        )
        if self._config != expected_config:
            raise self._error(
                "invalid_input", "render request differs from its frozen configuration"
            )
        if valid_subtitle:
            expected = output_video_filename(request.mode)
            paths = (request.source_video, request.translated_srt)
            output = request.output_video
            if (
                request.mode not in {"hard", "soft"}
                or not output.is_absolute()
                or output.parent.resolve() != self._context.task_dir.resolve()
                or output.is_symlink()
                or output.resolve().parent != self._context.task_dir.resolve()
                or (
                    output.name != expected
                    and re.fullmatch(r"\.reburn-[0-9a-f]{32}\.mp4", output.name) is None
                )
            ):
                raise self._error("invalid_input", "subtitle output path is invalid")
            for source in paths:
                if output.resolve() == source.resolve() or (
                    output.exists() and source.exists() and output.samefile(source)
                ):
                    raise self._error("invalid_input", "subtitle output overlaps an input")
        else:
            expected = self._context.task_dir / "output_dubbed.mp4"
            paths = (request.base_video, request.source_audio, request.speech_audio)
            if request.output_video.resolve() != expected.resolve():
                raise self._error("invalid_input", "dubbed video output path is invalid")
            if request.mixed_audio.resolve() != (self._context.task_dir / "dubbed_mix.wav").resolve():
                raise self._error("invalid_input", "mixed audio output path is invalid")
            mode = request.original_voice_mode
            if mode not in {"keep", "lower", "replace"}:
                raise self._error("invalid_input", "original voice mode is invalid")
            if mode in {"lower", "replace"} and request.background_audio is None:
                raise self._error("invalid_input", "background audio is required for this voice mode")
            if mode == "lower" and request.vocal_audio is None:
                raise self._error("invalid_input", "vocal audio is required for lower mode")
            if request.background_audio is not None:
                paths += (request.background_audio,)
            if request.vocal_audio is not None:
                paths += (request.vocal_audio,)
        if any(not Path(path).is_file() for path in paths):
            raise self._error("invalid_input", "render input is missing")

    def run(self, request):
        self.validate(request)
        self._check_cancelled()
        try:
            if isinstance(request, SubtitleRenderRequest):
                result = subtitle_burner.burn_subtitles(
                    request.source_video,
                    request.translated_srt,
                    self._context.task_id,
                    on_progress=lambda progress: self._report(progress.percent),
                    mode=request.mode,
                    output_path=request.output_video,
                )
                self._check_cancelled()
                output = Path(result.output_path).resolve()
                if output != request.output_video.resolve() or not self._nonempty(output):
                    raise self._error("invalid_result", "subtitle renderer produced an invalid output")
                return SubtitleRenderResult(video_path=output)
            mixed = self._mix_audio(request)
            self._mux_video(request, mixed)
            self._check_cancelled()
            if not self._nonempty(mixed) or not self._nonempty(request.output_video):
                raise self._error("invalid_result", "FFmpeg produced an invalid dubbed output")
            return DubMuxResult(mixed_audio=mixed, video_path=request.output_video.resolve())
        except (PluginCancelled, PluginError):
            raise
        except subtitle_burner.BurnError as exc:
            self._check_cancelled()
            code, message = self._failure_metadata(exc)
            raise self._error(code, message, exc) from exc
        except Exception as exc:
            self._check_cancelled()
            code, message = self._failure_metadata(exc)
            raise self._error(code, message, exc) from exc

    def close(self) -> None:
        self._closed = True

    def _mix_audio(self, request: DubMuxRequest) -> Path:
        inputs: list[str] = []
        filters: list[str] = []
        if request.original_voice_mode == "keep":
            inputs.extend(["-i", str(request.source_audio)])
            filters.append("[0:a]volume=1.0[base]")
            speech_index = 1
        else:
            assert request.background_audio is not None
            inputs.extend(["-i", str(request.background_audio)])
            filters.append("[0:a]volume=1.0[base]")
            speech_index = 1
            if request.original_voice_mode == "lower":
                assert request.vocal_audio is not None
                inputs.extend(["-i", str(request.vocal_audio)])
                filters.append("[1:a]volume=0.25[vocal]")
                speech_index = 2
        inputs.extend(["-i", str(request.speech_audio)])
        filters.append(f"[{speech_index}:a]volume=1.0[tts]")
        labels = "[base]" + ("[vocal]" if request.original_voice_mode == "lower" else "") + "[tts]"
        count = 3 if request.original_voice_mode == "lower" else 2
        filters.append(f"{labels}amix=inputs={count}:duration=longest:normalize=0[mix]")
        cmd = [
            settings.ffmpeg_bin, "-y", *inputs, "-filter_complex", ";".join(filters),
            "-map", "[mix]", "-ar", "48000", "-progress", "pipe:1", "-nostats",
            "-loglevel", "error", str(request.mixed_audio),
        ]
        self._run_ffmpeg(cmd, 0.0, 50.0)
        return request.mixed_audio.resolve()

    def _mux_video(self, request: DubMuxRequest, mixed: Path) -> None:
        cmd = [
            settings.ffmpeg_bin, "-y", "-i", str(request.base_video), "-i", str(mixed),
            "-map", "0:v:0", "-map", "1:a:0", "-map", "0:s?", "-c:v", "copy",
            "-c:a", "aac", "-c:s", "copy", "-progress", "pipe:1", "-nostats",
            "-loglevel", "error", str(request.output_video),
        ]
        self._run_ffmpeg(cmd, 50.0, 100.0)

    def _run_ffmpeg(self, cmd: list[str], start: float, end: float) -> None:
        self._check_cancelled()

        def tick(seconds: float) -> None:
            self._check_cancelled()
            self._report(start + min(1.0, max(0.0, seconds / 1.0)) * (end - start))

        ffmpeg_utils.run_ffmpeg(cmd, tick, task_id=self._context.task_id)
        self._check_cancelled()

    def _report(self, percent: float | None) -> None:
        self._check_cancelled()
        self._context.report_progress(percent)

    def _check_cancelled(self) -> None:
        self._context.check_cancelled()

    @staticmethod
    def _nonempty(path: Path) -> bool:
        return path.is_file() and path.stat().st_size > 0

    @staticmethod
    def _failure_metadata(error: Exception) -> tuple[str, str]:
        core_code = getattr(error, "code", None)
        if core_code:
            return core_code, sanitize_message(error)
        current: BaseException | None = error
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            if isinstance(current, (FileNotFoundError, ImportError)):
                return (
                    "dependency_missing",
                    "FFmpeg dependency is unavailable; install FFmpeg and verify its configured path",
                )
            visited.add(id(current))
            current = current.__cause__ or current.__context__
        if (
            isinstance(error, subtitle_burner.BurnError)
            and "subtitles 滤镜不可用" in str(error)
        ):
            return (
                "dependency_missing",
                "Hard subtitles require FFmpeg with libass support; install an FFmpeg build with the subtitles filter",
            )
        return "execution_error", sanitize_message(error)

    @staticmethod
    def _error(code: str, message: str, cause: Exception | None = None) -> PluginError:
        return PluginError(code, "render", PLUGIN_ID, message, cause)


factory = FFmpegRenderFactory()
