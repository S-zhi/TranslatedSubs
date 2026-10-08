"""流水线编排：把 ①~⑤ 串成一条任务，逐步上报进度。

设计为纯逻辑：不碰数据库 / Redis，只通过 on_event 回调把状态与进度往外抛。
Worker 层把 on_event 接到「写 SQLite + 发 SSE」即可。

各步的内部百分比按权重映射到整体 0-100：
  下载 0-20 · 提取 20-35 · 识别 35-65 · 翻译 65-85 · 烧录 85-100
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from src.config import settings, task_dir
from src.core.audio_extractor import extract_audio
from src.core.downloader import download_video
from src.core.subtitle_burner import burn_subtitles
from src.core.transcriber import TranscribeCancelledError, transcribe
from src.core.translator import translate_srt
from src.core.tts import TTSError, generate_timeline_audio
from src.core.ffmpeg_utils import probe_duration, run_ffmpeg
from src.core.vocal_separator import (
    VocalSeparationCancelledError,
    VocalSeparationError,
    cached_vocals_are_current,
    separate_vocals,
)
from src.service.asset_resolver import AssetResolver, ResourceError, ResourceState

logger = logging.getLogger(__name__)

_cancel_events: dict[str, threading.Event] = {}
_cancel_events_lock = threading.Lock()


def register_cancellation_signal(task_id: str, is_cancelled: bool = False) -> threading.Event:
    with _cancel_events_lock:
        ev = _cancel_events.setdefault(task_id, threading.Event())
        if is_cancelled:
            ev.set()
        else:
            ev.clear()
        return ev


def unregister_cancellation_signal(task_id: str) -> None:
    with _cancel_events_lock:
        _cancel_events.pop(task_id, None)


def set_cancelled_signal(task_id: str) -> None:
    with _cancel_events_lock:
        ev = _cancel_events.setdefault(task_id, threading.Event())
        ev.set()


def is_cancelled_signal(task_id: str) -> bool:
    with _cancel_events_lock:
        ev = _cancel_events.get(task_id)
        return ev.is_set() if ev is not None else False


class PipelineError(RuntimeError):
    """流水线编排阶段的错误（如上传源缺失）。"""


class PipelineCancelledError(RuntimeError):
    """流水线已被用户取消。"""


@dataclass
class PipelineParams:
    task_id: str
    url: str
    source_lang: str
    target_lang: str
    mode: str = "mono"     # mono | bilingual
    burn: str = "hard"     # hard | soft
    model: str = "local:tiny"
    engine: str = "deepseek"
    source_type: str = "url"    # url=在线链接下载 upload=本地上传视频
    need_subtitle: bool = True  # False = 仅下载视频，跳过识别/翻译/烧录
    title: Optional[str] = None  # 上传模式下用原始文件名作为展示标题
    quality: Optional[str] = None  # 下载清晰度策略：best/1080p/720p/480p/360p/audio_only
    tts_enabled: bool = False
    tts_voice: str = "auto"
    original_voice_mode: str = "keep"


@dataclass
class PipelineEvent:
    status: str
    progress: int
    current_step: Optional[str]
    title: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    outputs: Optional[dict] = None
    tts_status: Optional[str] = None
    tts_error: Optional[str] = None


EventHook = Callable[[PipelineEvent], None]

# (status, 整体进度下界, 上界)
_BANDS = {
    "DOWNLOADING": (0, 20),
    "EXTRACTING": (20, 35),
    "TRANSCRIBING": (35, 65),
    "TRANSLATING": (65, 85),
    "BURNING": (85, 100),
    "SYNTHESIZING": (94, 97),
    "DUBBING": (97, 100),
}

_EXCEPTION_CODE_MAP: tuple[tuple[type[Exception], str], ...] = (
    (KeyError, "invalid_input"),
    (ValueError, "invalid_input"),
    (AttributeError, "internal_error"),
    (TypeError, "internal_error"),
    (OSError, "io_error"),
)


def _error_code_for_exception(exc: Exception) -> str:
    """返回异常的稳定错误码，自定义非空 code 优先于类型映射。"""
    custom_code = getattr(exc, "code", None)
    if custom_code:
        return custom_code
    for exception_type, code in _EXCEPTION_CODE_MAP:
        if isinstance(exc, exception_type):
            return code
    return "internal_error"


def _scale(lo: int, hi: int, pct: Optional[float]) -> int:
    if pct is None:
        return lo
    return int(lo + max(0.0, min(100.0, pct)) / 100.0 * (hi - lo))


def _check_cancelled(task_id: str) -> None:
    if is_cancelled_signal(task_id):
        raise PipelineCancelledError("任务已被用户取消")


@dataclass
class PipelineResources:
    """责任链阶段之间传递的任务产物。

    当前资源由本地路径表示；将来可以把字段替换为带状态、元数据或远程
    引用的资源对象，而不必继续扩张 ``PipelineContext``。
    """

    video_path: Optional[Path] = None
    audio_path: Optional[Path] = None
    vocal_audio_path: Optional[Path] = None
    original_srt_path: Optional[Path] = None
    translated_srt_path: Optional[Path] = None
    output_video_path: Optional[Path] = None


@dataclass
class PipelineContext:
    """责任链各节点共享的任务参数、资源、进度与执行结果。"""

    params: PipelineParams
    on_event: EventHook
    api_key: Optional[str] = None
    engine_config: object = None
    resources: PipelineResources = field(default_factory=PipelineResources)
    progress: int = 0
    current_step: Optional[str] = None
    title: Optional[str] = None
    terminal_event: Optional[PipelineEvent] = None
    # 配置在任务启动时快照，避免用户在任务运行中切换开关导致音频格式
    # 与后续分离阶段不一致。直接构造 Context 的旧调用方可留 None，回退
    # 到动态 settings（主要用于单元测试和自定义责任链）。
    vocal_separation_enabled: Optional[bool] = None

    @property
    def task_id(self) -> str:
        return self.params.task_id

    def emit(self, status: str, progress: int, **extra) -> None:
        _check_cancelled(self.task_id)
        self.progress = max(self.progress, progress)
        self.current_step = status if status in _BANDS else None
        self.on_event(PipelineEvent(
            status=status,
            progress=self.progress,
            current_step=self.current_step,
            **extra,
        ))

    def step_callback(self, status: str):
        if status == "DOWNLOADING" and not self.params.need_subtitle:
            lo, hi = (0, 100)
        else:
            lo, hi = _BANDS[status]

        def callback(progress) -> None:
            self.emit(status, _scale(lo, hi, getattr(progress, "percent", None)))

        return callback

    def emit_smooth(self, status: str, target: int) -> None:
        start = self.progress
        if target > start:
            step_size = max(1, (target - start) // 4)
            current = start + step_size
            while current < target:
                self.emit(status, current)
                current += step_size
        self.emit(status, target)

    def artifact_available(self, resolver) -> bool:
        resource_state, path, _ = resolver(self.task_id)
        return resource_state == ResourceState.AVAILABLE and path is not None

    def complete(self, *, outputs: dict, title: Optional[str] = None) -> PipelineEvent:
        event = PipelineEvent("SUCCESS", 100, None, title=title, outputs=outputs)
        self.terminal_event = event
        self.on_event(event)
        return event


class PipelineHandler:
    """责任链节点：处理当前阶段，然后将共享上下文交给下一节点。"""

    def __init__(self) -> None:
        self._next_handler: Optional[PipelineHandler] = None

    def set_next(self, handler: "PipelineHandler") -> "PipelineHandler":
        self._next_handler = handler
        return handler

    def handle(self, context: PipelineContext) -> PipelineEvent:
        self.process(context)
        if context.terminal_event is not None:
            return context.terminal_event
        if self._next_handler is None:
            raise PipelineError("责任链未产生任务完成结果")
        return self._next_handler.handle(context)

    def process(self, context: PipelineContext) -> None:
        raise NotImplementedError


class DownloadHandler(PipelineHandler):
    """准备源视频；上传、断点续跑和仅下载分支均在本节点处理。"""

    def process(self, context: PipelineContext) -> None:
        params = context.params
        tid = context.task_id
        context.emit("DOWNLOADING", 0)
        target_progress = 100 if not params.need_subtitle else 20

        if params.source_type == "upload":
            context.resources.video_path = _locate_uploaded_source(tid)
            context.title = params.title or context.resources.video_path.stem
            context.emit_smooth("DOWNLOADING", target_progress)
        elif context.artifact_available(AssetResolver.resolve_source):
            context.resources.video_path = AssetResolver.require_source(tid)
            context.title = params.title
            context.emit_smooth("DOWNLOADING", target_progress)
        else:
            download = download_video(
                params.url,
                tid,
                on_progress=context.step_callback("DOWNLOADING"),
                quality=params.quality,
            )
            context.resources.video_path = AssetResolver.require_source(tid)
            context.title = download.title
            if not params.need_subtitle:
                context.emit("DOWNLOADING", 100)

        if not params.need_subtitle:
            context.complete(
                outputs={"video": str(context.resources.video_path)},
                title=context.title,
            )
            logger.info("仅获取视频完成: task=%s source=%s", tid, params.source_type)


class AudioExtractionHandler(PipelineHandler):
    """获取可识别音频，优先复用已完成的音频产物。"""

    def process(self, context: PipelineContext) -> None:
        tid = context.task_id
        context.emit("EXTRACTING", 20)
        context.resources.video_path = AssetResolver.require_source(tid)
        # 人声分离要求 48 kHz 立体声。老任务留下的 16 kHz 单声道 audio.wav
        # 虽然物理文件可用，但不能拿来跑 Demucs；提取阶段会通过 sidecar
        # metadata 判断格式并重新生成，避免切换配置后误复用缓存。
        audio_metadata = AssetResolver.artifact(tid, "audio.meta.json").address
        requires_high_quality = (
            context.vocal_separation_enabled
            if context.vocal_separation_enabled is not None
            else settings.vocal_separation_enabled
        )
        metadata_matches = _audio_metadata_matches(
            audio_metadata,
            sample_rate=48000 if requires_high_quality else settings.audio_sample_rate,
            channels=2 if requires_high_quality else settings.audio_channels,
            source_path=context.resources.video_path,
        )
        # Older tasks predate the sidecar metadata. Reuse their non-empty audio
        # cache when vocal separation is disabled; requiring metadata here would
        # make every resumed task invoke ffmpeg again.
        legacy_audio_cache = (
            not audio_metadata.exists()
            and context.artifact_available(AssetResolver.resolve_audio)
        )
        if context.artifact_available(AssetResolver.resolve_audio) and (metadata_matches or legacy_audio_cache):
            context.resources.audio_path = AssetResolver.require_audio(tid)
            context.emit("EXTRACTING", 35)
        else:
            extract_options = {
                "on_progress": context.step_callback("EXTRACTING"),
            }
            if requires_high_quality:
                extract_options.update(sample_rate=48000, channels=2)
            result = extract_audio(context.resources.video_path, tid, **extract_options)
            # Custom extractors and test doubles may write the standard artifact
            # but return no response object; resolve the artifact as the source
            # of truth instead of dereferencing None.
            context.resources.audio_path = getattr(result, "audio_path", None)
            if context.resources.audio_path is None:
                context.resources.audio_path = AssetResolver.require_audio(tid)
            result_sample_rate = getattr(
                result, "sample_rate", 48000 if requires_high_quality else settings.audio_sample_rate
            )
            result_channels = getattr(
                result, "channels", 2 if requires_high_quality else settings.audio_channels
            )
            try:
                audio_metadata.write_text(
                    _audio_metadata_json(
                        sample_rate=result_sample_rate,
                        channels=result_channels,
                        source_path=context.resources.video_path,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                # 测试替身或外部 extractor 可能把音频写到其它目录；sidecar
                # 只是缓存校验辅助信息，不能让已成功的提取失败。
                logger.debug("无法写入音频 sidecar: %s", audio_metadata, exc_info=True)


class VocalSeparationHandler(PipelineHandler):
    """可选的人声抽取阶段；关闭时零成本透传原始音频。"""

    def process(self, context: PipelineContext) -> None:
        enabled = (
            context.vocal_separation_enabled
            if context.vocal_separation_enabled is not None
            else settings.vocal_separation_enabled
        )
        if not enabled:
            return
        tid = context.task_id
        context.emit("EXTRACTING", 35)
        output = task_dir(tid) / "vocal.wav"
        if cached_vocals_are_current(context.resources.audio_path, tid):
            context.resources.vocal_audio_path = output
            return
        try:
            try:
                context.resources.vocal_audio_path = separate_vocals(
                    context.resources.audio_path,
                    tid,
                    cancel_check=lambda: _check_cancelled(tid),
                )
            except TypeError as exc:
                # 兼容旧版自定义 backend / 测试替身的二参数签名；真实
                # separator 的 TypeError 仍会继续向外抛出。
                if "cancel_check" not in str(exc):
                    raise
                context.resources.vocal_audio_path = separate_vocals(
                    context.resources.audio_path, tid
                )
        except VocalSeparationCancelledError as exc:
            raise PipelineCancelledError("任务已被用户取消") from exc
        except VocalSeparationError:
            logger.exception("人声分离失败: task=%s", tid)
            raise


class TranscriptionHandler(PipelineHandler):
    """把音频转成原文 SRT，优先复用已有字幕产物。"""

    def process(self, context: PipelineContext) -> None:
        params = context.params
        tid = context.task_id
        context.emit("TRANSCRIBING", 35)
        if context.artifact_available(AssetResolver.resolve_original_srt):
            context.resources.original_srt_path = AssetResolver.require_original_srt(tid)
            context.emit("TRANSCRIBING", 65)
            return
        try:
            backend = None
            model_name = params.model
            if ":" in model_name:
                candidate, selected_model = model_name.split(":", 1)
                if candidate.strip().lower() in {"replicate", "local", "local_whisper", "whisper"}:
                    backend = "local_whisper" if candidate.strip().lower() != "replicate" else "replicate"
                    model_name = selected_model.strip()
            transcribe(
                context.resources.vocal_audio_path or context.resources.audio_path,
                tid,
                language=params.source_lang,
                model_name=model_name,
                backend=backend,
                on_progress=context.step_callback("TRANSCRIBING"),
                cancel_check=lambda: _check_cancelled(tid),
            )
        except TranscribeCancelledError as exc:
            raise PipelineCancelledError("任务已被用户取消") from exc
        context.resources.original_srt_path = AssetResolver.require_original_srt(tid)


class TranslationHandler(PipelineHandler):
    """翻译原文字幕，并在恢复时复用现有翻译产物。"""

    def process(self, context: PipelineContext) -> None:
        params = context.params
        tid = context.task_id
        context.emit("TRANSLATING", 65)
        if context.artifact_available(AssetResolver.resolve_translated_srt):
            context.resources.translated_srt_path = AssetResolver.require_translated_srt(tid)
            context.emit("TRANSLATING", 85)
            return
        translate_srt(
            context.resources.original_srt_path,
            tid,
            params.source_lang,
            params.target_lang,
            mode=params.mode,
            on_progress=context.step_callback("TRANSLATING"),
            api_key=context.api_key,
            engine_config=context.engine_config,
            cancel_check=lambda: _check_cancelled(tid),
        )
        context.resources.translated_srt_path = AssetResolver.require_translated_srt(tid)


class SubtitleBurningHandler(PipelineHandler):
    """将翻译字幕烧录到源视频并生成最终结果事件。"""

    def process(self, context: PipelineContext) -> None:
        params = context.params
        tid = context.task_id
        context.emit("BURNING", 85)
        context.resources.video_path = AssetResolver.require_source(tid)
        resolver = lambda task_id: AssetResolver.resolve_output_video(task_id, params.burn)
        if context.artifact_available(resolver):
            # Keep the legacy one-argument call contract for injected resolvers
            # and test doubles; mode-aware selection already happened above.
            context.resources.output_video_path = AssetResolver.require_output_video(tid)
            context.emit("BURNING", 94 if params.tts_enabled else 100)
        else:
            burn_subtitles(
                context.resources.video_path,
                context.resources.translated_srt_path,
                tid,
                mode=params.burn,
                on_progress=(lambda p: context.emit("BURNING", _scale(85, 94 if params.tts_enabled else 100, getattr(p, "percent", None)))),
            )
            context.resources.output_video_path = AssetResolver.require_output_video(tid)

        outputs = {
            "video": str(context.resources.output_video_path),
            "subtitle": str(context.resources.translated_srt_path),
        }
        if params.tts_enabled:
            self._add_dubbed_video(context, outputs)
        context.complete(outputs=outputs, title=context.title)
        logger.info("责任链完成: task=%s", tid)

    def _add_dubbed_video(self, context: PipelineContext, outputs: dict) -> None:
        """独立生成配音视频；失败时保留普通成品并只记录 TTS 错误。"""
        from src.config import settings, task_dir
        from src.core.vocal_separator import separate_background, separate_vocals
        tid = context.task_id
        params = context.params
        context.emit("SYNTHESIZING", 95, tts_status="RUNNING")
        try:
            task_path = task_dir(tid)
            high_quality_audio = task_path / "tts_source.wav"
            run_ffmpeg([
                settings.ffmpeg_bin, "-y", "-i", str(context.resources.video_path),
                "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
                "-progress", "pipe:1", "-nostats", "-loglevel", "error", str(high_quality_audio),
            ], task_id=tid)
            voice = params.tts_voice
            if voice == "auto":
                voice = "zf_001" if params.target_lang.lower().startswith("zh") else "af_maple"
            speech = generate_timeline_audio(
                context.resources.translated_srt_path,
                context.resources.original_srt_path,
                task_path / "dubbed.wav",
                target_lang=params.target_lang,
                voice=voice,
                bilingual=params.mode == "bilingual",
                duration=probe_duration(context.resources.video_path, settings.ffprobe_bin),
                cancel_check=lambda: _check_cancelled(tid),
                on_progress=lambda pct: context.emit("SYNTHESIZING", _scale(95, 97, pct)),
                task_id=tid,
            )
            context.emit("DUBBING", 97, tts_status="RUNNING")
            mixed = task_path / "dubbed_mix.wav"
            inputs = []
            filters = []
            if params.original_voice_mode == "keep":
                inputs += ["-i", str(high_quality_audio)]
                filters.append("[0:a]volume=1.0[base]")
                speech_index = 1
            else:
                bg = separate_background(
                    high_quality_audio, tid,
                    cancel_check=lambda: _check_cancelled(tid),
                )
                inputs += ["-i", str(bg)]
                filters.append("[0:a]volume=1.0[base]")
                speech_index = 1
                if params.original_voice_mode == "lower":
                    vocal = task_path / "vocal_stem.wav"
                    if not vocal.is_file():
                        raise TTSError("Demucs 未生成高质量人声 stem")
                    inputs += ["-i", str(vocal)]
                    filters.append("[1:a]volume=0.25[vocal]")
                    speech_index = 2
            inputs += ["-i", str(speech)]
            filters.append(f"[{speech_index}:a]volume=1.0[tts]")
            labels = "[base]" + ("[vocal]" if params.original_voice_mode == "lower" else "") + "[tts]"
            filters.append(f"{labels}amix=inputs={2 if params.original_voice_mode != 'lower' else 3}:duration=longest:normalize=0[mix]")
            run_ffmpeg([settings.ffmpeg_bin, "-y", *inputs, "-filter_complex", ";".join(filters), "-map", "[mix]", "-ar", "48000", "-progress", "pipe:1", "-nostats", "-loglevel", "error", str(mixed)], task_id=tid)
            dubbed_video = task_path / "output_dubbed.mp4"
            base_video = context.resources.output_video_path
            run_ffmpeg([
                settings.ffmpeg_bin, "-y", "-i", str(base_video), "-i", str(mixed),
                "-map", "0:v:0", "-map", "1:a:0", "-map", "0:s?", "-c:v", "copy",
                "-c:a", "aac", "-c:s", "copy", "-progress", "pipe:1", "-nostats", "-loglevel", "error", str(dubbed_video),
            ], task_id=tid)
            outputs["dubbedVideo"] = str(dubbed_video)
            context.emit("DUBBING", 99, tts_status="SUCCESS")
        except PipelineCancelledError:
            raise
        except Exception as exc:
            logger.exception("TTS 生成失败，保留原译制视频: task=%s", tid)
            context.emit("DUBBING", 99, tts_status="FAILED", tts_error=str(exc))


def build_pipeline_chain(*handlers: PipelineHandler) -> PipelineHandler:
    """按给定顺序连接 handler，返回责任链入口。"""
    if not handlers:
        raise ValueError("责任链至少需要一个 handler")
    for current, next_handler in zip(handlers, handlers[1:]):
        current.set_next(next_handler)
    handlers[-1]._next_handler = None
    return handlers[0]


def build_default_pipeline_chain() -> PipelineHandler:
    """创建下载 → 提取 → 转写 → 翻译 → 烧录的标准任务责任链。"""
    return build_pipeline_chain(
        DownloadHandler(),
        AudioExtractionHandler(),
        VocalSeparationHandler(),
        TranscriptionHandler(),
        TranslationHandler(),
        SubtitleBurningHandler(),
    )


def run_pipeline(
    params: PipelineParams,
    on_event: EventHook,
    *,
    api_key: Optional[str] = None,
    engine_config=None,
    handler_chain: Optional[PipelineHandler] = None,
) -> PipelineEvent:
    """执行任务责任链，并按已有产物从最近完成的阶段继续。"""
    context = PipelineContext(
        params=params,
        on_event=on_event,
        api_key=api_key,
        engine_config=engine_config,
        vocal_separation_enabled=settings.vocal_separation_enabled,
    )
    _check_cancelled(params.task_id)

    try:
        chain = handler_chain if handler_chain is not None else build_default_pipeline_chain()
        return chain.handle(context)

    except PipelineCancelledError:
        logger.info("责任链已被用户取消: task=%s", params.task_id)
        AssetResolver.cleanup_cancelled_artifacts(
            params.task_id,
            current_step=context.current_step,
            source_type=params.source_type,
        )
        raise
    except ResourceError as exc:
        if is_cancelled_signal(params.task_id):
            logger.info("责任链已被用户取消: task=%s", params.task_id)
            AssetResolver.cleanup_cancelled_artifacts(
                params.task_id,
                current_step=context.current_step,
                source_type=params.source_type,
            )
            raise PipelineCancelledError("任务已被用户取消") from exc
        logger.error(
            "责任链因资源异常中断: task=%s step=%s, msg=%s",
            params.task_id,
            context.current_step,
            str(exc),
        )
        on_event(PipelineEvent(
            status="FAILED",
            progress=context.progress,
            current_step=context.current_step,
            error=str(exc),
            error_code=getattr(exc, "code", "resource_error"),
        ))
        raise
    except Exception as exc:
        if is_cancelled_signal(params.task_id):
            logger.info("责任链已被用户取消: task=%s", params.task_id)
            AssetResolver.cleanup_cancelled_artifacts(
                params.task_id,
                current_step=context.current_step,
                source_type=params.source_type,
            )
            raise PipelineCancelledError("任务已被用户取消") from exc
        logger.exception("责任链失败: task=%s step=%s", params.task_id, context.current_step)
        on_event(PipelineEvent(
            status="FAILED",
            progress=context.progress,
            current_step=context.current_step,
            error=str(exc),
            error_code=_error_code_for_exception(exc),
        ))
        raise


def _locate_uploaded_source(task_id: str) -> Path:
    """定位上传模式下预先落盘的源视频 data/{task_id}/source.*。"""
    try:
        return AssetResolver.require_source(task_id)
    except ResourceError as e:
        raise PipelineError(str(e)) from e


def _audio_metadata_json(*, sample_rate: int, channels: int, source_path: Optional[Path]) -> str:
    import json
    payload = {"sample_rate": int(sample_rate), "channels": int(channels)}
    if source_path is not None:
        try:
            stat = source_path.stat()
            payload.update({
                "source": str(source_path.resolve()),
                "source_size": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
            })
        except OSError:
            pass
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _audio_metadata_matches(
    path: Path,
    *,
    sample_rate: int,
    channels: int,
    source_path: Optional[Path] = None,
) -> bool:
    """检查音频 sidecar；不存在时返回 False，让本次提取建立元数据。"""
    if not path.is_file():
        return False
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        matches = int(data.get("sample_rate")) == int(sample_rate) and int(data.get("channels")) == int(channels)
        if not matches or source_path is None:
            return matches
        stat = source_path.stat()
        return (
            data.get("source") == str(source_path.resolve())
            and int(data.get("source_size", -1)) == stat.st_size
            and int(data.get("source_mtime_ns", -1)) == stat.st_mtime_ns
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
