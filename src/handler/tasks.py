"""任务相关路由（业务域：tasks）。

新增其它业务时，仿照本文件建一个 APIRouter，再在 app.py 里 include 即可。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

from src.config import (
    OUTPUT_VIDEO,
    OUTPUT_VIDEO_NAMES,
    SOURCE_VIDEO_STEM,
    TRANSLATED_SRT,
    artifacts_present,
    settings,
    task_dir,
)
from src.core.downloader import probe_video
from src.core.ffmpeg_utils import probe_duration
from src.handler.subtitle_editor import release_lock
from src.handler.deps import (
    get_probe_store,
    get_store,
    get_translation_engine_store,
    require_api_token,
)
from src.handler.schemas import (
    ErrorDetail,
    ProbeRecordOut,
    ProbeRecordsClearOut,
    ProbeBatchStatusOut,
    TaskCreate,
    TaskOut,
    TaskProbeIn,
    TaskProbeOut,
    YtDlpEnvInfo,
    _probe_record_to_out,
    to_out,
)
from src.service.runner import _cleanup_partial_artifacts, cancel_pipeline, enqueue_pipeline
from src.service.asset_resolver import AssetResolver, ResourceState
from src.service.probe_batch import probe_batch_manager
from src.service.model_manager import model_manager
from src.service.translation_model_manager import get_translation_model_manager
from src.service.task import task_artifacts as _task_artifacts
from src.service.task import task_runtime as _task_runtime
from src.service.task import task_lifecycle as _task_lifecycle
from src.store import (
    DOWNGRADE_REASON_DISK_FAILURE,
    DOWNGRADE_REASON_UNKNOWN,
    DOWNGRADE_REASON_USER_CLEANED,
    DOWNGRADE_REASON_VOLUME_MIGRATED,
    DEFAULT_TRANSLATION_ENGINE_ID,
    RESOURCE_STATUS_AVAILABLE,
    RESOURCE_STATUS_MISSING,
    ProbeStore,
    TaskStore,
    TranslationEngineStore,
)

logger = logging.getLogger(__name__)

def _ensure_local_model_ready(model: str) -> None:
    value = str(model or "")
    backend, separator, name = value.partition(":")
    if separator and backend.strip().lower() in {"local", "local_whisper", "whisper", "faster_whisper"}:
        if not model_manager.is_ready(name.strip()):
            raise HTTPException(status_code=409, detail={"code": "MODEL_NOT_READY", "message": f"本地模型未导入或未通过检查: {name}"})

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

_BROWSER_PREVIEW_EXTENSIONS = {
    ".txt", ".srt", ".vtt", ".json", ".md", ".log", ".csv",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mov",
}

_TERMINAL = {"SUCCESS", "FAILED", "CANCELLED"}

# 资源已丢失时给用户的简短、稳定错误文案，避免把文件系统异常 / 堆栈漏到 UI
_DELETED_MESSAGE = "资源已删除"

# 允许上传的本地视频扩展名（小写，含点）
_UPLOAD_VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi",
    ".m4v", ".flv", ".ts", ".mpeg", ".mpg", ".wmv",
}


def _upload_error(
    status_code: int,
    *,
    code: str,
    message: str,
    limits: Optional[dict] = None,
    suggestion: Optional[str] = None,
) -> HTTPException:
    detail = ErrorDetail(
        code=code,
        message=message,
        limits=limits,
        suggestion=suggestion,
    ).model_dump(exclude_none=True)
    return HTTPException(status_code=status_code, detail=detail)


def _require(store: TaskStore, task_id: str):
    rec = store.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return rec


def _mark_resource_missing(
    store: TaskStore,
    task_id: str,
    reason: str,
    downgrade_reason: str = DOWNGRADE_REASON_UNKNOWN,
    downgrade_errno: Optional[int] = None,
) -> None:
    """把一个任务的 resource_status 幂等地置为 MISSING。"""
    _task_runtime.mark_resource_missing(
        store,
        task_id,
        reason,
        downgrade_reason=downgrade_reason,
        downgrade_errno=downgrade_errno,
    )


def _ensure_translation_engine(
    engine: str,
    need_subtitle: bool,
    engines: TranslationEngineStore,
) -> None:
    if not need_subtitle:
        return
    if engine == "deepseek":
        if not (settings.deepseek_api_key and settings.deepseek_api_key.strip()):
            raise HTTPException(
                status_code=422,
                detail="缺少 DeepSeek API Key，请在 .env 配置 SUBTRANS_DEEPSEEK_API_KEY",
            )
        return

    rec = engines.get(engine)
    if rec is None:
        raise HTTPException(status_code=422, detail="翻译引擎配置不存在")
    if not rec.enabled:
        raise HTTPException(status_code=422, detail="翻译引擎已停用")
    if rec.api_type == "local_ct2":
        manager = get_translation_model_manager()
        if not manager.is_ready():
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "LOCAL_TRANSLATION_MODEL_NOT_READY",
                    "message": "本地英译中模型尚未安装，请先在翻译引擎设置中下载。",
                },
            )
        dependency_status = getattr(manager, "dependency_status", None)
        if dependency_status is not None:
            status = dependency_status()
            if not status.get("ready", False):
                missing = ", ".join(status.get("missing", []))
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "LOCAL_TRANSLATION_DEPENDENCY_MISSING",
                        "message": f"本地翻译依赖未安装：{missing}。请运行 uv sync --extra local-translation。",
                    },
                )
        return
    if not (rec.api_key and rec.api_key.strip()):
        raise HTTPException(status_code=422, detail="翻译引擎尚未配置 API Key")
    if rec.availability != "AVAILABLE":
        raise HTTPException(status_code=422, detail="翻译引擎尚未通过可用性检测")


def _ensure_supported_translation_languages(
    engine: str,
    need_subtitle: bool,
    source_lang: str,
    target_lang: str,
) -> None:
    if not need_subtitle or engine != "local-opus-en-zh":
        return
    if source_lang != "en" or target_lang not in {"zh-CN", "zh"}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNSUPPORTED_TRANSLATION_LANGUAGE",
                "message": "本地翻译模型只支持源语言 en 和目标语言 zh-CN/zh。",
            },
        )


def scan_missing_terminal(
    store: TaskStore,
    *,
    task_id: Optional[str] = None,
    data_dir=None,
    downgrade_reason: Optional[str] = None,
) -> List[str]:
    """扫描终态任务的资源状态；实现位于 task_runtime。"""
    return _task_runtime.scan_missing_terminal(
        store,
        task_id=task_id,
        data_dir=data_dir,
        settings=settings,
        downgrade_reason=downgrade_reason,
    )

# ---------- CRUD ----------

@router.post("", response_model=TaskOut, status_code=201, dependencies=[Depends(require_api_token)])
def create_task(
    body: TaskCreate,
    store: TaskStore = Depends(get_store),
    engines: TranslationEngineStore = Depends(get_translation_engine_store),
    task_origin: str = Header("web", alias="X-Task-Origin"),
) -> TaskOut:
    rec, created = _task_lifecycle.create_url_task(
        body=body, store=store, engines=engines,
        ensure_supported_translation_languages=_ensure_supported_translation_languages,
        ensure_local_model_ready=_ensure_local_model_ready,
        ensure_translation_engine=_ensure_translation_engine,
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
    enqueue_pipeline(rec.id)  # 第 2 步接入真正执行
    return to_out(rec)


@router.post("/upload", response_model=TaskOut, status_code=201, dependencies=[Depends(require_api_token)])
def create_upload_task(
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
    """上传本地视频并创建任务；文件处理实现位于 task_lifecycle。"""
    try:
        rec = _task_lifecycle.create_upload_task(
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
            settings=settings,
            task_dir=task_dir,
            probe_duration=probe_duration,
            enqueue_pipeline=enqueue_pipeline,
            release_lock=release_lock,
            ensure_supported_translation_languages=_ensure_supported_translation_languages,
            ensure_local_model_ready=_ensure_local_model_ready,
            ensure_translation_engine=_ensure_translation_engine,
            upload_error=_upload_error,
            http_exception_type=HTTPException,
            source_video_stem=SOURCE_VIDEO_STEM,
            upload_video_extensions=_UPLOAD_VIDEO_EXTS,
        )
    except _task_lifecycle.UploadTaskError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return to_out(rec)

@router.get("", response_model=List[TaskOut], dependencies=[Depends(require_api_token)])
def list_tasks(
    offset: int = Query(0, ge=0, description="跳过前 N 条记录"),
    limit: int = Query(50, ge=1, le=200, description="单页最大记录数，取值范围 1 到 200，默认 50"),
    before_id: Optional[str] = Query(None, description="游标：仅返回 ID 早于该任务的记录"),
    after_id: Optional[str] = Query(None, description="游标：仅返回 ID 晚于该任务的记录"),
    origin: Optional[Literal["web", "mcp"]] = Query(None, description="任务来源"),
    store: TaskStore = Depends(get_store),
) -> List[TaskOut]:
    return _task_lifecycle.list_tasks(store=store, to_out=to_out, offset=offset, limit=limit,
        before_id=before_id, after_id=after_id, origin=origin)


@router.get("/{task_id}", response_model=TaskOut, dependencies=[Depends(require_api_token)])
def get_task(task_id: str, store: TaskStore = Depends(get_store)) -> TaskOut:
    try:
        return _task_lifecycle.get_task(store=store, task_id=task_id, to_out=to_out)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@router.post("/probe", response_model=TaskProbeOut, dependencies=[Depends(require_api_token)])
def probe_task(
    body: TaskProbeIn,
    probes: ProbeStore = Depends(get_probe_store),
) -> TaskProbeOut:
    """执行 yt-dlp 探测并返回持久化结果。"""
    if body.cookiesFromBrowser and not settings.allow_cookies_from_browser:
        raise HTTPException(
            status_code=409,
            detail=ErrorDetail(
                code="BROWSER_COOKIES_DISABLED",
                message="浏览器 Cookie 探测未启用",
                suggestion="请在后端同一台可信机器上设置 SUBTRANS_ALLOW_COOKIES_FROM_BROWSER=1 后重试。",
            ).model_dump(exclude_none=True),
        )
    result, _record = _task_runtime.probe_and_record(
        url=body.url,
        cookies_from_browser=body.cookiesFromBrowser,
        probes=probes,
        probe_video=probe_video,
        probe_batch_manager=probe_batch_manager,
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
def get_probe_startup_status() -> ProbeBatchStatusOut:
    """返回固定十站点启动探测与开发者页重测的当前状态。"""
    return ProbeBatchStatusOut.model_validate(probe_batch_manager.status())


@router.get("/probe/records", response_model=List[ProbeRecordOut], dependencies=[Depends(require_api_token)])
def list_probe_records(
    limit: int = Query(50, ge=1, le=500),
    probes: ProbeStore = Depends(get_probe_store),
) -> List[ProbeRecordOut]:
    """按时间倒序返回最近的下载测试记录。limit 默认 50，上限 500。"""
    return [_probe_record_to_out(r) for r in _task_runtime.list_probe_records(limit=limit, probes=probes)]


@router.delete("/probe/records", response_model=ProbeRecordsClearOut, dependencies=[Depends(require_api_token)])
def clear_probe_records(
    probes: ProbeStore = Depends(get_probe_store),
) -> ProbeRecordsClearOut:
    """一键清空所有下载测试历史。"""
    deleted = _task_runtime.clear_probe_records(probes=probes)
    return ProbeRecordsClearOut(deleted=deleted)


@router.delete("/probe/records/{record_id}", status_code=204, dependencies=[Depends(require_api_token)])
def delete_probe_record(
    record_id: str,
    probes: ProbeStore = Depends(get_probe_store),
) -> None:
    """删除单条下载测试历史；不存在返回 404。"""
    if not _task_runtime.delete_probe_record(record_id=record_id, probes=probes):
        raise HTTPException(status_code=404, detail="测试记录不存在")


@router.get("/probe/ytdlp-info", response_model=YtDlpEnvInfo, dependencies=[Depends(require_api_token)])
def get_ytdlp_info() -> YtDlpEnvInfo:
    """返回 yt-dlp 环境摘要；实现位于 task_runtime。"""
    return YtDlpEnvInfo.model_validate(_task_runtime.ytdlp_info(settings))

@router.delete("/{task_id}", status_code=204, dependencies=[Depends(require_api_token)])
def delete_task(task_id: str, store: TaskStore = Depends(get_store)) -> None:
    """删除终态任务及其目录，取消清理期间拒绝删除以避免目录竞态。"""
    try:
        _task_lifecycle.delete_task(store=store, task_id=task_id, task_dir=task_dir,
            cleanup_partial_artifacts=_cleanup_partial_artifacts, release_lock=release_lock)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except _task_lifecycle.LifecycleConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{task_id}/cancel", response_model=TaskOut, dependencies=[Depends(require_api_token)])
def cancel_task(task_id: str, store: TaskStore = Depends(get_store)) -> TaskOut:
    """取消正在运行的任务。"""
    try:
        return _task_lifecycle.cancel_task(store=store, task_id=task_id,
            cancel_pipeline=cancel_pipeline, to_out=to_out)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except _task_lifecycle.LifecycleConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{task_id}/retry", response_model=TaskOut, dependencies=[Depends(require_api_token)])
def retry_task(task_id: str, store: TaskStore = Depends(get_store)) -> TaskOut:
    """仅允许失败或已取消任务重新入队，避免运行中任务重复执行。"""
    try:
        return _task_lifecycle.retry_task(store=store, task_id=task_id,
            enqueue_pipeline=enqueue_pipeline, to_out=to_out)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except _task_lifecycle.LifecycleConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ---------- 文件下载 ----------

@router.head("/{task_id}/source", status_code=204, dependencies=[Depends(require_api_token)])
def check_source_video(task_id: str, store: TaskStore = Depends(get_store)):
    """轻量确认源视频是否可用。"""
    download_source_video(task_id, store)
    return Response(status_code=204)


@router.get("/{task_id}/source", dependencies=[Depends(require_api_token)])
def download_source_video(task_id: str, store: TaskStore = Depends(get_store)):
    """返回未烧录字幕的源视频。"""
    try:
        path = _task_artifacts.download_source(task_id=task_id, store=store, task_dir=task_dir)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(path, filename=f"{task_id}-source{path.suffix}")


@router.head("/{task_id}/download", status_code=204, dependencies=[Depends(require_api_token)])
def check_download_video(task_id: str, store: TaskStore = Depends(get_store)):
    """轻量确认成品视频是否可用。"""
    download_video(task_id, store)
    return Response(status_code=204)


@router.get("/{task_id}/download", dependencies=[Depends(require_api_token)])
def download_video(task_id: str, store: TaskStore = Depends(get_store)):
    try:
        path = _task_artifacts.download_video(task_id=task_id, store=store,
            resolve_video_fn=_resolve_video, mark_missing=_mark_resource_missing,
            available_status=RESOURCE_STATUS_AVAILABLE,
            missing_downgrade_reason=DOWNGRADE_REASON_DISK_FAILURE)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.head("/{task_id}/dubbed", status_code=204, dependencies=[Depends(require_api_token)])
def check_dubbed_video(task_id: str, store: TaskStore = Depends(get_store)):
    download_dubbed_video(task_id, store)
    return Response(status_code=204)


@router.get("/{task_id}/dubbed", dependencies=[Depends(require_api_token)])
def download_dubbed_video(task_id: str, store: TaskStore = Depends(get_store)):
    try:
        path = _task_artifacts.download_dubbed(task_id=task_id, store=store, task_dir=task_dir)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(path, media_type="video/mp4", filename=path.name)

def _resolve_video(task_id: str, mode: str | None = None):
    """定位可下载的视频：优先烧录成品 output.mp4，仅下载模式回退到 source.*（排除 .part 临时文件）。"""
    return _task_artifacts.resolve_video(
        task_id,
        mode,
        task_dir=task_dir,
        output_video=OUTPUT_VIDEO,
        output_video_names=OUTPUT_VIDEO_NAMES,
    )


@router.get("/{task_id}/subtitle", dependencies=[Depends(require_api_token)])
def download_subtitle(task_id: str, store: TaskStore = Depends(get_store)):
    try:
        path = _task_artifacts.download_subtitle(task_id=task_id, store=store, task_dir=task_dir,
            mark_missing=_mark_resource_missing, available_status=RESOURCE_STATUS_AVAILABLE,
            missing_downgrade_reason=DOWNGRADE_REASON_DISK_FAILURE)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(path, media_type="application/x-subrip", filename=f"{task_id}.srt")


@router.post("/{task_id}/folder", summary="打开任务文件夹", dependencies=[Depends(require_api_token)])
def open_task_folder(task_id: str, store: TaskStore = Depends(get_store)) -> dict:
    """用系统文件管理器打开任务产物目录。"""
    try:
        _task_artifacts.validate_task_id(task_id)
    except _task_artifacts.TaskFolderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _require(store, task_id)
    try:
        path = _task_artifacts.prepare_task_folder(task_id, task_dir=task_dir, data_dir=settings.data_dir)
    except _task_artifacts.TaskFolderMissingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except _task_artifacts.TaskFolderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        _task_artifacts.open_folder(
            path,
            platform=sys.platform,
            popen=subprocess.Popen,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True}


def _task_path(task_id: str, relative_path: str = "") -> Path:
    """Resolve a path below a task directory and reject traversal."""
    try:
        return _task_artifacts.task_path(task_id, relative_path, task_dir=task_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="文件路径非法") from exc


@router.get("/{task_id}/folder-capability", summary="查询文件夹预览能力", dependencies=[Depends(require_api_token)])
def folder_capability(task_id: str, store: TaskStore = Depends(get_store)) -> dict:
    """告诉前端当前服务器是否有可用的图形化文件管理器。"""
    _require(store, task_id)
    return _task_artifacts.folder_capability(platform=sys.platform)


@router.get("/{task_id}/files", summary="浏览任务文件", dependencies=[Depends(require_api_token)])
def list_task_files(task_id: str, path: str = "", store: TaskStore = Depends(get_store)) -> dict:
    """返回任务目录下的文件列表，供无图形界面的 Linux 预览。"""
    _require(store, task_id)
    try:
        entries = _task_artifacts.list_files(task_id, path, task_dir=task_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"path": path, "entries": entries}


@router.get("/{task_id}/file", summary="读取任务文件", dependencies=[Depends(require_api_token)])
def get_task_file(task_id: str, path: str, store: TaskStore = Depends(get_store)):
    """提供浏览器预览所需的安全文件响应。"""
    _require(store, task_id)
    try:
        file_path = _task_artifacts.preview_file(task_id, path, task_dir=task_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except _task_artifacts.PathSafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except _task_artifacts.UnsupportedPreviewError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    return FileResponse(file_path)


def _open_folder(path: Path | str) -> None:
    """兼容旧调用的目录打开包装。"""
    try:
        _task_artifacts.open_folder(path, platform=sys.platform, popen=subprocess.Popen)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------- SSE 进度 ----------

def _sse_payload(rec) -> str:
    return _task_runtime.sse_payload(rec, to_out=to_out)


@router.get("/{task_id}/stream", dependencies=[Depends(require_api_token)])
def stream_progress(task_id: str, store: TaskStore = Depends(get_store)):
    """轮询库表并以 SSE 推送进度（含心跳保活、超时断流与终态事件）。"""
    _require(store, task_id)
    events = _task_runtime.progress_events(
        task_id,
        store=store,
        settings=settings,
        to_out=to_out,
    )
    return StreamingResponse(events, media_type="text/event-stream")
