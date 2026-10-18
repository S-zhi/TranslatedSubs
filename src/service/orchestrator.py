"""流水线编排：把 ①~⑤ 串成一条任务，逐步上报进度。

设计为纯逻辑：不碰数据库 / Redis，只通过 on_event 回调把状态与进度往外抛。
Worker 层把 on_event 接到「写 SQLite + 发 SSE」即可。

各步的内部百分比按权重映射到整体 0-100：
  下载 0-20 · 提取 20-35 · 识别 35-65 · 翻译 65-85 · 烧录 85-100
"""

from __future__ import annotations

import logging
import threading
import uuid
import wave
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Optional

from src.config import settings, task_dir
from src.plugin_system import (
    AudioExtractRequest,
    AudioExtractResult,
    DownloadRequest,
    DubMuxRequest,
    PluginCancelled,
    PluginError,
    ProbeRequest,
    ProbeResult,
    SubtitleRenderRequest,
    TTSRequest,
    TranscribeRequest,
    TranslateRequest,
    VocalSeparateRequest,
)
from src.plugin_system.context import PluginContext
from src.service.asset_resolver import AssetResolver, ResourceError, ResourceState
from src.store import DEFAULT_TRANSLATION_ENGINE_ID

logger = logging.getLogger(__name__)

_cancel_events: dict[str, threading.Event] = {}
_cancel_events_lock = threading.Lock()


def register_cancellation_signal(task_id: str, is_cancelled: bool = False) -> threading.Event:
    """注册任务取消事件，并按初始状态设置其标记。"""
    with _cancel_events_lock:
        ev = _cancel_events.setdefault(task_id, threading.Event())
        if is_cancelled:
            ev.set()
        else:
            ev.clear()
        return ev


def unregister_cancellation_signal(task_id: str) -> None:
    """移除任务取消事件，释放全局注册表中的任务引用。"""
    with _cancel_events_lock:
        _cancel_events.pop(task_id, None)


def set_cancelled_signal(task_id: str) -> None:
    """将任务标记为已取消；任务尚未注册时先创建对应事件。"""
    with _cancel_events_lock:
        ev = _cancel_events.setdefault(task_id, threading.Event())
        ev.set()


def is_cancelled_signal(task_id: str) -> bool:
    """读取任务取消标记；未注册的任务视为尚未取消。"""
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
    engine: str = DEFAULT_TRANSLATION_ENGINE_ID
    source_type: str = "url"    # url=在线链接下载 upload=本地上传视频
    need_subtitle: bool = True  # False = 仅下载视频，跳过识别/翻译/烧录
    title: Optional[str] = None  # 上传模式下用原始文件名作为展示标题
    quality: Optional[str] = None  # 下载清晰度策略：best/1080p/720p/480p/360p/audio_only
    tts_enabled: bool = False
    tts_voice: str = "auto"
    tts_speed: float = 1.0
    tts_format: str = "wav"
    tts_model: Optional[str] = None
    tts_provider: str = "kokoro"
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
    """将阶段百分比限制在 0 到 100，并映射到整体进度区间。"""
    if pct is None:
        return lo
    return int(lo + max(0.0, min(100.0, pct)) / 100.0 * (hi - lo))


def _check_cancelled(task_id: str) -> None:
    """在继续执行前检查任务取消标记，已取消时抛出统一异常。"""
    if is_cancelled_signal(task_id):
        raise PipelineCancelledError("任务已被用户取消")


def _stage_for_purpose(purpose: str) -> str:
    return {
        "download": "download",
        "extract.asr": "audio_extract",
        "extract.dubbing": "audio_extract",
        "separate.asr": "vocal_separate",
        "separate.dubbing": "vocal_separate",
        "transcribe": "transcribe",
        "translate": "translate",
        "tts": "tts",
        "render.subtitle": "render",
        "render.dub_mux": "render",
    }.get(purpose, "download")


