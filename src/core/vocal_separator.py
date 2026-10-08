"""Optional CPU vocal separation for subtitle recognition.

The model is intentionally an external dependency.  This keeps the default
installation small and lets deployments choose PyTorch Demucs or an ONNX
wrapper without changing the pipeline contract.
"""

from __future__ import annotations

import logging
import json
import os
import shutil
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from src.config import ensure_task_dir, settings

logger = logging.getLogger(__name__)
_SEPARATION_SLOT = threading.BoundedSemaphore(1)


class VocalSeparationError(RuntimeError):
    """人声分离失败或未生成 vocals 产物。"""

    code = "vocal_separation_error"


class VocalSeparationCancelledError(VocalSeparationError):
    """人声分离进程被任务取消。"""


def cached_vocals_are_current(audio_path: Path | str | None, task_id: str) -> bool:
    """判断缓存 vocals 是否由当前输入音频和分离配置生成。

    没有 metadata 的缓存无法确认输入音频和模型配置，必须重新生成，避免
    重试时误用另一段音频的人声。
    """
    if audio_path is None:
        return False
    audio = Path(audio_path)
    output = ensure_task_dir(task_id) / "vocal.wav"
    if not output.is_file() or output.stat().st_size == 0:
        return False
    metadata = output.parent / "vocal.meta.json"
    if not metadata.is_file():
        return False
    try:
        data = json.loads(metadata.read_text(encoding="utf-8"))
        stat = audio.stat()
        return (
            data.get("source") == str(audio.resolve())
            and int(data.get("source_size", -1)) == stat.st_size
            and int(data.get("source_mtime_ns", -1)) == stat.st_mtime_ns
            and str(data.get("backend", "")).strip().lower() == getattr(settings, "vocal_separation_backend", "demucs")
            and str(data.get("model", "htdemucs")).strip().lower() == getattr(settings, "vocal_separation_model", "htdemucs")
            and int(data.get("threads", 1)) == int(getattr(settings, "vocal_separation_threads", 1))
            and data.get("command") == getattr(settings, "vocal_separation_command", "python -m demucs.separate")
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _run_external(
    cmd: list[str],
    *,
    timeout: float,
    task_id: str,
    cancel_check: Optional[Callable[[], None]] = None,
) -> tuple[int, str, str]:
    """运行外部模型并接入 runner 的进程注册/取消机制。

    ``subprocess.run`` 无法在任务取消时终止 Demucs；使用 Popen 轮询让取消
    请求最多等待半秒即可生效，同时仍对没有 runner 环境的单元测试安全降级。
    """
    try:
        env = os.environ.copy()
        threads = str(getattr(settings, "vocal_separation_threads", 1))
        env["OMP_NUM_THREADS"] = threads
        env["MKL_NUM_THREADS"] = threads
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    except FileNotFoundError as exc:
        raise VocalSeparationError(
            "未找到 Demucs。请安装 demucs，或关闭 SUBTRANS_VOCAL_SEPARATION。"
        ) from exc
    try:
        try:
            from src.service.runner import register_process
            register_process(task_id, proc)
        except Exception:
            pass
        started = time.monotonic()
        while True:
            if cancel_check is not None:
                try:
                    cancel_check()
                except Exception as exc:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    raise VocalSeparationCancelledError("任务已被用户取消") from exc
            if time.monotonic() - started > timeout:
                try:
                    proc.terminate()
                except Exception:
                    pass
                raise VocalSeparationError("人声分离超时")
            try:
                stdout, stderr = proc.communicate(timeout=0.5)
                return proc.returncode or 0, stdout or "", stderr or ""
            except subprocess.TimeoutExpired:
                continue
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
        except Exception:
            pass
        try:
            from src.service.runner import unregister_process
            unregister_process(task_id, proc)
        except Exception:
            pass


def _separate_vocals_impl(
    audio_path: Path | str,
    task_id: str,
    *,
    keep_background: bool = False,
    cancel_check: Optional[Callable[[], None]] = None,
) -> Path:
    """用 CPU Demucs 二分模式抽取人声，并输出 16 kHz 单声道 WAV。"""
    source = Path(audio_path)
    if not source.is_file():
        raise VocalSeparationError(f"输入音频不存在: {source}")
    if settings.vocal_separation_backend != "demucs":
        raise VocalSeparationError(
            f"不支持的人声分离 backend: {settings.vocal_separation_backend}"
        )

    task_dir = ensure_task_dir(task_id)
    work_dir = task_dir / "vocal-separation"
    work_dir.mkdir(parents=True, exist_ok=True)
    model = getattr(settings, "vocal_separation_model", "htdemucs")
    threads = max(1, int(getattr(settings, "vocal_separation_threads", 1)))
    cmd = shlex.split(settings.vocal_separation_command)
    cmd += [
        "-n", str(model),
        "--two-stems=vocals",
        "--device", "cpu",
        "--jobs", str(threads),
        "--shifts", "0",
        "-o", str(work_dir),
        str(source),
    ]
    logger.info("开始 CPU 人声分离: task=%s backend=%s", task_id, settings.vocal_separation_backend)
    return_code, stdout, stderr = _run_external(
        cmd,
        timeout=settings.vocal_separation_timeout,
        task_id=task_id,
        cancel_check=cancel_check,
    )
    if return_code != 0:
        detail = (stderr or stdout or "未知错误").strip()[-1000:]
        raise VocalSeparationError(f"Demucs 执行失败: {detail}")

    candidates = sorted(work_dir.rglob("vocals.wav"), key=lambda p: len(p.parts))
    if not candidates:
        raise VocalSeparationError("Demucs 执行完成但未生成 vocals.wav")

    output = task_dir / "vocal.wav"
    backgrounds = sorted(work_dir.rglob("no_vocals.wav"), key=lambda p: len(p.parts))
    if keep_background and backgrounds:
        shutil.copy2(backgrounds[0], task_dir / "background.wav")
        shutil.copy2(candidates[0], task_dir / "vocal_stem.wav")
    temporary_output = task_dir / ".vocal.wav.tmp"
    try:
        temporary_output.unlink()
    except FileNotFoundError:
        pass
    normalize_cmd = [
        settings.ffmpeg_bin, "-y", "-i", str(candidates[0]), "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", str(temporary_output),
    ]
    return_code, stdout, stderr = _run_external(
        normalize_cmd, timeout=120, task_id=task_id, cancel_check=cancel_check
    )
    if return_code != 0 or not temporary_output.is_file() or temporary_output.stat().st_size == 0:
        try:
            temporary_output.unlink()
        except OSError:
            pass
        detail = (stderr or stdout or "未知错误").strip()[-1000:]
        raise VocalSeparationError(f"vocals 音频规范化失败: {detail}")
    os.replace(temporary_output, output)
    # Demucs writes full stems into a temporary tree; only the normalized vocal
    # file is consumed by Whisper, so discard the potentially large tree after
    # the atomic output is in place.
    try:
        shutil.rmtree(work_dir)
    except OSError:
        logger.warning("清理 Demucs 临时目录失败: %s", work_dir, exc_info=True)
    metadata = task_dir / "vocal.meta.json"
    metadata.write_text(
        json.dumps({
            "source": str(source.resolve()),
            "source_size": source.stat().st_size,
            "source_mtime_ns": source.stat().st_mtime_ns,
            "backend": settings.vocal_separation_backend,
            "model": model,
            "threads": threads,
            "command": settings.vocal_separation_command,
        }, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    logger.info("人声分离完成: task=%s output=%s", task_id, output)
    return output


def separate_vocals(
    audio_path: Path | str,
    task_id: str,
    *,
    cancel_check: Optional[Callable[[], None]] = None,
) -> Path:
    """串行运行 CPU 分离，避免多个任务同时加载 Demucs 造成资源争用。"""
    while not _SEPARATION_SLOT.acquire(timeout=0.5):
        if cancel_check is not None:
            cancel_check()
    try:
        return _separate_vocals_impl(audio_path, task_id, cancel_check=cancel_check)
    finally:
        _SEPARATION_SLOT.release()


def separate_background(
    audio_path: Path | str,
    task_id: str,
    *,
    cancel_check: Optional[Callable[[], None]] = None,
) -> Path:
    """用 Demucs two-stems 提取背景音，供配音时保留音乐和环境声。"""
    output = ensure_task_dir(task_id) / "background.wav"
    metadata = output.parent / "background.meta.json"
    source = Path(audio_path)
    vocal_stem = output.parent / "vocal_stem.wav"
    if (
        output.is_file() and output.stat().st_size
        and vocal_stem.is_file() and vocal_stem.stat().st_size
        and metadata.is_file()
    ):
        try:
            cached = json.loads(metadata.read_text(encoding="utf-8"))
            st = source.stat()
            if (
                cached.get("source") == str(source.resolve())
                and cached.get("source_size") == st.st_size
                and cached.get("source_mtime_ns") == st.st_mtime_ns
                and cached.get("backend") == getattr(settings, "vocal_separation_backend", "demucs")
                and cached.get("model") == getattr(settings, "vocal_separation_model", "htdemucs")
                and cached.get("threads") == int(getattr(settings, "vocal_separation_threads", 1))
                and cached.get("command") == getattr(
                    settings, "vocal_separation_command", "python -m demucs.separate"
                )
            ):
                return output
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    while not _SEPARATION_SLOT.acquire(timeout=0.5):
        if cancel_check is not None:
            cancel_check()
    try:
        _separate_vocals_impl(audio_path, task_id, keep_background=True, cancel_check=cancel_check)
        if not output.is_file() or not output.stat().st_size:
            raise VocalSeparationError("Demucs 未生成背景音 stem")
        st = source.stat()
        metadata.write_text(json.dumps({
            "source": str(source.resolve()), "source_size": st.st_size,
            "source_mtime_ns": st.st_mtime_ns,
            "backend": getattr(settings, "vocal_separation_backend", "demucs"),
            "model": getattr(settings, "vocal_separation_model", "htdemucs"),
            "threads": int(getattr(settings, "vocal_separation_threads", 1)),
            "command": getattr(settings, "vocal_separation_command", "python -m demucs.separate"),
        }, sort_keys=True), encoding="utf-8")
        return output
    finally:
        _SEPARATION_SLOT.release()
