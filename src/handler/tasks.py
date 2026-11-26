"""Compatibility facade and router aggregation for task endpoints.

The FastAPI endpoints are implemented in ``src.service.task`` modules.  This
module intentionally keeps the historic import surface used by storage,
tests, and integrations while exposing the single aggregate router.
"""

from __future__ import annotations

import os

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
from src.core.downloader import ProbeResult as CoreProbeResult
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
from src.service.model_runtime import resolve_engine as _resolve_engine
from src.service.model_runtime import resolve_runtime as _resolve_runtime
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
from src.plugin_system import ProbeRequest, PluginCancelled, PluginError
from src.service.plugin_plan import parse_plugin_plan, resolve_plugin_defaults, resolve_plugin_plan
from src.plugins import get_plugin_manager
import uuid

router = _task.router

_BROWSER_PREVIEW_EXTENSIONS = _task._BROWSER_PREVIEW_EXTENSIONS
_TERMINAL = _task._TERMINAL
_DELETED_MESSAGE = _task._DELETED_MESSAGE
_UPLOAD_VIDEO_EXTS = _task._UPLOAD_VIDEO_EXTS


class _ProbeContext:
    """Read-only invocation context for URL probes outside a task record."""

    def __init__(self, credential_resolver):
        self._task_id = "probe"
        self._invocation_id = str(uuid.uuid4())
        self._task_dir = Path("/tmp/subtrans-plugin-probe")
        self._credential_resolver = credential_resolver

    @property
    def task_id(self):
        return self._task_id

    @property
    def invocation_id(self):
        return self._invocation_id

    @property
    def task_dir(self):
        return self._task_dir

    @property
    def purpose(self):
        return "download"

    def check_cancelled(self):
        return None

    def report_progress(self, percent):
        return None

    def register_process(self, proc):
        return None

    def unregister_process(self, proc):
        return None

    def resolve_credential(self, ref):
        return self._credential_resolver(ref)


def probe_video(
    url, *, cookies_from_browser=None, force_refresh=False,
    plugin_instances=None, quality=None,
):
    """Compatibility wrapper that runs the configured download plugin probe."""
    from src.config.config_repository import get_config_provider
    from src.config.config import settings_scope
    from src.service.plugin_selection import prepare_instance_selection, build_instance_plan

    snapshot = get_config_provider().read()
    selected = prepare_instance_selection(
        snapshot, plugin_instances, request_model="local:tiny", engine=DEFAULT_TRANSLATION_ENGINE_ID,
        tts_voice="auto", need_subtitle=False,
    )
    defaults = selected.plugin_defaults
    with settings_scope(settings.capture_business_snapshot(config_snapshot=snapshot)):
        raw_plan = resolve_plugin_plan(
            source_type="url", source_lang="auto", target_lang="zh-CN",
            mode="mono", burn="hard", model="local:tiny",
            engine=DEFAULT_TRANSLATION_ENGINE_ID, need_subtitle=False,
            quality=quality or "480p", tts_enabled=False, tts_voice="auto",
            original_voice_mode="keep", settings=settings,
            plugin_defaults=defaults,
        )
    slot = parse_plugin_plan(build_instance_plan(raw_plan, selected))["slots"]["download"]

    def resolve_credential(ref):
        allowed_refs = {
            value for key, value in slot["config"].items()
            if key in {"credential_ref", "cookies_ref"} and isinstance(value, str)
        }
        if ref not in allowed_refs:
            raise PluginError("credential_missing", "download", slot["plugin_id"], "credential is not authorized by this probe plan")
        if ref == "settings:cobalt_api_key":
            if slot["plugin_id"] == "builtin.cobalt" and slot["config"].get("credential_ref") == ref:
                value = getattr(settings, "cobalt_api_key", None)
                if isinstance(value, str) and value.strip():
                    return value
        if ref == "settings:cookies_file":
            value = getattr(settings, "cookies_file", None)
            if isinstance(value, (str, os.PathLike)):
                path = os.fspath(value)
                if path:
                    return path
        raise PluginError("credential_missing", "download", slot["plugin_id"], "credential is unavailable")

    context = _ProbeContext(resolve_credential)
    try:
        result = get_plugin_manager().run_probe(
            slot["plugin_id"], slot["implementation_version"],
            ProbeRequest(url=url, force_refresh=bool(force_refresh), cookies_from_browser=cookies_from_browser),
            slot["config"], context,
        )
    except PluginCancelled as exc:
        raise RuntimeError("probe cancelled") from exc
    formats = [
        {
            "formatId": item.formatId, "ext": item.ext, "resolution": item.resolution,
            "height": item.height, "width": item.width, "fps": item.fps,
            "vcodec": item.vcodec, "acodec": item.acodec, "filesize": item.filesize,
            "tbr": item.tbr, "note": item.note, "type": item.type,
        }
        for item in result.formats
    ]
    return CoreProbeResult(
        ok=result.ok, title=result.title, extractor=result.extractor,
        duration=result.duration, formats_count=result.formats_count,
        webpage_url=result.webpage_url, reason=result.reason, detail=result.detail,
        cached=result.cached, language=result.language,
        available_qualities=list(result.available_qualities), formats=formats,
        thumbnail=result.thumbnail, uploader=result.uploader,
    )

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
    # The shared resolver is the validation source for task creation.  Keep
    # this historic helper for callers/tests that invoke it directly.
    model_store = None
    if getattr(engines, "db_path", None) is not None:
        from src.store.model_settings_store import ModelSettingsStore
        model_store = ModelSettingsStore(engines.db_path)
    _resolve_engine(
        engine, need_subtitle, engines, model_store=model_store,
        compat_settings=settings,
        translation_model_manager=get_translation_model_manager(),
    )


def resolve_runtime(**kwargs):
    """Resolve and snapshot all model routing for a newly created task."""
    kwargs.setdefault("compat_settings", settings)
    selected = kwargs.get("plugin_defaults")
    builtin_translate = selected is None or selected["translate"]["plugin_id"] == "builtin.translation_engine"
    if kwargs.get("need_subtitle") and builtin_translate and "translation_model_manager" not in kwargs:
        engines = kwargs.get("engines")
        engine = engines.get(kwargs.get("engine")) if engines is not None else None
        if engine is not None and engine.api_type == "local_ct2":
            kwargs["translation_model_manager"] = get_translation_model_manager()
    if "model_store" not in kwargs:
        engines = kwargs.get("engines")
        # The model store is Redis-backed too. Only preserve a path when an
        # explicitly injected legacy engine store supplies one; never consult
        # settings.db_path (that now belongs to one-time bootstrap/migration).
        db_path = getattr(engines, "db_path", None)
        if db_path is not None:
            from src.store.model_settings_store import ModelSettingsStore
            kwargs["model_store"] = ModelSettingsStore(db_path)
    return _resolve_runtime(**kwargs)

# 确保支持的语言翻译
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


def _resolve_video(
    task_id: str,
    mode: str | None = None,
    *,
    allow_source_fallback: bool = True,
):
    return _task_artifacts.resolve_video(
        task_id,
        mode,
        task_dir=task_dir,
        output_video=OUTPUT_VIDEO,
        output_video_names=OUTPUT_VIDEO_NAMES,
        allow_source_fallback=allow_source_fallback,
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
