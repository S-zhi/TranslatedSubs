"""Compatibility facade and router aggregation for task endpoints.

The FastAPI endpoints are implemented in ``src.service.task`` modules.  This
module intentionally keeps the historic import surface used by storage,
tests, and integrations while exposing the single aggregate router.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from fastapi import HTTPException

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
from src.service.translation_model_manager import get_translation_model_manager
from src.service.task import task as _task
from src.service.task import task_artifacts as _task_artifacts
from src.service.task import task_lifecycle as _task_lifecycle
from src.service.task import task_runtime as _task_runtime
from src.store import (
    DOWNGRADE_REASON_DISK_FAILURE,
    DOWNGRADE_REASON_UNKNOWN,
    DEFAULT_TRANSLATION_ENGINE_ID,
    RESOURCE_STATUS_AVAILABLE,
    RESOURCE_STATUS_MISSING,
    ProbeStore,
    TaskStore,
    TranslationEngineStore,
)

router = _task.router

_BROWSER_PREVIEW_EXTENSIONS = _task._BROWSER_PREVIEW_EXTENSIONS
_TERMINAL = _task._TERMINAL
_DELETED_MESSAGE = _task._DELETED_MESSAGE
_UPLOAD_VIDEO_EXTS = _task._UPLOAD_VIDEO_EXTS

# Legacy endpoint names remain available as direct callables for existing
# tests and integrations.  Route registration itself happens only in service
# modules.
create_task = _task_lifecycle.create_task_route
create_upload_task = _task_lifecycle.create_upload_task_route
list_tasks = _task_lifecycle.list_tasks_route
get_task = _task_lifecycle.get_task_route
probe_task = _task_lifecycle.probe_task_route
get_probe_startup_status = _task_lifecycle.get_probe_startup_status_route
list_probe_records = _task_lifecycle.list_probe_records_route
clear_probe_records = _task_lifecycle.clear_probe_records_route
delete_probe_record = _task_lifecycle.delete_probe_record_route
get_ytdlp_info = _task_lifecycle.get_ytdlp_info_route
delete_task = _task_lifecycle.delete_task_route
cancel_task = _task_lifecycle.cancel_task_route
retry_task = _task_lifecycle.retry_task_route
check_source_video = _task_artifacts.check_source_video_route
download_source_video = _task_artifacts.download_source_video_route
check_download_video = _task_artifacts.check_download_video_route
download_video = _task_artifacts.download_video_route
check_dubbed_video = _task_artifacts.check_dubbed_video_route
download_dubbed_video = _task_artifacts.download_dubbed_video_route
download_subtitle = _task_artifacts.download_subtitle_route
open_task_folder = _task_artifacts.open_task_folder_route
folder_capability = _task_artifacts.folder_capability_route
list_task_files = _task_artifacts.list_task_files_route
get_task_file = _task_artifacts.get_task_file_route
stream_progress = _task_runtime.stream_progress_route


def _upload_error(
    status_code: int,
    *,
    code: str,
    message: str,
    limits: Optional[dict] = None,
    suggestion: Optional[str] = None,
) -> HTTPException:
    return _task._upload_error(
        status_code,
        code=code,
        message=message,
        limits=limits,
        suggestion=suggestion,
    )


def _require(store: TaskStore, task_id: str):
    return _task._require(store, task_id)


def _mark_resource_missing(
    store: TaskStore,
    task_id: str,
    reason: str,
    downgrade_reason: str = DOWNGRADE_REASON_UNKNOWN,
    downgrade_errno: Optional[int] = None,
) -> None:
    _task_runtime.mark_resource_missing(
        store,
        task_id,
        reason,
        downgrade_reason=downgrade_reason,
        downgrade_errno=downgrade_errno,
    )


_ensure_local_model_ready = _task._ensure_local_model_ready


def _ensure_translation_engine(
    engine: str,
    need_subtitle: bool,
    engines: TranslationEngineStore,
) -> None:
    # Delegate to shared implementation, which reads compat.settings and
    # compat.get_translation_model_manager dynamically.
    return _task._ensure_translation_engine(engine, need_subtitle, engines)


def _ensure_supported_translation_languages(
    engine: str,
    need_subtitle: bool,
    source_lang: str,
    target_lang: str,
) -> None:
    return _task._ensure_supported_translation_languages(
        engine, need_subtitle, source_lang, target_lang
    )


def scan_missing_terminal(
    store: TaskStore,
    *,
    task_id: Optional[str] = None,
    data_dir=None,
    downgrade_reason: Optional[str] = None,
) -> List[str]:
    return _task_runtime.scan_missing_terminal(
        store,
        task_id=task_id,
        data_dir=data_dir,
        settings=settings,
        downgrade_reason=downgrade_reason,
    )


def _resolve_video(task_id: str, mode: str | None = None):
    return _task_artifacts.resolve_video(
        task_id,
        mode,
        task_dir=task_dir,
        output_video=OUTPUT_VIDEO,
        output_video_names=OUTPUT_VIDEO_NAMES,
    )


def _task_path(task_id: str, relative_path: str = "") -> Path:
    try:
        return _task_artifacts.task_path(task_id, relative_path, task_dir=task_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="文件路径非法") from exc


def _open_folder(path: Path | str) -> None:
    try:
        _task_artifacts.open_folder(path, platform=sys.platform, popen=subprocess.Popen)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _sse_payload(rec) -> str:
    return _task_runtime.sse_payload(rec, to_out=to_out)


__all__ = [
    "router",
    "settings",
    "task_dir",
    "enqueue_pipeline",
    "probe_duration",
    "probe_video",
    "subprocess",
    "release_lock",
    "get_translation_model_manager",
    "probe_batch_manager",
    "create_task",
    "create_upload_task",
    "list_tasks",
    "get_task",
    "probe_task",
    "get_probe_startup_status",
    "list_probe_records",
    "clear_probe_records",
    "delete_probe_record",
    "get_ytdlp_info",
    "delete_task",
    "cancel_task",
    "retry_task",
    "check_source_video",
    "download_source_video",
    "check_download_video",
    "download_video",
    "check_dubbed_video",
    "download_dubbed_video",
    "download_subtitle",
    "open_task_folder",
    "folder_capability",
    "list_task_files",
    "get_task_file",
    "stream_progress",
    "_require",
    "_mark_resource_missing",
    "_ensure_local_model_ready",
    "_ensure_translation_engine",
    "_ensure_supported_translation_languages",
    "scan_missing_terminal",
    "_resolve_video",
    "_task_path",
    "_open_folder",
    "_sse_payload",
    "_DELETED_MESSAGE",
]
