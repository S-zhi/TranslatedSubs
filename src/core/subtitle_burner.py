"""流水线第⑤步：把字幕烧录 / 内封到视频。

输入：source.mp4 + 字幕（通常是 translated.srt） + 任务 ID
输出：BurnResult（其中 output_path 指向 data/{task_id}/output.mp4）

两种模式：
  - hard（硬烧录）：subtitles 滤镜把字幕烧进画面，重编码视频，不可关闭。
  - soft（软字幕）：以 mov_text 内封为可开关的字幕轨，复制音视频流，速度快。

进度通过 on_progress 回调透传（复用 ffmpeg_utils 解析 -progress）。

依赖：ffmpeg（硬烧录需 libass 支持 subtitles 滤镜）。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from src.config import settings, ensure_task_dir, output_video_filename
from src.core.ffmpeg_utils import has_subtitles_filter, probe_duration, run_ffmpeg
from src.service.asset_resolver import AssetResolver, ResourceState

logger = logging.getLogger(__name__)


class BurnError(RuntimeError):
    """烧录字幕阶段失败。"""


@dataclass
class BurnProgress:
    percent: Optional[float]
    processed_seconds: float
    total_seconds: Optional[float]


@dataclass
class BurnResult:
    output_path: Path
    mode: str           # "hard" | "soft"
    filesize: Optional[int]


ProgressHook = Callable[[BurnProgress], None]


def _hard_cmd(video: Path, srt: Path, out: Path) -> list[str]:
    # subtitles 滤镜对路径里的 : ' \ 等极其敏感，转义很脆弱。
    # 改为：把 SRT 拷贝到任务目录下的 safe 临时文件名 tmp_burn.srt，
    # ffmpeg 以 out_dir 为 cwd，滤镜只引用 basename（纯 ASCII 无特殊字符/空格/中文），
    # 视频/输出走绝对路径（不经滤镜解析，不受影响）。
    return [
        settings.ffmpeg_bin, "-y",
        "-i", str(video.resolve()),
        "-vf", f"subtitles={srt.name}",
        "-c:a", "copy",           # 音频直接复制，只重编码视频
        "-progress", "pipe:1",
        "-nostats",
        "-loglevel", "error",
        str(out.resolve()),
    ]


def _soft_cmd(video: Path, srt: Path, out: Path) -> list[str]:
    return [
        settings.ffmpeg_bin, "-y",
        "-i", str(video),
        "-i", str(srt),
        "-map", "0", "-map", "1",
        "-c", "copy",             # 音视频流复制
        "-c:s", "mov_text",       # 字幕转 mp4 内封格式
        "-progress", "pipe:1",
        "-nostats",
        "-loglevel", "error",
        str(out),
    ]


def burn_subtitles(
    video_path: Path | str,
    srt_path: Path | str,
    task_id: str,
    on_progress: Optional[ProgressHook] = None,
    *,
    mode: str = "hard",
) -> BurnResult:
    """把字幕烧录 / 内封到视频，输出 data/{task_id}/output.mp4。

    Args:
        mode: "hard" 硬烧录（重编码）；"soft" 软字幕（内封可开关）。

    Raises:
        BurnError: 输入缺失、模式非法、ffmpeg 失败或产物缺失。
    """
    video_path = Path(video_path)
    srt_path = Path(srt_path)

    video_state = AssetResolver.check_file_state(video_path)
    if video_state == ResourceState.DELETED:
        raise BurnError(f"输入视频不存在: {video_path}")
    elif video_state == ResourceState.UNREADABLE:
        raise BurnError(f"输入视频损坏或不可读: {video_path}")

    srt_state = AssetResolver.check_file_state(srt_path)
    if srt_state == ResourceState.DELETED:
        raise BurnError(f"输入字幕不存在: {srt_path}")
    elif srt_state == ResourceState.UNREADABLE:
        raise BurnError(f"输入字幕损坏或不可读: {srt_path}")

    out_dir = ensure_task_dir(task_id)
    if mode not in ("hard", "soft"):
        raise BurnError(f"未知烧录模式: {mode}")
    out_path = out_dir / output_video_filename(mode)

    tmp_burn_path = out_dir / "tmp_burn.srt"
    cwd: Optional[str] = None
    if mode == "hard":
        if not has_subtitles_filter(settings.ffmpeg_bin):
            raise BurnError(
                "当前 ffmpeg 未编译 libass，硬烧录所需的 subtitles 滤镜不可用。"
                "请安装带 libass 的 ffmpeg（如 `brew reinstall ffmpeg`），"
                "或改用软字幕模式（mode='soft'）。"
            )
        shutil.copy2(srt_path, tmp_burn_path)
        cmd = _hard_cmd(video_path, tmp_burn_path, out_path)
        cwd = str(out_dir.resolve())  # 让滤镜引用 safe basename tmp_burn.srt
    elif mode == "soft":
        cmd = _soft_cmd(video_path, srt_path, out_path)

    total = probe_duration(video_path, settings.ffprobe_bin)

    def _tick(processed: float) -> None:
        if on_progress is None:
            return
        pct = max(0.0, min(100.0, processed / total * 100.0)) if total else None
        on_progress(BurnProgress(
            percent=round(pct, 1) if pct is not None else None,
            processed_seconds=processed,
            total_seconds=total,
        ))

    logger.info("开始烧录字幕: task=%s mode=%s", task_id, mode)
    try:
        run_ffmpeg(
            cmd, _tick,
            error_cls=BurnError,
            not_found_msg=f"找不到 ffmpeg（{settings.ffmpeg_bin}）。请在业务设置/API 配置 FFmpeg 可执行文件，或安装 ffmpeg。",
            cwd=cwd,
            task_id=task_id,
        )
    finally:
        if tmp_burn_path.exists():
            try:
                tmp_burn_path.unlink()
            except OSError:
                pass

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise BurnError("ffmpeg 执行完成但未生成有效输出文件")

    if on_progress is not None and total:
        on_progress(BurnProgress(percent=100.0, processed_seconds=total, total_seconds=total))

    result = BurnResult(
        output_path=out_path,
        mode=mode,
        filesize=out_path.stat().st_size,
    )
    logger.info(
        "烧录完成: task=%s file=%s (%.1f MB)",
        task_id, out_path.name, (result.filesize or 0) / 1024 / 1024,
    )
    return result


if __name__ == "__main__":
    # 独立测试入口：python -m src.core.subtitle_burner <video> <srt> [task_id] [mode]
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if len(sys.argv) < 3:
        print("用法: python -m src.core.subtitle_burner <video> <srt> [task_id] [hard|soft]")
        raise SystemExit(1)

    video = sys.argv[1]
    srt = sys.argv[2]
    task = sys.argv[3] if len(sys.argv) > 3 else "manual_test"
    m = sys.argv[4] if len(sys.argv) > 4 else "hard"

    def _p(p: BurnProgress) -> None:
        if p.percent is not None:
            print(f"\r烧录中 {p.percent:5.1f}%", end="", flush=True)

    res = burn_subtitles(video, srt, task, on_progress=_p, mode=m)
    print(f"\n烧录完成: {res.output_path}（{res.mode}, {(res.filesize or 0)/1024/1024:.1f} MB）")
