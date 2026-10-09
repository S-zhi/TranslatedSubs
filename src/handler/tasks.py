"""任务相关路由（业务域：tasks）。

新增其它业务时，仿照本文件建一个 APIRouter，再在 app.py 里 include 即可。
"""

from __future__ import annotations

import errno
import json
import logging
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
    rec = store.get(task_id)
    if rec is None or rec.resource_status == RESOURCE_STATUS_MISSING:
        return
    store.update(
        task_id,
        resource_status=RESOURCE_STATUS_MISSING,
        error=reason,
        downgrade_reason=downgrade_reason,
        downgrade_errno=downgrade_errno,
        downgraded_at=int(time.time() * 1000),
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
    """扫描终态 SUCCESS 任务，把磁盘产物已丢失的降级为 MISSING。

    幂等：已为 MISSING 的不再处理；运行中任务（status != SUCCESS）忽略。
    若指定 task_id，则仅针对该任务执行单任务降级检测。
    返回被降级的 task_id 列表，便于启动日志 / 测试断言 / 资源清理。
    """
    data_path = Path(data_dir if data_dir is not None else settings.data_dir)
    downgraded: List[str] = []
    if task_id is not None:
        target = store.get(task_id)
        recs = [target] if target is not None else []
    else:
        recs = store.list()

    success_count = sum(1 for rec in recs if rec.status == "SUCCESS")
    data_root_unavailable = False
    detected_reason: Optional[str] = None
    detected_errno: Optional[int] = None

    try:
        data_path.stat()
        list(data_path.iterdir())
    except FileNotFoundError:
        data_root_unavailable = True
    except NotADirectoryError as e:
        logger.warning("scan_missing_terminal data_dir 异常: errno=%s msg=%s", e.errno, e)
        data_root_unavailable = True
        detected_reason = DOWNGRADE_REASON_USER_CLEANED
        detected_errno = e.errno
    except PermissionError as e:
        logger.warning("scan_missing_terminal data_dir 异常: errno=%s msg=%s", e.errno, e)
        data_root_unavailable = True
        detected_reason = DOWNGRADE_REASON_DISK_FAILURE
        detected_errno = e.errno
    except OSError as e:
        logger.warning("scan_missing_terminal data_dir 异常: errno=%s msg=%s", e.errno, e)
        data_root_unavailable = True
        detected_errno = e.errno
        if e.errno in (errno.EIO, errno.ENXIO, errno.ESTALE):
            detected_reason = DOWNGRADE_REASON_DISK_FAILURE
        else:
            detected_reason = DOWNGRADE_REASON_UNKNOWN

    for rec in recs:
        if rec.status != "SUCCESS":
            continue
        if rec.resource_status == RESOURCE_STATUS_MISSING:
            continue

        need_sub = bool(rec.need_subtitle)
        if need_sub:
            state_out, _, _ = AssetResolver.resolve_output_video(rec.id)
            state_srt, _, _ = AssetResolver.resolve_translated_srt(rec.id)
            if state_out == ResourceState.AVAILABLE and state_srt == ResourceState.AVAILABLE:
                continue
            unreadable = state_out == ResourceState.UNREADABLE or state_srt == ResourceState.UNREADABLE
        else:
            state_src, _, _ = AssetResolver.resolve_source(rec.id)
            if state_src == ResourceState.AVAILABLE:
                continue
            unreadable = state_src == ResourceState.UNREADABLE

        error_msg = "资源不可读" if unreadable else _DELETED_MESSAGE
        audit_errno: Optional[int] = None
        if downgrade_reason is not None:
            audit_reason = downgrade_reason
        elif unreadable:
            audit_reason = DOWNGRADE_REASON_DISK_FAILURE
        elif data_root_unavailable:
            if detected_reason is not None:
                audit_reason = detected_reason
                audit_errno = detected_errno
            elif success_count > 1:
                audit_reason = DOWNGRADE_REASON_VOLUME_MIGRATED
            else:
                audit_reason = DOWNGRADE_REASON_UNKNOWN
        else:
            audit_reason = DOWNGRADE_REASON_UNKNOWN

        store.update(
            rec.id,
            resource_status=RESOURCE_STATUS_MISSING,
            error=error_msg,
            downgrade_reason=audit_reason,
            downgrade_errno=audit_errno,
            downgraded_at=int(time.time() * 1000),
        )
        downgraded.append(rec.id)
    return downgraded


# ---------- CRUD ----------

@router.post("", response_model=TaskOut, status_code=201, dependencies=[Depends(require_api_token)])
def create_task(
    body: TaskCreate,
    store: TaskStore = Depends(get_store),
    engines: TranslationEngineStore = Depends(get_translation_engine_store),
    task_origin: str = Header("web", alias="X-Task-Origin"),
) -> TaskOut:
    _ensure_supported_translation_languages(body.engine, body.needSubtitle, body.sourceLang, body.targetLang)
    _ensure_local_model_ready(body.model)
    _ensure_translation_engine(body.engine, body.needSubtitle, engines)
    rec, created = store.create_if_no_recent_active(
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
    """上传本地视频并创建任务：源文件直接落盘，跳过下载，走后续识别 / 翻译 / 烧录。

    字幕模式（mode）与烧录方式（burn）与链接任务同样透传到下层流水线。
    """
    # multipart 请求的 Content-Length 包含边界和表单字段，不能代表视频文件大小。
    # 实际文件大小在写入过程中通过 written_bytes 流式校验。
    max_upload_bytes = settings.max_upload_mb * 1024 * 1024
    filename = (file.filename or "").strip()
    _ensure_supported_translation_languages(engine, needSubtitle, sourceLang, targetLang)
    _ensure_translation_engine(engine, needSubtitle, engines)
    _ensure_local_model_ready(model)
    if ttsEnabled and not needSubtitle:
        raise HTTPException(status_code=422, detail="配音需要启用字幕翻译")
    if ttsEnabled and not targetLang.lower().startswith(("zh", "en")):
        raise HTTPException(status_code=422, detail="Kokoro 配音目前支持中文和英文目标语")
    if ttsEnabled and ttsVoice not in {"auto", "zf_001", "zm_010", "af_maple", "af_sol"}:
        raise HTTPException(status_code=422, detail="不支持的 Kokoro 音色")
    if ttsEnabled and ttsVoice in {"zf_001", "zm_010"} and not targetLang.lower().startswith("zh"):
        raise HTTPException(status_code=422, detail="中文音色只能用于中文目标语")
    if ttsEnabled and ttsVoice in {"af_maple", "af_sol"} and not targetLang.lower().startswith("en"):
        raise HTTPException(status_code=422, detail="英文音色只能用于英文目标语")
    ext = Path(filename).suffix.lower()
    if ext not in _UPLOAD_VIDEO_EXTS:
        supported_formats = sorted(_UPLOAD_VIDEO_EXTS)
        raise _upload_error(
            400,
            code="UNSUPPORTED_FORMAT",
            message=f"不支持的视频格式：{ext or '未知'}（支持 {', '.join(supported_formats)}）",
            limits={"supportedFormats": supported_formats},
            suggestion="请将视频转换为受支持的格式后重新上传。",
        )

    # 先建记录拿到 task_id，再把源文件落盘到该任务目录
    rec = store.create(
        url=filename,
        source_lang=sourceLang,
        target_lang=targetLang,
        mode=mode,
        burn=burn,
        model=model,
        engine=engine,
        source_type="upload",
        need_subtitle=needSubtitle,
        title=Path(filename).stem or "上传的视频",
        tts_enabled=ttsEnabled,
        tts_voice=ttsVoice,
        original_voice_mode=originalVoiceMode,
    )

    d = task_dir(rec.id)
    d.mkdir(parents=True, exist_ok=True)
    dest_part = d / f"{SOURCE_VIDEO_STEM}{ext}.part"
    dest = d / f"{SOURCE_VIDEO_STEM}{ext}"
    written_bytes = 0
    chunk_size = 1024 * 1024  # 1MB
    try:
        with dest_part.open("wb") as out:
            while True:
                chunk = file.file.read(chunk_size)
                if not chunk:
                    break
                written_bytes += len(chunk)
                if written_bytes > max_upload_bytes:
                    raise _upload_error(
                        413,
                        code="UPLOAD_TOO_LARGE",
                        message=f"上传文件大小超过最大限制 ({settings.max_upload_mb} MB)",
                        limits={"maxMb": settings.max_upload_mb},
                        suggestion="请压缩或切分视频，也可以改用 URL 任务模式。",
                    )
                out.write(chunk)
    except HTTPException:
        store.delete(rec.id)
        shutil.rmtree(d, ignore_errors=True)
        raise
    except Exception as e:
        store.delete(rec.id)
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(status_code=500, detail="保存上传文件失败") from e
    finally:
        file.file.close()

    if not dest_part.exists() or dest_part.stat().st_size == 0:
        store.delete(rec.id)
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(status_code=400, detail="上传的视频文件为空")

    duration_sec = probe_duration(dest_part, settings.ffprobe_bin)
    if duration_sec is None:
        store.delete(rec.id)
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(
            status_code=400,
            detail="无法解析视频时长，请确认文件格式正确且 ffprobe 可用",
        )

    max_video_seconds = settings.max_video_minutes * 60
    if duration_sec > max_video_seconds:
        store.delete(rec.id)
        shutil.rmtree(d, ignore_errors=True)
        release_lock(rec.id)
        raise _upload_error(
            400,
            code="UPLOAD_DURATION_EXCEEDED",
            message=f"视频时长 ({duration_sec / 60:.1f} 分钟) 超过最大限制 ({settings.max_video_minutes} 分钟)",
            limits={
                "maxMinutes": settings.max_video_minutes,
                "durationMinutes": round(duration_sec / 60, 1),
            },
            suggestion="请裁剪或切分视频后重新上传。",
        )

    dest_part.replace(dest)

    enqueue_pipeline(rec.id)
    return to_out(store.get(rec.id) or rec)


@router.get("", response_model=List[TaskOut], dependencies=[Depends(require_api_token)])
def list_tasks(
    offset: int = Query(0, ge=0, description="跳过前 N 条记录"),
    limit: int = Query(50, ge=1, le=200, description="单页最大记录数，取值范围 1 到 200，默认 50"),
    before_id: Optional[str] = Query(None, description="游标：仅返回 ID 早于该任务的记录"),
    after_id: Optional[str] = Query(None, description="游标：仅返回 ID 晚于该任务的记录"),
    origin: Optional[Literal["web", "mcp"]] = Query(None, description="任务来源"),
    store: TaskStore = Depends(get_store),
) -> List[TaskOut]:
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


@router.get("/{task_id}", response_model=TaskOut, dependencies=[Depends(require_api_token)])
def get_task(task_id: str, store: TaskStore = Depends(get_store)) -> TaskOut:
    return to_out(_require(store, task_id))


@router.post("/probe", response_model=TaskProbeOut, dependencies=[Depends(require_api_token)])
def probe_task(
    body: TaskProbeIn,
    probes: ProbeStore = Depends(get_probe_store),
) -> TaskProbeOut:
    """探测视频链接是否能被 yt-dlp 解析并找到可下载格式。

    每次探测都持久化一行到 probe_records，便于用户回看历史、
    排查"链接换格式还是不可下载"等问题。失败也会记录（含 reason/detail），
    错误信息不会因为页面刷新而丢失。
    """
    if body.cookiesFromBrowser and not settings.allow_cookies_from_browser:
        raise HTTPException(
            status_code=409,
            detail=ErrorDetail(
                code="BROWSER_COOKIES_DISABLED",
                message="浏览器 Cookie 探测未启用",
                suggestion="请在后端同一台可信机器上设置 SUBTRANS_ALLOW_COOKIES_FROM_BROWSER=1 后重试。",
            ).model_dump(exclude_none=True),
        )

    result = probe_video(body.url, cookies_from_browser=body.cookiesFromBrowser)
    # 探测本身失败不影响响应；同时把 ok=False 的记录也存下来，方便回看错误
    record = probes.record(
        url=body.url,
        ok=result.ok,
        title=result.title,
        extractor=result.extractor,
        duration=result.duration,
        formats_count=result.formats_count,
        webpage_url=result.webpage_url,
        reason=result.reason,
        detail=result.detail,
        language=result.language,
        available_qualities=result.available_qualities,
        formats=result.formats,
        thumbnail=result.thumbnail,
        uploader=result.uploader,
    )
    probe_batch_manager.record_manual_probe(body.url, result, record.created_at)
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
    return [_probe_record_to_out(r) for r in probes.list(limit=limit)]


@router.delete("/probe/records", response_model=ProbeRecordsClearOut, dependencies=[Depends(require_api_token)])
def clear_probe_records(
    probes: ProbeStore = Depends(get_probe_store),
) -> ProbeRecordsClearOut:
    """一键清空所有下载测试历史。"""
    deleted = probes.clear()
    return ProbeRecordsClearOut(deleted=deleted)


@router.delete("/probe/records/{record_id}", status_code=204, dependencies=[Depends(require_api_token)])
def delete_probe_record(
    record_id: str,
    probes: ProbeStore = Depends(get_probe_store),
) -> None:
    """删除单条下载测试历史；不存在返回 404。"""
    if not probes.delete(record_id):
        raise HTTPException(status_code=404, detail="测试记录不存在")


@router.get("/probe/ytdlp-info", response_model=YtDlpEnvInfo, dependencies=[Depends(require_api_token)])
def get_ytdlp_info() -> YtDlpEnvInfo:
    """返回 yt-dlp 的版本号、提取器总数与代理/配置状态。"""
    version = None
    try:
        import yt_dlp.version
        version = getattr(yt_dlp.version, "__version__", None)
    except Exception:
        pass

    extractors_count = 0
    try:
        from yt_dlp.extractor import list_extractors
        extractors_count = len(list_extractors())
    except Exception:
        pass

    proxy = getattr(settings, "download_proxy", None)
    proxy_masked = None
    if proxy:
        import re
        proxy_masked = re.sub(r"://[^@]+@", "://***:***@", str(proxy))

    cookies_configured = bool(settings.cookies_file and Path(settings.cookies_file).is_file())

    return YtDlpEnvInfo(
        version=version,
        extractorsCount=extractors_count,
        proxyConfigured=bool(proxy),
        proxyMasked=proxy_masked,
        cookiesConfigured=cookies_configured,
        browserCookiesEnabled=bool(settings.allow_cookies_from_browser),
        cacheTtlSec=float(settings.probe_cache_ttl_sec),
    )


@router.delete("/{task_id}", status_code=204, dependencies=[Depends(require_api_token)])
def delete_task(task_id: str, store: TaskStore = Depends(get_store)) -> None:
    """删除终态任务及其目录，取消清理期间拒绝删除以避免目录竞态。"""
    rec = _require(store, task_id)
    if rec.is_cancelling:
        raise HTTPException(status_code=409, detail="任务正在取消，请稍后再试")
    if rec.status not in _TERMINAL:
        raise HTTPException(
            status_code=409,
            detail="任务运行中，请先等待或调用取消接口",
        )
    _cleanup_partial_artifacts(task_id)
    AssetResolver.cleanup_cancelled_artifacts(
        task_id,
        current_step=rec.current_step,
        source_type=rec.source_type,
    )
    store.delete(task_id)
    shutil.rmtree(task_dir(task_id), ignore_errors=True)  # 连产物目录一起清
    release_lock(task_id)


@router.post("/{task_id}/cancel", response_model=TaskOut, dependencies=[Depends(require_api_token)])
def cancel_task(task_id: str, store: TaskStore = Depends(get_store)) -> TaskOut:
    """取消正在运行的任务。"""
    rec = _require(store, task_id)
    if rec.status in _TERMINAL:
        raise HTTPException(status_code=409, detail="任务非运行状态，无法取消")
    cancel_pipeline(task_id)
    updated = store.get(task_id) or rec
    return to_out(updated)


@router.post("/{task_id}/retry", response_model=TaskOut, dependencies=[Depends(require_api_token)])
def retry_task(task_id: str, store: TaskStore = Depends(get_store)) -> TaskOut:
    """仅允许失败或已取消任务重新入队，避免运行中任务重复执行。"""
    rec = _require(store, task_id)
    if rec.status not in ("FAILED", "CANCELLED"):
        raise HTTPException(status_code=409, detail="只有失败或已取消任务可以重试")
    updated = store.update(
        task_id,
        status="PENDING",
        progress=0,
        current_step=None,
        error=None,
    )
    enqueue_pipeline(task_id)
    return to_out(updated)


# ---------- 文件下载 ----------

@router.head("/{task_id}/source", status_code=204, dependencies=[Depends(require_api_token)])
def check_source_video(task_id: str, store: TaskStore = Depends(get_store)):
    """轻量确认源视频是否可用，避免前端先展示原生播放器加载态。"""
    download_source_video(task_id, store)
    return Response(status_code=204)


@router.get("/{task_id}/source", dependencies=[Depends(require_api_token)])
def download_source_video(task_id: str, store: TaskStore = Depends(get_store)):
    """返回未烧录字幕的源视频，供预览页在两个视频轨道之间切换。"""
    _require(store, task_id)
    state, path, message = AssetResolver.resolve_source(task_id)
    if state == ResourceState.AVAILABLE and path is not None:
        return FileResponse(path, filename=f"{task_id}-source{path.suffix}")
    raise HTTPException(status_code=409, detail=message)


@router.head("/{task_id}/download", status_code=204, dependencies=[Depends(require_api_token)])
def check_download_video(task_id: str, store: TaskStore = Depends(get_store)):
    """轻量确认成品视频是否可用，避免前端误显示播放器转圈。"""
    download_video(task_id, store)
    return Response(status_code=204)


@router.get("/{task_id}/download", dependencies=[Depends(require_api_token)])
def download_video(task_id: str, store: TaskStore = Depends(get_store)):
    rec = _require(store, task_id)
    if rec.status != "SUCCESS":
        raise HTTPException(status_code=409, detail="成品视频尚未生成")

    path = _resolve_video(task_id, rec.burn)
    if path is not None:
        state = AssetResolver.check_file_state(path)
        if state == ResourceState.AVAILABLE:
            return FileResponse(path, media_type="video/mp4", filename=path.name)
        elif state == ResourceState.UNREADABLE:
            if rec.resource_status == RESOURCE_STATUS_AVAILABLE:
                _mark_resource_missing(
                    store, task_id, "资源不可读", DOWNGRADE_REASON_DISK_FAILURE
                )
            raise HTTPException(status_code=409, detail="资源不可读")

    # 兜底：成功任务的产物被清掉时，要把状态降级为 MISSING，
    # 避免下次列表 / 详情接口继续暴露已失效的下载链接。
    if rec.resource_status == RESOURCE_STATUS_AVAILABLE:
        _mark_resource_missing(store, task_id, _DELETED_MESSAGE)
    raise HTTPException(
        status_code=409,
        detail=_DELETED_MESSAGE,
    )


@router.head("/{task_id}/dubbed", status_code=204, dependencies=[Depends(require_api_token)])
def check_dubbed_video(task_id: str, store: TaskStore = Depends(get_store)):
    download_dubbed_video(task_id, store)
    return Response(status_code=204)


@router.get("/{task_id}/dubbed", dependencies=[Depends(require_api_token)])
def download_dubbed_video(task_id: str, store: TaskStore = Depends(get_store)):
    rec = _require(store, task_id)
    if rec.status != "SUCCESS" or rec.tts_status != "SUCCESS":
        raise HTTPException(status_code=409, detail="配音视频尚未生成")
    path = task_dir(task_id) / (rec.output_dubbed_video or "output_dubbed.mp4")
    if AssetResolver.check_file_state(path) != ResourceState.AVAILABLE:
        raise HTTPException(status_code=409, detail="配音视频不可用")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


def _resolve_video(task_id: str, mode: str | None = None):
    """定位可下载的视频：优先烧录成品 output.mp4，仅下载模式回退到 source.*（排除 .part 临时文件）。"""
    d = task_dir(task_id)
    names = OUTPUT_VIDEO_NAMES
    if mode in ("hard", "soft"):
        preferred = "output_hard.mp4" if mode == "hard" else "output_soft.mp4"
        names = (preferred, OUTPUT_VIDEO)
    for name in names:
        out = d / name
        if out.is_file():
            return out
    state, source_path, _ = AssetResolver.resolve_source(task_id)
    if state == ResourceState.AVAILABLE and source_path is not None:
        return source_path
    return None


@router.get("/{task_id}/subtitle", dependencies=[Depends(require_api_token)])
def download_subtitle(task_id: str, store: TaskStore = Depends(get_store)):
    rec = _require(store, task_id)
    path = task_dir(task_id) / TRANSLATED_SRT
    state = AssetResolver.check_file_state(path)
    if state == ResourceState.AVAILABLE:
        return FileResponse(path, media_type="application/x-subrip", filename=f"{task_id}.srt")
    elif state == ResourceState.UNREADABLE:
        if rec.status == "SUCCESS" and rec.resource_status == RESOURCE_STATUS_AVAILABLE:
            _mark_resource_missing(
                store, task_id, "资源不可读", DOWNGRADE_REASON_DISK_FAILURE
            )
        raise HTTPException(status_code=409, detail="资源不可读")

    if rec.status == "SUCCESS" and rec.resource_status == RESOURCE_STATUS_AVAILABLE:
        _mark_resource_missing(store, task_id, _DELETED_MESSAGE)
    raise HTTPException(
        status_code=409,
        detail=_DELETED_MESSAGE if rec.status == "SUCCESS" else "译文字幕尚未生成",
    )


@router.post("/{task_id}/folder", summary="打开任务文件夹", dependencies=[Depends(require_api_token)])
def open_task_folder(task_id: str, store: TaskStore = Depends(get_store)) -> dict:
    """用系统文件管理器打开任务产物目录。"""
    if not task_id or not re.match(r"^task_[A-Za-z0-9_-]+$", task_id):
        raise HTTPException(status_code=400, detail="task_id 格式不符合规范")

    _require(store, task_id)
    path = task_dir(task_id).resolve()
    data_dir = settings.data_dir.resolve()
    try:
        if not path.is_relative_to(data_dir):
            raise HTTPException(status_code=400, detail="任务目录路径非法")
    except ValueError:
        raise HTTPException(status_code=400, detail="任务目录路径非法")

    if not path.exists():
        raise HTTPException(status_code=409, detail="任务目录尚未生成")
    _open_folder(path)
    return {"ok": True}


def _task_path(task_id: str, relative_path: str = "") -> Path:
    """Resolve a path below a task directory and reject traversal."""
    base = task_dir(task_id).resolve()
    candidate = (base / relative_path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="文件路径非法") from exc
    return candidate


@router.get("/{task_id}/folder-capability", summary="查询文件夹预览能力", dependencies=[Depends(require_api_token)])
def folder_capability(task_id: str, store: TaskStore = Depends(get_store)) -> dict:
    """告诉前端当前服务器是否有可用的图形化文件管理器。"""
    _require(store, task_id)
    graphical = sys.platform in {"darwin", "win32"} or bool(
        sys.platform.startswith("linux") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    )
    return {"mode": "system" if graphical else "browser"}


@router.get("/{task_id}/files", summary="浏览任务文件", dependencies=[Depends(require_api_token)])
def list_task_files(task_id: str, path: str = "", store: TaskStore = Depends(get_store)) -> dict:
    """返回任务目录下的文件列表，供无图形界面的 Linux 预览。"""
    _require(store, task_id)
    directory = _task_path(task_id, path)
    if not directory.exists() or not directory.is_dir():
        raise HTTPException(status_code=404, detail="目录不存在")
    base = task_dir(task_id).resolve()
    entries = []
    for item in sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        try:
            size = item.stat().st_size if item.is_file() else 0
        except OSError:
            size = 0
        entries.append({"name": item.name, "path": str(item.relative_to(base)), "directory": item.is_dir(), "size": size})
    return {"path": path, "entries": entries}


@router.get("/{task_id}/file", summary="读取任务文件", dependencies=[Depends(require_api_token)])
def get_task_file(task_id: str, path: str, store: TaskStore = Depends(get_store)):
    """提供浏览器预览所需的安全文件响应。"""
    _require(store, task_id)
    file_path = _task_path(task_id, path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    if file_path.suffix.lower() not in _BROWSER_PREVIEW_EXTENSIONS:
        raise HTTPException(status_code=415, detail="该文件类型暂不支持网页预览")
    return FileResponse(file_path)


def _open_folder(path: Path | str) -> None:
    """按当前系统选择文件管理器打开目录。"""
    abs_path = Path(path).resolve()
    if sys.platform == "darwin":
        cmd = ["open", str(abs_path)]
    elif sys.platform.startswith("win"):
        cmd = ["explorer", str(abs_path)]
    else:
        cmd = ["xdg-open", str(abs_path)]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail="当前系统不支持打开文件夹") from e


# ---------- SSE 进度 ----------

def _sse_payload(rec) -> str:
    data = {
        "id": rec.id,
        "status": rec.status,
        "progress": rec.progress,
        "currentStep": rec.current_step,
        "title": rec.title,
        "error": rec.error,
        "errorCode": rec.error_code,
        "resourceStatus": to_out(rec).resourceStatus,
        "outputs": to_out(rec).outputs,
    }
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/{task_id}/stream", dependencies=[Depends(require_api_token)])
def stream_progress(task_id: str, store: TaskStore = Depends(get_store)):
    """轮询库表并以 SSE 推送进度（含心跳保活、超时断流与终态事件）。"""
    _require(store, task_id)

    def gen():
        last = None
        start_time = time.time()
        last_sent = time.time()
        timeout_sec = max(1, settings.stream_timeout_sec)

        while True:
            rec = store.get(task_id)
            if rec is None:
                yield 'data: {"error":"任务不存在"}\n\n'
                return

            snapshot = (rec.status, rec.progress)
            now = time.time()

            if snapshot != last:
                if rec.status in _TERMINAL:
                    yield f"event: end\n{_sse_payload(rec)}"
                    return
                yield _sse_payload(rec)
                last = snapshot
                last_sent = now
            else:
                if rec.status in _TERMINAL:
                    yield f"event: end\n{_sse_payload(rec)}"
                    return
                elif now - last_sent >= 15:
                    yield ":keepalive\n\n"
                    last_sent = now

            if now - start_time >= timeout_sec:
                yield 'event: timeout\ndata: {"error":"stream timeout"}\n\n'
                return

            time.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")
