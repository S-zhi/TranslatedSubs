"""Optional CPU vocal separation for subtitle recognition.

The model is intentionally an external dependency.  This keeps the default
installation small and lets deployments choose PyTorch Demucs or an ONNX
wrapper without changing the pipeline contract.
"""

from __future__ import annotations

import logging
import json
import hashlib
import os
import shutil
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

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
    threads: int | None = None,
    register_process: Callable[[Any], None] | None = None,
    unregister_process: Callable[[Any], None] | None = None,
) -> tuple[int, str, str]:
    """运行外部模型并接入 runner 的进程注册/取消机制。

    ``subprocess.run`` 无法在任务取消时终止 Demucs；使用 Popen 轮询让取消
    请求最多等待半秒即可生效，同时仍对没有 runner 环境的单元测试安全降级。
    """
    try:
        env = os.environ.copy()
        effective_threads = str(
            threads if threads is not None else getattr(settings, "vocal_separation_threads", 1)
        )
        env["OMP_NUM_THREADS"] = effective_threads
        env["MKL_NUM_THREADS"] = effective_threads
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    except FileNotFoundError as exc:
        raise VocalSeparationError(
            "未找到 Demucs。请安装 demucs，或在设置页/API 关闭人声分离。"
        ) from exc
    try:
        if register_process is not None:
            try:
                register_process(proc)
            except Exception:
                pass
        else:
            try:
                from src.service.runner import register_process as register_runner_process
                register_runner_process(task_id, proc)
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
        if unregister_process is not None:
            try:
                unregister_process(proc)
            except Exception:
                pass
        else:
            try:
                from src.service.runner import unregister_process as unregister_runner_process
                unregister_runner_process(task_id, proc)
            except Exception:
                pass


@dataclass(frozen=True)
class SeparationOutputs:
    vocal_path: Path | None = None
    background_path: Path | None = None


