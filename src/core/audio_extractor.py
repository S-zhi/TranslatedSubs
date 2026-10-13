"""流水线第②步：从视频提取音频。

输入：source.mp4（下载阶段的产物） + 任务 ID
输出：AudioResult（其中 audio_path 指向 data/{task_id}/audio.wav）

用 ffmpeg 把视频音轨抽成 16kHz 单声道 PCM WAV，这是 Whisper / faster-whisper
的标准输入格式：提前重采样能减小体积、避免识别阶段重复转码。

进度通过 on_progress 回调透传（解析 ffmpeg 的 -progress 输出），供编排层转 SSE。

依赖：ffmpeg、ffprobe（需在 PATH 中，或用 SUBTRANS_FFMPEG / SUBTRANS_FFPROBE 指定）。
"""

from __future__ import annotations

import logging
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from src.config import settings, ensure_task_dir, AUDIO_FILENAME
from src.core.ffmpeg_utils import run_ffmpeg
from src.service.asset_resolver import AssetResolver, ResourceState

logger = logging.getLogger(__name__)


class AudioExtractError(RuntimeError):
    """提取音频阶段失败统一封装错误"""


@dataclass
class AudioProgress:
    """单次进度回调的数据。percent 为 0-100；总时长未知时为 None。"""

    percent: Optional[float]
    processed_seconds: float          # 已处理的媒体时长（秒）
    total_seconds: Optional[float]    # 媒体总时长（秒）


@dataclass
class AudioResult:
    """提取结果。"""

    audio_path: Path
    sample_rate: int
    channels: int
    duration: Optional[float]   # 秒
    filesize: Optional[int]     # 字节


ProgressHook = Callable[[AudioProgress], None]


