"""ffmpeg / ffprobe 公共工具：时长探测 + 带进度执行。

供 ⑤烧录字幕使用（②提取音频暂保留自己的实现，未来可统一到这里）。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def probe_duration(path: Path | str, ffprobe_bin: str = "ffprobe") -> Optional[float]:
    """探测媒体总时长（秒）。失败返回 None。"""
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
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


_subtitles_filter_cache: dict[tuple[object, ...], bool] = {}


def _ffmpeg_cache_key(ffmpeg_bin: str) -> tuple[object, ...]:
    """Return a process-local identity that changes when the binary changes."""
    resolved = shutil.which(ffmpeg_bin) if not Path(ffmpeg_bin).is_file() else ffmpeg_bin
    if resolved is None:
        return (ffmpeg_bin, None)
    try:
        stat = Path(resolved).stat()
    except OSError:
        return (resolved, None)
    return (
        str(Path(resolved).resolve()),
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
    )


def has_subtitles_filter(ffmpeg_bin: str = "ffmpeg") -> bool:
    """Check for libass support, caching independently for each binary identity.

    Args:
        ffmpeg_bin: Executable name or path to inspect.

    Returns:
        True when the executable advertises the ``subtitles`` filter.

    Side effects:
        Runs ``ffmpeg -hide_banner -filters`` on a cache miss. Missing or
        unresponsive executables return False without raising.
    """
    cache_key = _ffmpeg_cache_key(ffmpeg_bin)
    if cache_key in _subtitles_filter_cache:
        return _subtitles_filter_cache[cache_key]
    try:
        out = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=15,
        )
        result = " subtitles " in out.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result = False
    _subtitles_filter_cache[cache_key] = result
    return result


def run_ffmpeg(
    cmd: list[str],
    on_tick: Optional[Callable[[float], None]] = None,
    *,
    error_cls: type[Exception] = RuntimeError,
    not_found_msg: Optional[str] = None,
    cwd: Optional[str] = None,
    task_id: Optional[str] = None,
) -> None:
    """执行 ffmpeg 命令，解析 -progress 输出并回调 on_tick(已处理秒数)。

    要求 cmd 中带有 `-progress pipe:1`。失败抛 error_cls。
    cwd 用于让滤镜参数引用相对文件名，规避路径转义问题。
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=cwd,
        )
    except FileNotFoundError as e:
        raise error_cls(not_found_msg or f"找不到可执行文件: {cmd[0]}") from e

    if task_id:
        try:
            from src.service.runner import register_process
            register_process(task_id, proc)
        except Exception:
            pass

    stderr_lines: deque[str] = deque(maxlen=200)

    def _read_stderr() -> None:
        if proc.stderr is None:
            return
        try:
            if hasattr(proc.stderr, "__iter__"):
                for err_line in proc.stderr:
                    stderr_lines.append(err_line)
            elif hasattr(proc.stderr, "read"):
                content = proc.stderr.read()
                if content:
                    stderr_lines.extend(content.splitlines(keepends=True))
        except Exception:
            pass

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()

    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                line = line.strip()
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key == "out_time_us" and on_tick is not None:
                    try:
                        on_tick(int(value) / 1_000_000)
                    except (ValueError, TypeError):
                        pass

        proc.wait()
        stderr_thread.join(timeout=2.0)
        if proc.returncode != 0:
            stderr_text = "".join(stderr_lines).strip()
            raise error_cls(f"ffmpeg 执行失败（退出码 {proc.returncode}）: {stderr_text}")
    finally:
        if hasattr(proc, "poll") and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
        if proc.stdout and hasattr(proc.stdout, "close"):
            try:
                proc.stdout.close()
            except Exception:
                pass
        if proc.stderr and hasattr(proc.stderr, "close"):
            try:
                proc.stderr.close()
            except Exception:
                pass
        stderr_thread.join(timeout=1.0)
        if task_id:
            try:
                from src.service.runner import unregister_process
                unregister_process(task_id, proc)
            except Exception:
                pass