def _separation_config(
    *,
    backend: str | None = None,
    model: str | None = None,
    threads: int | None = None,
    command: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Use explicit task snapshots when supplied; keep legacy defaults otherwise."""
    result = {
        "backend": backend if backend is not None else getattr(settings, "vocal_separation_backend", "demucs"),
        "model": model if model is not None else getattr(settings, "vocal_separation_model", "htdemucs"),
        "threads": threads if threads is not None else getattr(settings, "vocal_separation_threads", 1),
        "command": command if command is not None else getattr(settings, "vocal_separation_command", "python -m demucs.separate"),
        "timeout_seconds": timeout_seconds if timeout_seconds is not None else getattr(settings, "vocal_separation_timeout", 60),
    }
    if result["backend"] != "demucs":
        raise VocalSeparationError(f"不支持的人声分离 backend: {result['backend']}")
    if not isinstance(result["model"], str) or not result["model"].strip():
        raise VocalSeparationError("人声分离 model 无效")
    for name in ("threads", "timeout_seconds"):
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise VocalSeparationError(f"人声分离 {name} 必须是正整数")
    if not isinstance(result["command"], str) or not result["command"].strip():
        raise VocalSeparationError("人声分离 command 无效")
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _separation_metadata_path(task_dir: Path, purpose: str) -> Path:
    if purpose not in {"separate.asr", "separate.dubbing"}:
        raise VocalSeparationError("人声分离 purpose 无效")
    suffix = "asr" if purpose == "separate.asr" else "dubbing"
    return task_dir / f"separation.{suffix}.meta.json"


def cached_separation_is_current(
    audio_path: Path | str,
    task_id: str,
    *,
    purpose: str,
    vocal_path: Path | None,
    background_path: Path | None,
    config: Mapping[str, Any],
    task_dir: Path | None = None,
) -> bool:
    """Require matching input bytes, purpose, effective config, and requested stems."""
    source = Path(audio_path)
    if not source.is_file() or (vocal_path is None and background_path is None):
        return False
    task_dir = Path(task_dir) if task_dir is not None else ensure_task_dir(task_id)
    outputs = [path for path in (vocal_path, background_path) if path is not None]
    if any(not path.is_file() or path.stat().st_size <= 0 for path in outputs):
        return False
    try:
        config_json = json.dumps(dict(config), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        metadata = json.loads(_separation_metadata_path(task_dir, purpose).read_text(encoding="utf-8"))
        return (
            metadata.get("schema_version") == 1
            and metadata.get("stage") == "vocal_separate"
            and metadata.get("operation") == "vocal_separate"
            and metadata.get("purpose") == purpose
            and metadata.get("plugin_id") == "builtin.vocal_demucs"
            and metadata.get("implementation_version") == "1"
            and metadata.get("config_sha256") == hashlib.sha256(config_json.encode("utf-8")).hexdigest()
            and metadata.get("input_sha256") == _file_sha256(source)
            and metadata.get("output_names") == sorted(path.name for path in outputs)
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _separate_stems_impl(
    audio_path: Path | str,
    task_id: str,
    *,
    purpose: str,
    vocal_path: Path | None,
    background_path: Path | None,
    config: Mapping[str, Any],
    invocation_id: str,
    task_dir: Path | None = None,
    cancel_check: Optional[Callable[[], None]] = None,
    register_process: Callable[[Any], None] | None = None,
    unregister_process: Callable[[Any], None] | None = None,
) -> SeparationOutputs:
    source = Path(audio_path)
    if not source.is_file():
        raise VocalSeparationError(f"输入音频不存在: {source}")
    if (vocal_path is None and background_path is None):
        raise VocalSeparationError("至少需要请求一个分离 stem")
    task_dir = (Path(task_dir) if task_dir is not None else ensure_task_dir(task_id)).resolve()
    outputs = [path.resolve() for path in (vocal_path, background_path) if path is not None]
    if any(task_dir not in path.parents for path in outputs) or len(set(outputs)) != len(outputs):
        raise VocalSeparationError("分离输出路径无效")
    if purpose == "separate.asr":
        if vocal_path is None or background_path is not None or vocal_path.name != "vocal.wav":
            raise VocalSeparationError("ASR 分离只支持输出 vocal.wav")
    elif purpose == "separate.dubbing":
        if background_path is None or background_path.name != "background.wav":
            raise VocalSeparationError("配音分离必须输出 background.wav")
        if vocal_path is not None and vocal_path.name != "vocal_stem.wav":
            raise VocalSeparationError("配音人声 stem 路径无效")
    else:
        raise VocalSeparationError("人声分离 purpose 无效")

    cfg = _separation_config(
        backend=config.get("backend"),
        model=config.get("model"),
        threads=config.get("threads"),
        command=config.get("command"),
        timeout_seconds=config.get("timeout_seconds"),
    )
    source_hash = _file_sha256(source)
    config_json = json.dumps(cfg, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    config_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()

    safe_invocation = "".join(ch for ch in invocation_id if ch.isalnum() or ch in "-_:")
    if not safe_invocation:
        safe_invocation = uuid.uuid4().hex
    work_dir = task_dir / ".vocal-separation" / purpose / safe_invocation
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    staged_outputs: list[tuple[Path, Path]] = []
    temporary_paths: list[Path] = []
    try:
        command = shlex.split(cfg["command"])
        command += [
            "-n", str(cfg["model"]), "--two-stems=vocals", "--device", "cpu",
            "--jobs", str(cfg["threads"]), "--shifts", "0", "-o", str(work_dir), str(source),
        ]
        logger.info("开始 CPU 人声分离: task=%s purpose=%s backend=%s", task_id, purpose, cfg["backend"])
        return_code, stdout, stderr = _run_external(
            command,
            timeout=cfg["timeout_seconds"],
            task_id=task_id,
            cancel_check=cancel_check,
            threads=cfg["threads"],
            register_process=register_process,
            unregister_process=unregister_process,
        )
        if return_code != 0:
            detail = (stderr or stdout or "未知错误").strip()[-1000:]
            raise VocalSeparationError(f"Demucs 执行失败: {detail}")
        vocal_candidates = sorted(work_dir.rglob("vocals.wav"), key=lambda path: len(path.parts))
        background_candidates = sorted(work_dir.rglob("no_vocals.wav"), key=lambda path: len(path.parts))
        if vocal_path is not None and (not vocal_candidates or vocal_candidates[0].stat().st_size <= 0):
            raise VocalSeparationError("Demucs 执行完成但未生成 vocals.wav")
        if background_path is not None and (
            not background_candidates or background_candidates[0].stat().st_size <= 0
        ):
            raise VocalSeparationError("Demucs 执行完成但未生成 no_vocals.wav")

        if vocal_path is not None:
            staged = task_dir / f".{vocal_path.name}.{safe_invocation}.tmp"
            temporary_paths.append(staged)
            staged.unlink(missing_ok=True)
            if purpose == "separate.asr":
                normalize = [
                    settings.ffmpeg_bin, "-y", "-i", str(vocal_candidates[0]), "-vn",
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", str(staged),
                ]
                code, out, err = _run_external(
                    normalize, timeout=cfg["timeout_seconds"], task_id=task_id,
                    cancel_check=cancel_check, threads=cfg["threads"],
                    register_process=register_process, unregister_process=unregister_process,
                )
                if code != 0 or not staged.is_file() or staged.stat().st_size <= 0:
                    detail = (err or out or "未知错误").strip()[-1000:]
                    raise VocalSeparationError(f"vocals 音频规范化失败: {detail}")
            else:
                shutil.copy2(vocal_candidates[0], staged)
            staged_outputs.append((staged, vocal_path))

        if background_path is not None:
            staged = task_dir / f".{background_path.name}.{safe_invocation}.tmp"
            temporary_paths.append(staged)
            staged.unlink(missing_ok=True)
            shutil.copy2(background_candidates[0], staged)
            staged_outputs.append((staged, background_path))

        if _file_sha256(source) != source_hash:
            raise VocalSeparationError("输入音频在分离期间发生变化")
        if cancel_check is not None:
            cancel_check()
        for staged, output in staged_outputs:
            if not staged.is_file() or staged.stat().st_size <= 0:
                raise VocalSeparationError("分离输出 stem 为空")
            os.replace(staged, output)

        meta = {
            "schema_version": 1,
            "stage": "vocal_separate",
            "operation": "vocal_separate",
            "purpose": purpose,
            "plugin_id": "builtin.vocal_demucs",
            "implementation_version": "1",
            "config_sha256": config_hash,
            "input_sha256": source_hash,
            "output_names": sorted(path.name for path in outputs),
        }
        metadata_path = _separation_metadata_path(task_dir, purpose)
        staged_metadata = task_dir / f".{metadata_path.name}.{safe_invocation}.tmp"
        temporary_paths.append(staged_metadata)
        staged_metadata.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
        os.replace(staged_metadata, metadata_path)
        return SeparationOutputs(
            vocal_path=vocal_path.resolve() if vocal_path is not None else None,
            background_path=background_path.resolve() if background_path is not None else None,
        )
    finally:
        for staged in temporary_paths:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            shutil.rmtree(work_dir)
        except OSError:
            logger.warning("清理 Demucs 临时目录失败: %s", work_dir, exc_info=True)


def separate_stems(
    audio_path: Path | str,
    task_id: str,
    *,
    purpose: str,
    vocal_path: Path | None,
    background_path: Path | None,
    backend: str,
    model: str,
    threads: int,
    command: str,
    timeout_seconds: int,
    invocation_id: str,
    task_dir: Path | None = None,
    cancel_check: Optional[Callable[[], None]] = None,
    register_process: Callable[[Any], None] | None = None,
    unregister_process: Callable[[Any], None] | None = None,
) -> SeparationOutputs:
    """Execute with the caller's immutable config and explicitly requested stems."""
    config = _separation_config(
        backend=backend, model=model, threads=threads,
        command=command, timeout_seconds=timeout_seconds,
    )
    effective_task_dir = Path(task_dir) if task_dir is not None else ensure_task_dir(task_id)
    if cached_separation_is_current(
        audio_path, task_id, purpose=purpose, vocal_path=vocal_path,
        background_path=background_path, config=config, task_dir=effective_task_dir,
    ):
        return SeparationOutputs(
            vocal_path=vocal_path.resolve() if vocal_path is not None else None,
            background_path=background_path.resolve() if background_path is not None else None,
        )
    while not _SEPARATION_SLOT.acquire(timeout=0.5):
        if cancel_check is not None:
            cancel_check()
    try:
        if cancel_check is not None:
            cancel_check()
        if cached_separation_is_current(
            audio_path, task_id, purpose=purpose, vocal_path=vocal_path,
            background_path=background_path, config=config, task_dir=effective_task_dir,
        ):
            return SeparationOutputs(
                vocal_path=vocal_path.resolve() if vocal_path is not None else None,
                background_path=background_path.resolve() if background_path is not None else None,
            )
        return _separate_stems_impl(
            audio_path,
            task_id,
            purpose=purpose,
            vocal_path=vocal_path,
            background_path=background_path,
            config=config,
            invocation_id=invocation_id,
            task_dir=effective_task_dir,
            cancel_check=cancel_check,
            register_process=register_process,
            unregister_process=unregister_process,
        )
    finally:
        _SEPARATION_SLOT.release()


def _separate_vocals_impl(
    audio_path: Path | str,
    task_id: str,
    *,
    keep_background: bool = False,
    cancel_check: Optional[Callable[[], None]] = None,
) -> Path:
    """Legacy wrapper retaining its path return and global default behavior."""
    task_dir = ensure_task_dir(task_id)
    purpose = "separate.dubbing" if keep_background else "separate.asr"
    vocal_path = task_dir / ("vocal_stem.wav" if keep_background else "vocal.wav")
    background_path = task_dir / "background.wav" if keep_background else None
    config = _separation_config()
    result = separate_stems(
        audio_path,
        task_id,
        purpose=purpose,
        vocal_path=vocal_path,
        background_path=background_path,
        backend=config["backend"],
        model=config["model"],
        threads=config["threads"],
        command=config["command"],
        timeout_seconds=config["timeout_seconds"],
        invocation_id=uuid.uuid4().hex,
        cancel_check=cancel_check,
    )
    if not keep_background and result.vocal_path is not None:
        source = Path(audio_path)
        metadata = task_dir / "vocal.meta.json"
        metadata.write_text(json.dumps({
            "source": str(source.resolve()),
            "source_size": source.stat().st_size,
            "source_mtime_ns": source.stat().st_mtime_ns,
            "backend": config["backend"],
            "model": config["model"],
            "threads": config["threads"],
            "command": config["command"],
        }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return result.vocal_path
    if result.vocal_path is not None:
        return result.vocal_path
    if result.background_path is not None:
        return result.background_path
    raise VocalSeparationError("Demucs 未生成所需 stem")


def separate_vocals(
    audio_path: Path | str,
    task_id: str,
    *,
    cancel_check: Optional[Callable[[], None]] = None,
) -> Path:
    """串行运行 CPU 分离，避免多个任务同时加载 Demucs 造成资源争用。"""
    return _separate_vocals_impl(audio_path, task_id, cancel_check=cancel_check)


def separate_background(
    audio_path: Path | str,
    task_id: str,
    *,
    cancel_check: Optional[Callable[[], None]] = None,
) -> Path:
    """用 Demucs two-stems 提取背景音，不触碰 ASR 的 vocal.wav。"""
    task_dir = ensure_task_dir(task_id)
    config = _separation_config()
    result = separate_stems(
        audio_path,
        task_id,
        purpose="separate.dubbing",
        vocal_path=task_dir / "vocal_stem.wav",
        background_path=task_dir / "background.wav",
        backend=config["backend"],
        model=config["model"],
        threads=config["threads"],
        command=config["command"],
        timeout_seconds=config["timeout_seconds"],
        invocation_id=uuid.uuid4().hex,
        cancel_check=cancel_check,
    )
    if result.background_path is None:
        raise VocalSeparationError("Demucs 未生成背景音 stem")
    # Preserve the legacy cache marker for any caller still using the old helper.
    source = Path(audio_path)
    metadata = task_dir / "background.meta.json"
    metadata.write_text(json.dumps({
        "source": str(source.resolve()), "source_size": source.stat().st_size,
        "source_mtime_ns": source.stat().st_mtime_ns,
        "backend": config["backend"], "model": config["model"],
        "threads": config["threads"], "command": config["command"],
    }, sort_keys=True), encoding="utf-8")
    return result.background_path