def extract_audio(
    video_path: Path | str,
    task_id: str,
    on_progress: Optional[ProgressHook] = None,
    *,
    sample_rate: Optional[int] = None,
    channels: Optional[int] = None,
    output_path: Path | str | None = None,
) -> AudioResult:
    """从视频提取音频到任务目录，默认路径仍是 audio.wav。
    可以共用
    Args:
        video_path: 输入视频路径（通常是下载阶段的 source.mp4）。
        task_id: 任务 ID，决定输出目录。
        on_progress: 可选进度回调。
        sample_rate: 采样率，默认取配置（16000）。
        channels: 声道数，默认取配置（1）。
        output_path: 可选任务目录内 WAV 目标路径。

    Returns:
        AudioResult

    Raises:
        AudioExtractError: 输入不存在、ffmpeg 失败或产物缺失。
    """
    video_path = Path(video_path)
    state = AssetResolver.check_file_state(video_path)
    if state == ResourceState.DELETED:
        logger.error(f"输入视频不存在: {video_path}")
        raise AudioExtractError(f"输入视频不存在: {video_path}")
    elif state == ResourceState.UNREADABLE:
        logger.error(f"输入视频损坏或不可读: {video_path}")
        raise AudioExtractError(f"输入视频损坏或不可读: {video_path}")

    if not _has_audio_stream(video_path):
        logger.error(f"源视频不包含音频流，无法提取音频: {video_path}")
        raise AudioExtractError("源视频不包含音频流，无法提取音频")

    sr = sample_rate or settings.audio_sample_rate
    ch = channels or settings.audio_channels

    out_dir = ensure_task_dir(task_id)
    audio_path = out_dir / AUDIO_FILENAME if output_path is None else Path(output_path)
    try:
        task_root = out_dir.resolve(strict=True)
        resolved_output = audio_path.resolve()
        resolved_output.relative_to(task_root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise AudioExtractError("音频输出路径必须位于任务目录内") from exc
    if audio_path.is_symlink() or resolved_output.parent != task_root:
        raise AudioExtractError("音频输出路径必须是任务目录根目录中的普通文件")
    if output_path is not None:
        if (
            not audio_path.is_absolute()
            or audio_path.name not in {"audio.wav", "tts_source.wav"}
        ):
            raise AudioExtractError("音频输出路径必须是任务目录内的保留 WAV 目标")
    if audio_path.resolve() == video_path.resolve():
        raise AudioExtractError("音频输出路径不能覆盖输入视频")

    total = _probe_duration(video_path)

    cmd = [
        settings.ffmpeg_bin,
        "-y",                      # 覆盖已存在文件（支持重跑）
        "-i", str(video_path),
        "-vn",                     # 丢弃视频流
        "-ac", str(ch),
        "-ar", str(sr),
        "-c:a", "pcm_s16le",       # 16-bit PCM WAV
        "-progress", "pipe:1",     # 进度以 key=value 写到 stdout
        "-nostats",
        "-loglevel", "error",      # 真正的错误才写 stderr
        str(audio_path),
    ]

    logger.info("开始提取音频: task=%s -> %s", task_id, audio_path.name)
    _run_ffmpeg(cmd, total, on_progress, task_id=task_id)

    if not audio_path.exists() or audio_path.stat().st_size == 0:
        raise AudioExtractError("ffmpeg 执行完成但未生成有效音频文件")

    try:
        with wave.open(str(audio_path), "rb") as audio_file:
            actual_sample_rate = audio_file.getframerate()
            actual_channels = audio_file.getnchannels()
            frame_count = audio_file.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise AudioExtractError("ffmpeg 产物不是有效的 WAV 音频文件") from exc
    if actual_sample_rate <= 0 or actual_channels <= 0:
        raise AudioExtractError("ffmpeg 产物的音频格式无效")
    result = AudioResult(
        audio_path=audio_path,
        sample_rate=actual_sample_rate,
        channels=actual_channels,
        duration=frame_count / actual_sample_rate,
        filesize=_safe_filesize(audio_path),
    )
    logger.info(
        "音频提取完成: task=%s file=%s (%.1f MB, %s)",
        task_id,
        audio_path.name,
        (result.filesize or 0) / 1024 / 1024,
        f"{total:.1f}s" if total else "时长未知",
    )
    return result


def _run_ffmpeg(
    cmd: list[str],
    total_seconds: Optional[float],
    on_progress: Optional[ProgressHook],
    task_id: Optional[str] = None,
) -> None:
    """执行 ffmpeg 并解析 -progress 输出，逐步回调进度。"""
    last_pct = -1.0

    def _tick(processed: float) -> None:
        nonlocal last_pct
        if on_progress is None:
            return
        pct = None
        if total_seconds and total_seconds > 0:
            pct = max(0.0, min(100.0, processed / total_seconds * 100.0))
        # 去抖：百分比无变化时不重复回调
        if pct is None or round(pct, 1) != last_pct:
            last_pct = round(pct, 1) if pct is not None else last_pct
            _safe_call(on_progress, AudioProgress(
                percent=round(pct, 1) if pct is not None else None,
                processed_seconds=processed,
                total_seconds=total_seconds,
            ))

    not_found_msg = (
        f"找不到 ffmpeg（{cmd[0]}）。请安装 ffmpeg-full 或设置 SUBTRANS_FFMPEG。"
        " MAC用户请使用  brew install ffmpeg-full,请注意不要下载 ffmpeg，"
        "ffmpeg-full 是 ffmpeg 的完整版本，仅下载ffmpeg可能会导致字幕无法烧录到视频"
    )

    run_ffmpeg(
        cmd,
        on_tick=_tick,
        error_cls=AudioExtractError,
        not_found_msg=not_found_msg,
        task_id=task_id,
    )

    if on_progress is not None:
        _safe_call(on_progress, AudioProgress(
            percent=100.0 if total_seconds else None,
            processed_seconds=total_seconds or 0.0,
            total_seconds=total_seconds,
        ))


def _has_audio_stream(video_path: Path) -> bool:
    """用 ffprobe 判断是否存在音频流。ffprobe 不可用时返回 True（交给 ffmpeg 兜底）。"""
    cmd = [
        settings.ffprobe_bin,
        "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        str(video_path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return True
    if out.returncode != 0:
        return True
    return bool(out.stdout.strip())


def _probe_duration(video_path: Path) -> Optional[float]:
    """用 ffprobe 探测媒体总时长（秒）。失败返回 None（不影响提取，只是没法算百分比）。"""
    cmd = [
        settings.ffprobe_bin,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return float(out.stdout.strip())
    except (ValueError, AttributeError):
        return None


def _parse_us(value: str) -> Optional[float]:
    """把 ffmpeg 的 out_time_us（微秒）转成秒。"""
    try:
        return int(value) / 1_000_000
    except (ValueError, TypeError):
        return None


def _safe_call(hook: ProgressHook, p: AudioProgress) -> None:
    try:
        hook(p)
    except Exception:  # 回调异常不应中断提取
        logger.exception("进度回调异常，已忽略")


def _safe_filesize(path: Path) -> Optional[int]:
    try:
        return path.stat().st_size
    except OSError:
        return None


