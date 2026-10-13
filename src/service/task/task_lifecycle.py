"""任务生命周期业务实现。

本模块不依赖 FastAPI。HTTP 层通过显式传入校验、文件路径和流水线回调，
把请求转换成任务记录和生命周期动作。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable, List, Literal, Optional

from fastapi import Depends, File, Form, Header, HTTPException, Query, UploadFile

from src.config import SOURCE_VIDEO_STEM
from src.handler.deps import get_probe_store, get_store, get_translation_engine_store, require_api_token
from src.handler.schemas import (
    ErrorDetail,
    ProbeBatchStatusOut,
    ProbeRecordOut,
    ProbeRecordsClearOut,
    TaskCreate,
    TaskOut,
    TaskProbeIn,
    TaskProbeOut,
    YtDlpEnvInfo,
    _probe_record_to_out,
    to_out,
)
from src.service.probe_batch import probe_batch_manager
from src.store import DEFAULT_TRANSLATION_ENGINE_ID, ProbeStore, TaskStore, TranslationEngineStore
from src.service.asset_resolver import AssetResolver
from src.service.task.task import task_error_boundary


from fastapi import APIRouter

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


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
    resolve_runtime: Callable[..., Any] | None = None,
    task_origin: str = "web",
) -> tuple[Any, bool]:
    """校验并创建 URL 任务；入队由 HTTP 门面在冲突检查后完成。"""

    ensure_supported_translation_languages(
        body.engine, body.needSubtitle, body.sourceLang, body.targetLang
    )
    if resolve_runtime is not None:
        runtime = resolve_runtime(
            engine=body.engine,
            need_subtitle=body.needSubtitle,
            request_model=body.model,
            source_lang=body.sourceLang,
            target_lang=body.targetLang,
            tts_enabled=body.ttsEnabled,
            tts_voice=body.ttsVoice,
            engines=engines,
            ensure_local_model_ready=ensure_local_model_ready,
        )
        runtime_config_json = json.dumps(runtime.snapshot, ensure_ascii=False, separators=(",", ":"))
    else:
        ensure_local_model_ready(body.model)
        ensure_translation_engine(body.engine, body.needSubtitle, engines)
        runtime_config_json = None
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
        runtime_config_json=runtime_config_json,
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
    resolve_runtime: Callable[..., Any] | None = None,
    http_exception_type: type[BaseException],
    source_video_stem: str,
    upload_video_extensions: set[str],
) -> Any:
    """保存上传文件、校验媒体并创建任务。"""

    max_upload_bytes = settings.max_upload_mb * 1024 * 1024
    filename = (file.filename or "").strip()
    ensure_supported_translation_languages(engine, need_subtitle, source_lang, target_lang)
    if resolve_runtime is not None:
        runtime = resolve_runtime(
            engine=engine,
            need_subtitle=need_subtitle,
            request_model=model,
            source_lang=source_lang,
            target_lang=target_lang,
            tts_enabled=tts_enabled,
            tts_voice=tts_voice,
            engines=engines,
            ensure_local_model_ready=ensure_local_model_ready,
        )
        runtime_config_json = json.dumps(runtime.snapshot, ensure_ascii=False, separators=(",", ":"))
    else:
        ensure_translation_engine(engine, need_subtitle, engines)
        ensure_local_model_ready(model)
        runtime_config_json = None

    if tts_enabled and not need_subtitle:
        raise UploadTaskError(422, "配音需要启用字幕翻译")
    if tts_enabled and not target_lang.lower().startswith(("zh", "en")):
        raise UploadTaskError(422, "Kokoro 配音目前支持中文和英文目标语")
    effective_tts_voice = tts_voice
    if runtime_config_json:
        try:
            effective_tts_voice = str(json.loads(runtime_config_json).get("tts", {}).get("voice") or tts_voice)
        except (TypeError, ValueError):
            effective_tts_voice = tts_voice
    if tts_enabled and effective_tts_voice not in {"auto", "zf_001", "zm_010", "af_maple", "af_sol"}:
        raise UploadTaskError(422, "不支持的 Kokoro 音色")
    if tts_enabled and effective_tts_voice in {"zf_001", "zm_010"} and not target_lang.lower().startswith("zh"):
        raise UploadTaskError(422, "中文音色只能用于中文目标语")
    if tts_enabled and effective_tts_voice in {"af_maple", "af_sol"} and not target_lang.lower().startswith("en"):
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
        runtime_config_json=runtime_config_json,
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


# HTTP endpoints are kept in this module so the lifecycle router owns the
# complete route registration.  Mutable application dependencies are resolved
# through handler.tasks at call time for backwards compatible monkeypatching.


@router.post("", response_model=TaskOut, status_code=201, dependencies=[Depends(require_api_token)])
def create_task_route(
    body: TaskCreate,
    store: TaskStore = Depends(get_store),
    engines: TranslationEngineStore = Depends(get_translation_engine_store),
    task_origin: str = Header("web", alias="X-Task-Origin"),
) -> TaskOut:
    from src.handler import tasks as compat

    rec, created = compat._task_lifecycle.create_url_task(
        body=body,
        store=store,
        engines=engines,
        ensure_supported_translation_languages=compat._ensure_supported_translation_languages,
        ensure_local_model_ready=compat._ensure_local_model_ready,
        ensure_translation_engine=compat._ensure_translation_engine,
        resolve_runtime=compat.resolve_runtime,
        task_origin=task_origin,
    )
    if not created:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TASK_ALREADY_RUNNING",
                "message": "该 URL 已有任务正在处理，请复用现有 task_id",
                "taskId": rec.id,
            },
        )
    compat.enqueue_pipeline(rec.id)
    return compat.to_out(rec)


@router.post("/upload", response_model=TaskOut, status_code=201, dependencies=[Depends(require_api_token)])
def create_upload_task_route(
    file: UploadFile = File(..., description="本地视频文件"),
    sourceLang: str = Form("en", min_length=1),
    targetLang: str = Form("zh-CN", min_length=1),
    mode: Literal["mono", "bilingual"] = Form("mono"),
    burn: Literal["hard", "soft"] = Form("hard"),
    model: str = Form("local:tiny", min_length=1),
    engine: str = Form(DEFAULT_TRANSLATION_ENGINE_ID, min_length=1),
    needSubtitle: bool = Form(True),
    ttsEnabled: bool = Form(False),
    ttsVoice: str = Form("auto"),
    originalVoiceMode: Literal["keep", "lower", "replace"] = Form("keep"),
    store: TaskStore = Depends(get_store),
    engines: TranslationEngineStore = Depends(get_translation_engine_store),
) -> TaskOut:
    from src.handler import tasks as compat

    try:
        rec = compat._task_lifecycle.create_upload_task(
            file=file,
            source_lang=sourceLang,
            target_lang=targetLang,
            mode=mode,
            burn=burn,
            model=model,
            engine=engine,
            need_subtitle=needSubtitle,
            tts_enabled=ttsEnabled,
            tts_voice=ttsVoice,
            original_voice_mode=originalVoiceMode,
            store=store,
            engines=engines,
            settings=compat.settings,
            task_dir=compat.task_dir,
            probe_duration=compat.probe_duration,
            enqueue_pipeline=compat.enqueue_pipeline,
            release_lock=compat.release_lock,
            ensure_supported_translation_languages=compat._ensure_supported_translation_languages,
            ensure_local_model_ready=compat._ensure_local_model_ready,
            ensure_translation_engine=compat._ensure_translation_engine,
            upload_error=compat._upload_error,
            resolve_runtime=compat.resolve_runtime,
            http_exception_type=HTTPException,
            source_video_stem=SOURCE_VIDEO_STEM,
            upload_video_extensions=compat._UPLOAD_VIDEO_EXTS,
        )
    except compat._task_lifecycle.UploadTaskError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return compat.to_out(rec)


@router.get("", response_model=List[TaskOut], dependencies=[Depends(require_api_token)])
def list_tasks_route(
    offset: int = Query(0, ge=0, description="跳过前 N 条记录"),
    limit: int = Query(50, ge=1, le=200, description="单页最大记录数，取值范围 1 到 200，默认 50"),
    before_id: Optional[str] = Query(None, description="游标：仅返回 ID 早于该任务的记录"),
    after_id: Optional[str] = Query(None, description="游标：仅返回 ID 晚于该任务的记录"),
    origin: Optional[Literal["web", "mcp"]] = Query(None, description="任务来源"),
    store: TaskStore = Depends(get_store),
) -> List[TaskOut]:
    from src.handler import tasks as compat

    return compat._task_lifecycle.list_tasks(
        store=store,
        to_out=compat.to_out,
        offset=offset,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
        origin=origin,
    )


@router.get("/{task_id}", response_model=TaskOut, dependencies=[Depends(require_api_token)])
def get_task_route(task_id: str, store: TaskStore = Depends(get_store)) -> TaskOut:
    from src.handler import tasks as compat

    try:
        return compat._task_lifecycle.get_task(store=store, task_id=task_id, to_out=compat.to_out)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@router.post("/probe", response_model=TaskProbeOut, dependencies=[Depends(require_api_token)])
def probe_task_route(
    body: TaskProbeIn,
    probes: ProbeStore = Depends(get_probe_store),
) -> TaskProbeOut:
    from src.handler import tasks as compat

    if body.cookiesFromBrowser and not compat.settings.allow_cookies_from_browser:
        raise HTTPException(
            status_code=409,
            detail=ErrorDetail(
                code="BROWSER_COOKIES_DISABLED",
                message="浏览器 Cookie 探测未启用",
                suggestion="请在后端同一台可信机器上设置 SUBTRANS_ALLOW_COOKIES_FROM_BROWSER=1 后重试。",
            ).model_dump(exclude_none=True),
        )
    result, _record = compat._task_runtime.probe_and_record(
        url=body.url,
        cookies_from_browser=body.cookiesFromBrowser,
        probes=probes,
        probe_video=compat.probe_video,
        probe_batch_manager=compat.probe_batch_manager,
    )
    return TaskProbeOut(
        ok=result.ok,
        title=result.title,
        extractor=result.extractor,
        duration=result.duration,
        formatsCount=result.formats_count,
        webpageUrl=result.webpage_url,
        reason=result.reason,
        detail=result.detail,
        cached=result.cached,
        language=result.language,
        availableQualities=result.available_qualities,
        formats=result.formats,
        thumbnail=result.thumbnail,
        uploader=result.uploader,
    )


@router.get("/probe/startup-status", response_model=ProbeBatchStatusOut, dependencies=[Depends(require_api_token)])
def get_probe_startup_status_route() -> ProbeBatchStatusOut:
    from src.handler import tasks as compat

    return ProbeBatchStatusOut.model_validate(compat.probe_batch_manager.status())


@router.get("/probe/records", response_model=List[ProbeRecordOut], dependencies=[Depends(require_api_token)])
def list_probe_records_route(
    limit: int = Query(50, ge=1, le=500),
    probes: ProbeStore = Depends(get_probe_store),
) -> List[ProbeRecordOut]:
    from src.handler import tasks as compat

    return [
        _probe_record_to_out(r)
        for r in compat._task_runtime.list_probe_records(limit=limit, probes=probes)
    ]


@router.delete("/probe/records", response_model=ProbeRecordsClearOut, dependencies=[Depends(require_api_token)])
def clear_probe_records_route(
    probes: ProbeStore = Depends(get_probe_store),
) -> ProbeRecordsClearOut:
    from src.handler import tasks as compat

    return ProbeRecordsClearOut(deleted=compat._task_runtime.clear_probe_records(probes=probes))


@router.delete("/probe/records/{record_id}", status_code=204, dependencies=[Depends(require_api_token)])
def delete_probe_record_route(
    record_id: str,
    probes: ProbeStore = Depends(get_probe_store),
) -> None:
    from src.handler import tasks as compat

    if not compat._task_runtime.delete_probe_record(record_id=record_id, probes=probes):
        raise HTTPException(status_code=404, detail="测试记录不存在")


@router.get("/probe/ytdlp-info", response_model=YtDlpEnvInfo, dependencies=[Depends(require_api_token)])
def get_ytdlp_info_route() -> YtDlpEnvInfo:
    from src.handler import tasks as compat
    return YtDlpEnvInfo.model_validate(compat._task_runtime.ytdlp_info(compat.settings))


@router.delete("/{task_id}", status_code=204, dependencies=[Depends(require_api_token)])
@task_error_boundary
def delete_task_route(task_id: str, store: TaskStore = Depends(get_store)) -> None:
    from src.handler import tasks as compat

    compat._task_lifecycle.delete_task(
        store=store,
        task_id=task_id,
        task_dir=compat.task_dir,
        cleanup_partial_artifacts=compat._cleanup_partial_artifacts,
        release_lock=compat.release_lock,
    )


@router.post("/{task_id}/cancel", response_model=TaskOut, dependencies=[Depends(require_api_token)])
@task_error_boundary
def cancel_task_route(task_id: str, store: TaskStore = Depends(get_store)) -> TaskOut:
    from src.handler import tasks as compat

    return compat._task_lifecycle.cancel_task(
        store=store,
        task_id=task_id,
        cancel_pipeline=compat.cancel_pipeline,
        to_out=compat.to_out,
    )


@router.post("/{task_id}/retry", response_model=TaskOut, dependencies=[Depends(require_api_token)])
@task_error_boundary
def retry_task_route(task_id: str, store: TaskStore = Depends(get_store)) -> TaskOut:
    from src.handler import tasks as compat

    rec = compat._task_lifecycle.require(store, task_id)
    if str(rec.model or "").partition(":")[0].strip().lower() == "replicate":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REPLICATE_BACKEND_REMOVED",
                "message": "该历史任务使用已移除的 Replicate 转写，请新建任务并选择本地 Whisper 模型",
            },
        )
    return compat._task_lifecycle.retry_task(
        store=store,
        task_id=task_id,
        enqueue_pipeline=compat.enqueue_pipeline,
        to_out=compat.to_out,
    )