def _purpose_band(purpose: str, params: PipelineParams) -> tuple[str, int, int]:
    status = {
        "download": "DOWNLOADING",
        "extract.asr": "EXTRACTING",
        "extract.dubbing": "SYNTHESIZING",
        "separate.asr": "EXTRACTING",
        "separate.dubbing": "DUBBING",
        "transcribe": "TRANSCRIBING",
        "translate": "TRANSLATING",
        "tts": "SYNTHESIZING",
        "render.subtitle": "BURNING",
        "render.dub_mux": "DUBBING",
    }[purpose]
    if purpose == "download" and not params.need_subtitle:
        return status, 0, 100
    if purpose == "extract.dubbing":
        return status, 94, 95
    if purpose == "separate.asr":
        return status, 35, 35
    if purpose == "separate.dubbing":
        return status, 95, 96
    if purpose == "tts":
        return status, 95, 97
    if purpose == "render.subtitle" and params.tts_enabled:
        return status, 85, 94
    if purpose == "render.dub_mux":
        return status, 97, 100
    return (status, *_BANDS[status])


class _PipelinePluginContext:
    """Fresh host callback context for one Manager invocation."""

    def __init__(self, pipeline: "PipelineContext", purpose: str) -> None:
        self._pipeline = pipeline
        self._task_id = pipeline.task_id
        self._invocation_id = str(uuid.uuid4())
        self._task_dir = pipeline.directory
        self._purpose = purpose
        self._active = True
        self._lock = threading.RLock()

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def invocation_id(self) -> str:
        return self._invocation_id

    @property
    def task_dir(self) -> Path:
        return self._task_dir

    @property
    def purpose(self) -> str:
        return self._purpose

    def check_cancelled(self) -> None:
        if is_cancelled_signal(self._task_id):
            raise PluginCancelled("plugin invocation cancelled")

    def report_progress(self, percent: float | None) -> None:
        with self._lock:
            if not self._active:
                return
            self.check_cancelled()
            status, low, high = _purpose_band(self._purpose, self._pipeline.params)
            scaled = low if percent is None else _scale(low, high, percent)
            try:
                self._pipeline.emit(status, scaled)
            except PipelineCancelledError as exc:
                raise PluginCancelled("plugin invocation cancelled") from exc

    def register_process(self, proc) -> None:
        from src.service.runner import register_process
        register_process(self._task_id, proc)

    def unregister_process(self, proc) -> None:
        from src.service.runner import unregister_process
        unregister_process(self._task_id, proc)

    def resolve_credential(self, ref: str) -> str:
        return self._pipeline.credential_resolver(ref)

    def finish(self) -> None:
        with self._lock:
            self._active = False


def _result_paths(result: object) -> set[Path]:
    paths = set()
    for name in (
        "source_path", "audio_path", "vocal_path", "background_path",
        "original_srt", "translated_srt", "speech_audio", "mixed_audio", "video_path",
    ):
        value = getattr(result, name, None)
        if isinstance(value, Path):
            paths.add(value)
    return paths


