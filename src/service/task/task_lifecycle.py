"""任务生命周期业务实现。

本模块不依赖 FastAPI。HTTP 层通过显式传入校验、文件路径和流水线回调，
把请求转换成任务记录和生命周期动作。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable

from src.service.asset_resolver import AssetResolver


class UploadTaskError(RuntimeError):
    """上传任务无法创建时使用的领域错误。"""

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class LifecycleConflictError(RuntimeError):
    """Expected lifecycle state conflict suitable for HTTP 409."""


def require(store: Any, task_id: str) -> Any:
    rec = store.get(task_id)
    if rec is None:
        raise LookupError("任务不存在")
    return rec


def create_url_task(
    *,
    body: Any,
    store: Any,
    engines: Any,
    ensure_supported_translation_languages: Callable[..., None],
    ensure_local_model_ready: Callable[..., None],
    ensure_translation_engine: Callable[..., None],
    task_origin: str = "web",
) -> tuple[Any, bool]:
    """校验并创建 URL 任务；入队由 HTTP 门面在冲突检查后完成。"""

    ensure_supported_translation_languages(
        body.engine, body.needSubtitle, body.sourceLang, body.targetLang
    )
    ensure_local_model_ready(body.model)
    ensure_translation_engine(body.engine, body.needSubtitle, engines)
    return store.create_if_no_recent_active(
        url=body.url,
        source_lang=body.sourceLang,
        target_lang=body.targetLang,
        mode=body.mode,
        burn=body.burn,
        model=body.model,
        engine=body.engine,
        need_subtitle=body.needSubtitle,
        quality=body.quality,
        task_origin="mcp" if task_origin.strip().lower() == "mcp" else "web",
        tts_enabled=body.ttsEnabled,
        tts_voice=body.ttsVoice,
        original_voice_mode=body.originalVoiceMode,
    )


def create_upload_task(
    *,
    file: Any,
    source_lang: str,
    target_lang: str,
    mode: str,
    burn: str,
    model: str,
    engine: str,
    need_subtitle: bool,
    tts_enabled: bool,
    tts_voice: str,
    original_voice_mode: str,
    store: Any,
    engines: Any,
    settings: Any,
    task_dir: Callable[[str], Path],
    probe_duration: Callable[[Path, str], float | None],
    enqueue_pipeline: Callable[[str], None],
    release_lock: Callable[[str], None],
    ensure_supported_translation_languages: Callable[..., None],
    ensure_local_model_ready: Callable[..., None],
    ensure_translation_engine: Callable[..., None],
    upload_error: Callable[..., Exception],
    http_exception_type: type[BaseException],
    source_video_stem: str,
    upload_video_extensions: set[str],
) -> Any:
    """保存上传文件、校验媒体并创建任务。"""

    max_upload_bytes = settings.max_upload_mb * 1024 * 1024
    filename = (file.filename or "").strip()
    ensure_supported_translation_languages(engine, need_subtitle, source_lang, target_lang)
    ensure_translation_engine(engine, need_subtitle, engines)
    ensure_local_model_ready(model)

    if tts_enabled and not need_subtitle:
        raise UploadTaskError(422, "配音需要启用字幕翻译")
    if tts_enabled and not target_lang.lower().startswith(("zh", "en")):
        raise UploadTaskError(422, "Kokoro 配音目前支持中文和英文目标语")
    if tts_enabled and tts_voice not in {"auto", "zf_001", "zm_010", "af_maple", "af_sol"}:
        raise UploadTaskError(422, "不支持的 Kokoro 音色")
    if tts_enabled and tts_voice in {"zf_001", "zm_010"} and not target_lang.lower().startswith("zh"):
        raise UploadTaskError(422, "中文音色只能用于中文目标语")
    if tts_enabled and tts_voice in {"af_maple", "af_sol"} and not target_lang.lower().startswith("en"):
        raise UploadTaskError(422, "英文音色只能用于英文目标语")

    ext = Path(filename).suffix.lower()
    if ext not in upload_video_extensions:
        supported_formats = sorted(upload_video_extensions)
        raise upload_error(
            400,
            code="UNSUPPORTED_FORMAT",
            message=f"不支持的视频格式：{ext or '未知'}（支持 {', '.join(supported_formats)}）",
            limits={"supportedFormats": supported_formats},
            suggestion="请将视频转换为受支持的格式后重新上传。",
        )

    rec = store.create(
        url=filename,
        source_lang=source_lang,
        target_lang=target_lang,
        mode=mode,
        burn=burn,
        model=model,
        engine=engine,
        source_type="upload",
        need_subtitle=need_subtitle,
        title=Path(filename).stem or "上传的视频",
        tts_enabled=tts_enabled,
        tts_voice=tts_voice,
        original_voice_mode=original_voice_mode,
    )

    directory = task_dir(rec.id)
    directory.mkdir(parents=True, exist_ok=True)
    dest_part = directory / f"{source_video_stem}{ext}.part"
    dest = directory / f"{source_video_stem}{ext}"
    try:
        written_bytes = 0
        with dest_part.open("wb") as output:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written_bytes += len(chunk)
                if written_bytes > max_upload_bytes:
                    raise upload_error(
                        413,
                        code="UPLOAD_TOO_LARGE",
                        message=f"上传文件大小超过最大限制 ({settings.max_upload_mb} MB)",
                        limits={"maxMb": settings.max_upload_mb},
                        suggestion="请压缩或切分视频，也可以改用 URL 任务模式。",
                    )
                output.write(chunk)
    except http_exception_type:
        store.delete(rec.id)
        shutil.rmtree(directory, ignore_errors=True)
        raise
    except Exception as exc:
        store.delete(rec.id)
        shutil.rmtree(directory, ignore_errors=True)
        raise UploadTaskError(500, "保存上传文件失败") from exc
    finally:
        file.file.close()

    if not dest_part.exists() or dest_part.stat().st_size == 0:
        store.delete(rec.id)
        shutil.rmtree(directory, ignore_errors=True)
        raise UploadTaskError(400, "上传的视频文件为空")

    duration_sec = probe_duration(dest_part, settings.ffprobe_bin)
    if duration_sec is None:
        store.delete(rec.id)
        shutil.rmtree(directory, ignore_errors=True)
        raise UploadTaskError(400, "无法解析视频时长，请确认文件格式正确且 ffprobe 可用")

    if duration_sec > settings.max_video_minutes * 60:
        store.delete(rec.id)
        shutil.rmtree(directory, ignore_errors=True)
        release_lock(rec.id)
        raise upload_error(
            400,
            code="UPLOAD_DURATION_EXCEEDED",
            message=(
                f"视频时长 ({duration_sec / 60:.1f} 分钟) 超过最大限制 "
                f"({settings.max_video_minutes} 分钟)"
            ),
            limits={
                "maxMinutes": settings.max_video_minutes,
                "durationMinutes": round(duration_sec / 60, 1),
            },
            suggestion="请裁剪或切分视频后重新上传。",
        )

    dest_part.replace(dest)
    enqueue_pipeline(rec.id)
    return store.get(rec.id) or rec


def list_tasks(
    *,
    store: Any,
    to_out: Callable[..., Any],
    offset: int = 0,
    limit: int = 50,
    before_id: str | None = None,
    after_id: str | None = None,
    origin: str | None = None,
) -> list[Any]:
    return [
        to_out(r)
        for r in store.list(
            limit=limit,
            offset=offset,
            before_id=before_id,
            after_id=after_id,
            task_origin=origin,
        )
    ]


def get_task(*, store: Any, task_id: str, to_out: Callable[..., Any]) -> Any:
    return to_out(require(store, task_id))


def delete_task(
    *,
    store: Any,
    task_id: str,
    task_dir: Callable[[str], Path],
    cleanup_partial_artifacts: Callable[[str], None],
    release_lock: Callable[[str], None],
) -> None:
    rec = require(store, task_id)
    if rec.is_cancelling:
        raise LifecycleConflictError("任务正在取消，请稍后再试")
    if rec.status not in {"SUCCESS", "FAILED", "CANCELLED"}:
        raise LifecycleConflictError("任务运行中，请先等待或调用取消接口")
    cleanup_partial_artifacts(task_id)
    AssetResolver.cleanup_cancelled_artifacts(
        task_id, current_step=rec.current_step, source_type=rec.source_type
    )
    store.delete(task_id)
    shutil.rmtree(task_dir(task_id), ignore_errors=True)
    release_lock(task_id)


def cancel_task(
    *,
    store: Any,
    task_id: str,
    cancel_pipeline: Callable[[str], None],
    to_out: Callable[..., Any],
) -> Any:
    rec = require(store, task_id)
    if rec.status in {"SUCCESS", "FAILED", "CANCELLED"}:
        raise LifecycleConflictError("任务非运行状态，无法取消")
    cancel_pipeline(task_id)
    return to_out(store.get(task_id) or rec)


def retry_task(
    *,
    store: Any,
    task_id: str,
    enqueue_pipeline: Callable[[str], None],
    to_out: Callable[..., Any],
) -> Any:
    rec = require(store, task_id)
    if rec.status not in {"FAILED", "CANCELLED"}:
        raise LifecycleConflictError("只有失败或已取消任务可以重试")
    updated = store.update(
        task_id, status="PENDING", progress=0, current_step=None, error=None
    )
    enqueue_pipeline(task_id)
    return to_out(updated)