def _wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as audio:
            rate = audio.getframerate()
            return audio.getnframes() / rate if rate > 0 else None
    except (OSError, wave.Error):
        return None


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
    plugin_manager: object
    plugin_plan: Mapping
    credential_resolver: Callable[[str], str]
    legacy_cache: bool = False
    resources: PipelineResources = field(default_factory=PipelineResources)
    progress: int = 0
    current_step: Optional[str] = None
    title: Optional[str] = None
    terminal_event: Optional[PipelineEvent] = None
    # 配置在任务启动时快照，避免用户在任务运行中切换开关导致音频格式
    # 与后续分离阶段不一致。直接构造 Context 的旧调用方可留 None，回退
    # 到动态 settings（主要用于单元测试和自定义责任链）。
    vocal_separation_enabled: Optional[bool] = None
    _unpublished: set[Path] = field(default_factory=set)
    _pending_purposes: set[str] = field(default_factory=set)

    @property
    def task_id(self) -> str:
        """返回上下文关联任务的唯一标识。"""
        return self.params.task_id

    def emit(self, status: str, progress: int, **extra) -> None:
        """发布阶段事件，并单调更新上下文中的整体进度。"""
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
        """创建把底层阶段进度换算到整体区间的回调函数。"""
        if status == "DOWNLOADING" and not self.params.need_subtitle:
            lo, hi = (0, 100)
        else:
            lo, hi = _BANDS[status]

        def callback(progress) -> None:
            """接收底层进度对象，并转发为当前阶段事件。"""
            self.emit(status, _scale(lo, hi, getattr(progress, "percent", None)))

        return callback

    def emit_smooth(self, status: str, target: int) -> None:
        """通过少量中间事件平滑推进到目标进度。"""
        start = self.progress
        if target > start:
            step_size = max(1, (target - start) // 4)
            current = start + step_size
            while current < target:
                self.emit(status, current)
                current += step_size
        self.emit(status, target)

    def artifact_available(self, resolver) -> bool:
        """调用资源解析器，判断对应产物是否可以复用。"""
        resource_state, path, _ = resolver(self.task_id)
        return resource_state == ResourceState.AVAILABLE and path is not None

    def check_cancelled(self) -> None:
        if is_cancelled_signal(self.task_id):
            raise PipelineCancelledError("任务已被用户取消")

    @property
    def directory(self) -> Path:
        return task_dir(self.task_id).resolve()

    def _slot(self, purpose: str) -> Mapping:
        try:
            return self.plugin_plan["slots"][purpose]
        except (KeyError, TypeError) as exc:
            raise PluginError("invalid_input", _stage_for_purpose(purpose), "host", "required plugin slot is missing", exc) from exc

    def invoke(
        self,
        purpose: str,
        request,
        *,
        stage: str,
        operation: str,
        inputs: dict[str, object],
        output_names: tuple[str, ...],
        upstream_purposes: Optional[dict[str, str]] = None,
        legacy_compatible: bool = False,
        cache_check: bool = True,
    ):
        slot = self._slot(purpose)
        self.directory.mkdir(parents=True, exist_ok=True)
        plugin_id = slot["plugin_id"]
        version = slot["implementation_version"]
        config = dict(slot["config"])
        legacy_allowed_plugin = {
            "download": "builtin.ytdlp",
            "audio_extract": "builtin.ffmpeg_audio",
            "vocal_separate": "builtin.vocal_demucs",
            "transcribe": "builtin.whisper",
            "translate": "builtin.translation_engine",
            "tts": "builtin.kokoro",
            "render": "builtin.ffmpeg_render",
        }[stage]
        effective_outputs = output_names
        if purpose == "download" and not effective_outputs:
            state, source, _ = AssetResolver.resolve_source(self.task_id)
            if state == ResourceState.AVAILABLE and source is not None:
                effective_outputs = (source.name,)
        if cache_check and effective_outputs and AssetResolver.plugin_cache_matches(
            self.task_id,
            stage=stage,
            operation=operation,
            purpose=purpose,
            plugin_id=plugin_id,
            implementation_version=version,
            config=config,
            inputs=inputs,
            output_names=effective_outputs,
            upstream_purposes=upstream_purposes,
            legacy_cache=(self.legacy_cache and plugin_id == legacy_allowed_plugin and version == "1"),
            legacy_compatible=legacy_compatible,
        ):
            self._emit_stage_complete(purpose)
            return None

        output_paths = {self.directory / name for name in output_names}
        previously_unpublished = self._unpublished.copy()
        self._unpublished.update(output_paths)
        self._pending_purposes.add(purpose)
        preexisting_download_files = set()
        if purpose == "download":
            preexisting_download_files = {
                path.resolve() for pattern in ("source.*", "*.part", "*.ytdl", "*.tmp")
                for path in self.directory.glob(pattern)
            }
        invocation_context = _PipelinePluginContext(self, purpose)
        try:
            result = self.plugin_manager.run(
                plugin_id, version, request, config, invocation_context
            )
            invocation_context.finish()
            result_paths = _result_paths(result)
            self._unpublished.update(result_paths)
            self.check_cancelled()
            result_names = tuple(path.name for path in result_paths)
            # Preserve request order for stable metadata.
            if output_names:
                result_names = output_names
            metadata = AssetResolver.plugin_metadata(
                task_id=self.task_id,
                stage=stage,
                operation=operation,
                purpose=purpose,
                plugin_id=plugin_id,
                implementation_version=version,
                config=config,
                inputs=inputs,
                output_names=result_names,
                upstream_purposes=upstream_purposes,
            )
            AssetResolver.write_plugin_cache_metadata(self.task_id, metadata)
            # Metadata is the publication record. Recheck cancellation after it
            # is written but before the invocation is considered published.
            self.check_cancelled()
            self._unpublished.difference_update(result_paths)
            self._unpublished.difference_update(output_paths)
            self._pending_purposes.discard(purpose)
            return result
        except PluginCancelled as exc:
            invocation_context.finish()
            if purpose == "download":
                self._collect_new_download_files(preexisting_download_files)
            self.cleanup_unpublished()
            raise PipelineCancelledError("任务已被用户取消") from exc
        except Exception:
            invocation_context.finish()
            if purpose == "download":
                self._collect_new_download_files(preexisting_download_files)
            # A failed call must not leave artifacts that a later run can mistake
            # for published output. Preserve already completed purposes.
            failed_paths = output_paths | (self._unpublished - previously_unpublished)
            AssetResolver.cleanup_unpublished_invocation(
                self.task_id, failed_paths, {purpose}
            )
            self._unpublished.difference_update(failed_paths)
            self._pending_purposes.discard(purpose)
            raise

    def _collect_new_download_files(self, preexisting: set[Path]) -> None:
        for pattern in ("source.*", "*.part", "*.ytdl", "*.tmp"):
            for path in self.directory.glob(pattern):
                resolved = path.resolve()
                if resolved not in preexisting:
                    self._unpublished.add(path)

    def cleanup_unpublished(self) -> None:
        AssetResolver.cleanup_unpublished_invocation(
            self.task_id, self._unpublished, self._pending_purposes
        )
        self._unpublished.clear()
        self._pending_purposes.clear()

    def _emit_stage_complete(self, purpose: str) -> None:
        status, lo, hi = _purpose_band(purpose, self.params)
        self.emit(status, hi)

    def invoke_context(self, purpose: str) -> PluginContext:
        return _PipelinePluginContext(self, purpose)

    def complete(self, *, outputs: dict, title: Optional[str] = None) -> PipelineEvent:
        """记录并发布包含输出路径的成功终态事件。"""
        self.check_cancelled()
        event = PipelineEvent("SUCCESS", 100, None, title=title, outputs=outputs)
        self.terminal_event = event
        self.on_event(event)
        return event


class PipelineHandler:
    """责任链节点：处理当前阶段，然后将共享上下文交给下一节点。"""

    def __init__(self) -> None:
        """初始化责任链节点的后继引用。"""
        self._next_handler: Optional[PipelineHandler] = None

    def set_next(self, handler: "PipelineHandler") -> "PipelineHandler":
        """设置后继节点并返回它，以便连续组装责任链。"""
        self._next_handler = handler
        return handler

    def handle(self, context: PipelineContext) -> PipelineEvent:
        """执行当前节点，并将上下文交给后继节点直到得到终态事件。"""
        self.process(context)
        if context.terminal_event is not None:
            return context.terminal_event
        if self._next_handler is None:
            raise PipelineError("责任链未产生任务完成结果")
        return self._next_handler.handle(context)

    def process(self, context: PipelineContext) -> None:
        """处理当前阶段；具体责任链节点必须实现此方法。"""
        raise NotImplementedError


class DownloadHandler(PipelineHandler):
    """准备源视频；上传、断点续跑和仅下载分支均在本节点处理。"""

    def process(self, context: PipelineContext) -> None:
        """准备源视频；URL 下载只由 download 插件完成。"""
        params = context.params
        tid = context.task_id
        context.emit("DOWNLOADING", 0)
        if params.source_type == "upload":
            context.resources.video_path = _locate_uploaded_source(tid)
            context.title = params.title or context.resources.video_path.stem
            context.emit_smooth("DOWNLOADING", 100 if not params.need_subtitle else 20)
        else:
            slot = context._slot("download")
            config = dict(slot["config"])
            context.emit("DOWNLOADING", 0)
            request = DownloadRequest(
                stage="download", operation="download", purpose="download",
                url=params.url, quality=config["quality"], target_dir=context.directory,
            )
            existing_state, existing, _ = AssetResolver.resolve_source(tid)
            download = context.invoke(
                "download", request, stage="download", operation="download",
                inputs={"url": params.url}, output_names=(),
                legacy_compatible=existing is not None,
            )
            context.resources.video_path = AssetResolver.require_source(tid)
            context.title = (download.title if download is not None else None) or params.title or context.resources.video_path.stem
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
        """经 audio_extract 插件生成 ASR 音频并检查用途缓存。"""
        tid = context.task_id
        context.emit("EXTRACTING", 20)
        context.resources.video_path = AssetResolver.require_source(tid).resolve()
        slot = context._slot("extract.asr")
        config = dict(slot["config"])
        output = context.directory / "audio.wav"
        request = AudioExtractRequest(
            stage="audio_extract", operation="audio_extract", purpose="extract.asr",
            source_path=context.resources.video_path, output_path=output,
            sample_rate=config["sample_rate"], channels=config["channels"],
        )
        legacy_audio = AssetResolver.check_file_state(output) == ResourceState.AVAILABLE
        old_meta = context.directory / "audio.meta.json"
        legacy_compatible = legacy_audio and (
            not old_meta.is_file() or _audio_metadata_matches(
                old_meta, sample_rate=config["sample_rate"], channels=config["channels"],
                source_path=context.resources.video_path,
            )
        )
        result = context.invoke(
            "extract.asr", request, stage="audio_extract", operation="audio_extract",
            inputs={"source_path": context.resources.video_path}, output_names=("audio.wav",),
            upstream_purposes={"source_path": "download"} if context.params.source_type == "url" else None,
            legacy_compatible=legacy_compatible,
        )
        context.resources.audio_path = result.audio_path if result else output
        context.emit("EXTRACTING", 35)


class VocalSeparationHandler(PipelineHandler):
    """可选的人声抽取阶段；关闭时零成本透传原始音频。"""

    def process(self, context: PipelineContext) -> None:
        """按配置复用或生成分离后的人声音频，关闭时直接透传。"""
        enabled = (
            context.vocal_separation_enabled
            if context.vocal_separation_enabled is not None
            else settings.vocal_separation_enabled
        )
        if not enabled:
            return
        tid = context.task_id
        context.emit("EXTRACTING", 35)
        slot = context._slot("separate.asr")
        request = VocalSeparateRequest(
            stage="vocal_separate", operation="vocal_separate", purpose="separate.asr",
            audio_path=context.resources.audio_path,
            vocal_path=context.directory / "vocal.wav", background_path=None,
        )
        result = context.invoke(
            "separate.asr", request, stage="vocal_separate", operation="vocal_separate",
            inputs={"audio_path": context.resources.audio_path}, output_names=("vocal.wav",),
            upstream_purposes={"audio_path": "extract.asr"},
            legacy_compatible=(context.legacy_cache and (context.directory / "vocal.meta.json").is_file()),
        )
        context.resources.vocal_audio_path = result.vocal_path if result else request.vocal_path


class TranscriptionHandler(PipelineHandler):
    """把音频转成原文 SRT，优先复用已有字幕产物。"""

    def process(self, context: PipelineContext) -> None:
        """复用或生成原文 SRT，并把识别进度映射到流水线。"""
        params = context.params
        tid = context.task_id
        context.emit("TRANSCRIBING", 35)
        try:
            slot = context._slot("transcribe")
            config = dict(slot["config"])
            audio_path = context.resources.vocal_audio_path or context.resources.audio_path
            request = TranscribeRequest(
                stage="transcribe", operation="transcribe", purpose="transcribe",
                audio_path=audio_path, output_srt=context.directory / "original.srt",
                source_lang=params.source_lang, backend=config["backend"], model=config["model"],
            )
            result = context.invoke(
                "transcribe", request, stage="transcribe", operation="transcribe",
                inputs={"audio_path": audio_path}, output_names=("original.srt",),
                upstream_purposes={"audio_path": "separate.asr" if context.resources.vocal_audio_path else "extract.asr"},
                legacy_compatible=context.legacy_cache and (context.directory / "original.srt").is_file(),
            )
            context.resources.original_srt_path = result.original_srt if result else request.output_srt
        except PluginCancelled as exc:
            raise PipelineCancelledError("任务已被用户取消") from exc


class TranslationHandler(PipelineHandler):
    """翻译原文字幕，并在恢复时复用现有翻译产物。"""

    def process(self, context: PipelineContext) -> None:
        """复用或生成目标语言字幕，并把翻译进度映射到流水线。"""
        params = context.params
        tid = context.task_id
        context.emit("TRANSLATING", 65)
        slot = context._slot("translate")
        request = TranslateRequest(
            stage="translate", operation="translate", purpose="translate",
            original_srt=context.resources.original_srt_path,
            output_srt=context.directory / "translated.srt",
            source_lang=params.source_lang, target_lang=params.target_lang, mode=params.mode,
        )
        result = context.invoke(
            "translate", request, stage="translate", operation="translate",
            inputs={"original_srt": context.resources.original_srt_path}, output_names=("translated.srt",),
            upstream_purposes={"original_srt": "transcribe"},
            legacy_compatible=context.legacy_cache and (context.directory / "translated.srt").is_file(),
        )
        context.resources.translated_srt_path = result.translated_srt if result else request.output_srt


class SubtitleBurningHandler(PipelineHandler):
    """将翻译字幕烧录到源视频并生成最终结果事件。"""

    def process(self, context: PipelineContext) -> None:
        """复用或生成烧录视频，必要时追加配音并发布完成事件。"""
        params = context.params
        tid = context.task_id
        context.emit("BURNING", 85)
        context.resources.video_path = AssetResolver.require_source(tid).resolve()
        output = context.directory / f"output_{params.burn}.mp4"
        legacy_output = context.directory / "output.mp4"
        if context.legacy_cache and legacy_output.is_file() and not output.exists():
            legacy = AssetResolver.check_file_state(legacy_output) == ResourceState.AVAILABLE
            if legacy:
                legacy_output.replace(output)
        slot = context._slot("render.subtitle")
        request = SubtitleRenderRequest(
            stage="render", operation="subtitle", purpose="render.subtitle",
            source_video=context.resources.video_path,
            translated_srt=context.resources.translated_srt_path,
            output_video=output, mode=params.burn,
        )
        result = context.invoke(
            "render.subtitle", request, stage="render", operation="subtitle",
            inputs={"source_video": context.resources.video_path, "translated_srt": context.resources.translated_srt_path},
            output_names=(output.name,),
            upstream_purposes={
                "source_video": "download" if params.source_type == "url" else "",
                "translated_srt": "translate",
            },
            legacy_compatible=context.legacy_cache and output.is_file(),
        )
        context.resources.output_video_path = result.video_path if result else output
        context.emit("BURNING", 94 if params.tts_enabled else 100)

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
        tid = context.task_id
        params = context.params
        context.emit("SYNTHESIZING", 95, tts_status="RUNNING")
        try:
            extract_slot = context._slot("extract.dubbing")
            extract_config = dict(extract_slot["config"])
            high_quality_audio = context.directory / "tts_source.wav"
            extract_request = AudioExtractRequest(
                stage="audio_extract", operation="audio_extract", purpose="extract.dubbing",
                source_path=context.resources.video_path, output_path=high_quality_audio,
                sample_rate=extract_config["sample_rate"], channels=extract_config["channels"],
            )
            extract_result = context.invoke(
                "extract.dubbing", extract_request, stage="audio_extract", operation="audio_extract",
                inputs={"source_video": context.resources.video_path}, output_names=("tts_source.wav",),
                upstream_purposes={"source_video": "download" if params.source_type == "url" else ""},
                legacy_compatible=context.legacy_cache and high_quality_audio.is_file(),
            )
            if extract_result:
                high_quality_audio = extract_result.audio_path

            tts_slot = context._slot("tts")
            tts_config = dict(tts_slot["config"])
            voice = tts_config["voice"]
            speech_path = context.directory / "dubbed.wav"
            tts_request = TTSRequest(
                stage="tts", operation="tts", purpose="tts",
                translated_srt=context.resources.translated_srt_path,
                original_srt=context.resources.original_srt_path,
                output_audio=speech_path, target_lang=params.target_lang,
                voice=voice, bilingual=tts_config["bilingual"],
                duration_seconds=(extract_result.duration_seconds if extract_result else _wav_duration(high_quality_audio)),
            )
            tts_result = context.invoke(
                "tts", tts_request, stage="tts", operation="tts",
                inputs={"translated_srt": context.resources.translated_srt_path,
                        "original_srt": context.resources.original_srt_path},
                output_names=("dubbed.wav",),
                upstream_purposes={"translated_srt": "translate", "original_srt": "transcribe"},
                legacy_compatible=context.legacy_cache and speech_path.is_file(),
            )
            speech_path = tts_result.speech_audio if tts_result else speech_path
            context.emit("DUBBING", 97, tts_status="RUNNING")
            background = None
            vocal = None
            if params.original_voice_mode != "keep":
                separate_slot = context._slot("separate.dubbing")
                separate_request = VocalSeparateRequest(
                    stage="vocal_separate", operation="vocal_separate", purpose="separate.dubbing",
                    audio_path=high_quality_audio,
                    vocal_path=(context.directory / "vocal_stem.wav") if params.original_voice_mode == "lower" else None,
                    background_path=context.directory / "background.wav",
                )
                separate_result = context.invoke(
                    "separate.dubbing", separate_request, stage="vocal_separate", operation="vocal_separate",
                    inputs={"audio_path": high_quality_audio},
                    output_names=("background.wav", "vocal_stem.wav") if separate_request.vocal_path else ("background.wav",),
                    upstream_purposes={"audio_path": "extract.dubbing"},
                    legacy_compatible=context.legacy_cache and separate_request.background_path.is_file(),
                )
                if separate_result:
                    background = separate_result.background_path
                    vocal = separate_result.vocal_path
                else:
                    background = separate_request.background_path
                    vocal = separate_request.vocal_path
            mux_slot = context._slot("render.dub_mux")
            mux_request = DubMuxRequest(
                stage="render", operation="dub_mux", purpose="render.dub_mux",
                base_video=context.resources.output_video_path,
                source_audio=high_quality_audio, speech_audio=speech_path,
                background_audio=background, vocal_audio=vocal,
                mixed_audio=context.directory / "dubbed_mix.wav",
                output_video=context.directory / "output_dubbed.mp4",
                original_voice_mode=params.original_voice_mode,
            )
            mux_result = context.invoke(
                "render.dub_mux", mux_request, stage="render", operation="dub_mux",
                inputs={"base_video": context.resources.output_video_path,
                        "source_audio": high_quality_audio, "speech_audio": speech_path,
                        **({"background_audio": background} if background else {}),
                        **({"vocal_audio": vocal} if vocal else {})},
                output_names=("dubbed_mix.wav", "output_dubbed.mp4"),
                upstream_purposes={"base_video": "render.subtitle", "source_audio": "extract.dubbing",
                                   "speech_audio": "tts", **({"background_audio": "separate.dubbing"} if background else {}),
                                   **({"vocal_audio": "separate.dubbing"} if vocal else {})},
                legacy_compatible=context.legacy_cache and mux_request.output_video.is_file(),
            )
            dubbed_video = mux_result.video_path if mux_result else mux_request.output_video
            outputs["dubbedVideo"] = str(dubbed_video)
            context.emit("DUBBING", 99, tts_status="SUCCESS")
        except PipelineCancelledError:
            raise
        except PluginCancelled as exc:
            raise PipelineCancelledError("任务已被用户取消") from exc
        except PluginError as exc:
            logger.warning("TTS plugin failed: task=%s code=%s", tid, exc.code)
            context.emit("DUBBING", 99, tts_status="FAILED", tts_error=exc.message)
        except Exception as exc:
            if is_cancelled_signal(tid):
                raise PipelineCancelledError("任务已被用户取消") from exc
            logger.error("TTS plugin failed: task=%s code=%s", tid, _error_code_for_exception(exc))
            context.emit("DUBBING", 99, tts_status="FAILED", tts_error="配音生成失败")


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
    plugin_manager,
    plugin_plan: Mapping,
    credential_resolver: Callable[[str], str],
    legacy_cache: bool = False,
    handler_chain: Optional[PipelineHandler] = None,
) -> PipelineEvent:
    """执行通过版本化插件计划固定的责任链。"""
    _check_cancelled(params.task_id)
    slots = {
        purpose: MappingProxyType({
            "plugin_id": slot["plugin_id"],
            "implementation_version": slot["implementation_version"],
            "config": MappingProxyType(dict(slot["config"])),
        })
        for purpose, slot in plugin_plan["slots"].items()
    }
    context = PipelineContext(
        params=params,
        on_event=on_event,
        plugin_manager=plugin_manager,
        plugin_plan=MappingProxyType({"schema_version": plugin_plan["schema_version"], "slots": MappingProxyType(slots)}),
        credential_resolver=credential_resolver,
        legacy_cache=legacy_cache,
        vocal_separation_enabled="separate.asr" in slots,
    )
    try:
        chain = handler_chain if handler_chain is not None else build_default_pipeline_chain()
        return chain.handle(context)

    except PipelineCancelledError:
        logger.info("责任链已被用户取消: task=%s", params.task_id)
        context.cleanup_unpublished()
        raise
    except ResourceError as exc:
        if is_cancelled_signal(params.task_id):
            logger.info("责任链已被用户取消: task=%s", params.task_id)
            context.cleanup_unpublished()
            raise PipelineCancelledError("任务已被用户取消") from exc
        logger.error("责任链资源异常: task=%s step=%s", params.task_id, context.current_step)
        on_event(PipelineEvent(
            status="FAILED",
            progress=context.progress,
            current_step=context.current_step,
            error="任务资源不可用",
            error_code=getattr(exc, "code", "resource_error"),
        ))
        raise
    except Exception as exc:
        if is_cancelled_signal(params.task_id):
            logger.info("责任链已被用户取消: task=%s", params.task_id)
            context.cleanup_unpublished()
            raise PipelineCancelledError("任务已被用户取消") from exc
        if isinstance(exc, PluginError):
            logger.warning("责任链插件失败: task=%s stage=%s code=%s", params.task_id, exc.stage, exc.code)
            public_error = exc.message
            error_code = exc.code
        else:
            logger.error("责任链失败: task=%s step=%s code=%s", params.task_id, context.current_step, _error_code_for_exception(exc))
            public_error = "任务执行失败"
            error_code = _error_code_for_exception(exc)
        on_event(PipelineEvent(
            status="FAILED",
            progress=context.progress,
            current_step=context.current_step,
            error=public_error,
            error_code=error_code,
        ))
        raise


def _locate_uploaded_source(task_id: str) -> Path:
    """定位上传模式下预先落盘的源视频 data/{task_id}/source.*。"""
    try:
        return AssetResolver.require_source(task_id).resolve()
    except ResourceError as e:
        raise PipelineError(str(e)) from e


def _audio_metadata_json(*, sample_rate: int, channels: int, source_path: Optional[Path]) -> str:
    """构造音频缓存 sidecar JSON，记录格式及源文件信息。"""
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
