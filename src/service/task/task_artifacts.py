"""任务产物和任务目录的访问逻辑。

这里处理文件系统定位、安全边界和目录枚举；HTTP 响应由 handler 构造。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response

from src.handler.deps import get_store, require_api_token
from src.store import (
    DOWNGRADE_REASON_DISK_FAILURE,
    RESOURCE_STATUS_AVAILABLE,
    TaskStore,
)
from src.service.asset_resolver import AssetResolver, ResourceState
from src.service.task.task import task_error_boundary

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

BROWSER_PREVIEW_EXTENSIONS = {
    ".txt", ".srt", ".vtt", ".json", ".md", ".log", ".csv",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mov",
}

class PathSafetyError(ValueError):
    """Requested path escapes the task directory."""

class UnsupportedPreviewError(ValueError):
    """File extension is not supported for browser preview."""

class TaskFolderError(ValueError):
    """Task folder identifier or containment is invalid."""

class TaskFolderMissingError(FileNotFoundError):
    """Task folder has not been created yet."""


def validate_task_id(task_id: str) -> None:
    if not task_id or not re.match(r"^task_[A-Za-z0-9_-]+$", task_id):
        raise TaskFolderError("task_id 格式不符合规范")


def resolve_video(
    task_id: str,
    mode: str | None,
    *,
    task_dir: Callable[[str], Path],
    output_video: str,
    output_video_names: tuple[str, ...],
    asset_resolver: Any = AssetResolver,
) -> Path | None:
    """优先定位成品视频，下载模式下回退到源视频。"""

    directory = task_dir(task_id)
    names = output_video_names
    if mode in ("hard", "soft"):
        preferred = "output_hard.mp4" if mode == "hard" else "output_soft.mp4"
        names = (preferred, output_video)
    for name in names:
        output = directory / name
        if output.is_file():
            return output
    state, source_path, _ = asset_resolver.resolve_source(task_id)
    if state == ResourceState.AVAILABLE and source_path is not None:
        return source_path
    return None


def task_path(task_id: str, relative_path: str, *, task_dir: Callable[[str], Path]) -> Path:
    """解析任务目录下的路径并拒绝目录穿越。"""

    base = task_dir(task_id).resolve()
    candidate = (base / relative_path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise PathSafetyError("文件路径非法") from exc
    return candidate


def folder_capability(*, platform: str = sys.platform, environ: Any = os.environ) -> dict[str, str]:
    graphical = platform in {"darwin", "win32"} or bool(
        platform.startswith("linux")
        and (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))
    )
    return {"mode": "system" if graphical else "browser"}


def open_folder(
    path: Path | str,
    *,
    platform: str = sys.platform,
    popen: Callable[..., Any] = subprocess.Popen,
) -> None:
    """按当前平台调用系统文件管理器。"""

    absolute_path = Path(path).resolve()
    if platform == "darwin":
        command = ["open", str(absolute_path)]
    elif platform.startswith("win"):
        command = ["explorer", str(absolute_path)]
    else:
        command = ["xdg-open", str(absolute_path)]
    try:
        popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise RuntimeError("当前系统不支持打开文件夹") from exc


def prepare_task_folder(task_id: str, *, task_dir: Callable[[str], Path], data_dir: Path) -> Path:
    validate_task_id(task_id)
    path = task_dir(task_id).resolve()
    try:
        if not path.is_relative_to(data_dir.resolve()):
            raise TaskFolderError("任务目录路径非法")
    except ValueError as exc:
        raise TaskFolderError("任务目录路径非法") from exc
    if not path.exists():
        raise TaskFolderMissingError("任务目录尚未生成")
    return path


def list_files(
    task_id: str,
    relative_path: str,
    *,
    task_dir: Callable[[str], Path],
) -> list[dict[str, Any]]:
    directory = task_path(task_id, relative_path, task_dir=task_dir)
    if not directory.exists() or not directory.is_dir():
        raise FileNotFoundError("目录不存在")
    base = task_dir(task_id).resolve()
    entries: list[dict[str, Any]] = []
    for item in sorted(directory.iterdir(), key=lambda path: (path.is_file(), path.name.lower())):
        try:
            size = item.stat().st_size if item.is_file() else 0
        except OSError:
            size = 0
        entries.append(
            {
                "name": item.name,
                "path": str(item.relative_to(base)),
                "directory": item.is_dir(),
                "size": size,
            }
        )
    return entries


def preview_file(
    task_id: str,
    relative_path: str,
    *,
    task_dir: Callable[[str], Path],
    allowed_extensions: set[str] = BROWSER_PREVIEW_EXTENSIONS,
) -> Path:
    file_path = task_path(task_id, relative_path, task_dir=task_dir)
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError("文件不存在")
    if file_path.suffix.lower() not in allowed_extensions:
        raise UnsupportedPreviewError("该文件类型暂不支持网页预览")
    return file_path


def resolve_source(task_id: str, *, asset_resolver: Any = AssetResolver) -> tuple[Any, Path | None, str]:
    return asset_resolver.resolve_source(task_id)


def resolve_dubbed(task_id: str, filename: str | None, *, task_dir: Callable[[str], Path]) -> Path:
    return task_dir(task_id) / (filename or "output_dubbed.mp4")


def check_file(path: Path, *, asset_resolver: Any = AssetResolver) -> Any:
    return asset_resolver.check_file_state(path)


def resolve_subtitle(task_id: str, filename: str, *, task_dir: Callable[[str], Path]) -> Path:
    return task_dir(task_id) / filename


def download_source(*, task_id: str, store: Any, task_dir: Callable[[str], Path], asset_resolver: Any = AssetResolver) -> Path:
    require = store.get(task_id)
    if require is None:
        raise LookupError("任务不存在")
    state, path, message = asset_resolver.resolve_source(task_id)
    if state == ResourceState.AVAILABLE and path is not None:
        return path
    raise RuntimeError(message)


def download_video(*, task_id: str, store: Any, resolve_video_fn: Callable[..., Path | None], mark_missing: Callable[..., None], asset_resolver: Any = AssetResolver, available_status: str, missing_downgrade_reason: Any = None) -> Path:
    rec = store.get(task_id)
    if rec is None:
        raise LookupError("任务不存在")
    if rec.status != "SUCCESS":
        raise RuntimeError("成品视频尚未生成")
    path = resolve_video_fn(task_id, rec.burn)
    if path is not None:
        state = asset_resolver.check_file_state(path)
        if state == ResourceState.AVAILABLE:
            return path
        if state == ResourceState.UNREADABLE:
            if rec.resource_status == available_status:
                if missing_downgrade_reason is None:
                    mark_missing(store, task_id, "资源不可读")
                else:
                    mark_missing(store, task_id, "资源不可读", missing_downgrade_reason)
            raise RuntimeError("资源不可读")
    if rec.resource_status == available_status:
        mark_missing(store, task_id, "资源已删除")
    raise RuntimeError("资源已删除")


def download_dubbed(*, task_id: str, store: Any, task_dir: Callable[[str], Path], asset_resolver: Any = AssetResolver) -> Path:
    rec = store.get(task_id)
    if rec is None:
        raise LookupError("任务不存在")
    if rec.status != "SUCCESS" or rec.tts_status != "SUCCESS":
        raise RuntimeError("配音视频尚未生成")
    path = task_dir(task_id) / (rec.output_dubbed_video or "output_dubbed.mp4")
    if asset_resolver.check_file_state(path) != ResourceState.AVAILABLE:
        raise RuntimeError("配音视频不可用")
    return path


def download_subtitle(*, task_id: str, store: Any, task_dir: Callable[[str], Path], mark_missing: Callable[..., None], asset_resolver: Any = AssetResolver, available_status: str, missing_downgrade_reason: Any = None) -> Path:
    rec = store.get(task_id)
    if rec is None:
        raise LookupError("任务不存在")
    path = task_dir(task_id) / "translated.srt"
    state = asset_resolver.check_file_state(path)
    if state == ResourceState.AVAILABLE:
        return path
    if state == ResourceState.UNREADABLE:
        if rec.status == "SUCCESS" and rec.resource_status == available_status:
            if missing_downgrade_reason is None:
                mark_missing(store, task_id, "资源不可读")
            else:
                mark_missing(store, task_id, "资源不可读", missing_downgrade_reason)
        raise RuntimeError("资源不可读")
    if rec.status == "SUCCESS" and rec.resource_status == available_status:
        mark_missing(store, task_id, "资源已删除")
    raise RuntimeError("资源已删除" if rec.status == "SUCCESS" else "译文字幕尚未生成")


@router.head("/{task_id}/source", status_code=204, dependencies=[Depends(require_api_token)])
def check_source_video_route(task_id: str, store: TaskStore = Depends(get_store)):
    from src.handler import tasks as compat

    compat.download_source_video(task_id, store)
    return Response(status_code=204)


@router.get("/{task_id}/source", dependencies=[Depends(require_api_token)])
@task_error_boundary
def download_source_video_route(task_id: str, store: TaskStore = Depends(get_store)):
    from src.handler import tasks as compat

    path = compat._task_artifacts.download_source(task_id=task_id, store=store, task_dir=compat.task_dir)
    return FileResponse(path, filename=f"{task_id}-source{path.suffix}")


@router.head("/{task_id}/download", status_code=204, dependencies=[Depends(require_api_token)])
def check_download_video_route(task_id: str, store: TaskStore = Depends(get_store)):
    from src.handler import tasks as compat

    compat.download_video(task_id, store)
    return Response(status_code=204)


@router.get("/{task_id}/download", dependencies=[Depends(require_api_token)])
@task_error_boundary
def download_video_route(task_id: str, store: TaskStore = Depends(get_store)):
    from src.handler import tasks as compat

    path = compat._task_artifacts.download_video(
        task_id=task_id,
        store=store,
        resolve_video_fn=compat._resolve_video,
        mark_missing=compat._mark_resource_missing,
        available_status=RESOURCE_STATUS_AVAILABLE,
        missing_downgrade_reason=DOWNGRADE_REASON_DISK_FAILURE,
    )
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.head("/{task_id}/dubbed", status_code=204, dependencies=[Depends(require_api_token)])
def check_dubbed_video_route(task_id: str, store: TaskStore = Depends(get_store)):
    from src.handler import tasks as compat

    compat.download_dubbed_video(task_id, store)
    return Response(status_code=204)


@router.get("/{task_id}/dubbed", dependencies=[Depends(require_api_token)])
@task_error_boundary
def download_dubbed_video_route(task_id: str, store: TaskStore = Depends(get_store)):
    from src.handler import tasks as compat

    path = compat._task_artifacts.download_dubbed(task_id=task_id, store=store, task_dir=compat.task_dir)
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.get("/{task_id}/subtitle", dependencies=[Depends(require_api_token)])
@task_error_boundary
def download_subtitle_route(task_id: str, store: TaskStore = Depends(get_store)):
    from src.handler import tasks as compat

    path = compat._task_artifacts.download_subtitle(
        task_id=task_id,
        store=store,
        task_dir=compat.task_dir,
        mark_missing=compat._mark_resource_missing,
        available_status=RESOURCE_STATUS_AVAILABLE,
        missing_downgrade_reason=DOWNGRADE_REASON_DISK_FAILURE,
    )
    return FileResponse(path, media_type="application/x-subrip", filename=f"{task_id}.srt")


@router.post("/{task_id}/folder", summary="打开任务文件夹", dependencies=[Depends(require_api_token)])
def open_task_folder_route(task_id: str, store: TaskStore = Depends(get_store)) -> dict:
    from src.handler import tasks as compat

    try:
        compat._task_artifacts.validate_task_id(task_id)
    except compat._task_artifacts.TaskFolderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    compat._require(store, task_id)
    try:
        path = compat._task_artifacts.prepare_task_folder(task_id, task_dir=compat.task_dir, data_dir=compat.settings.data_dir)
    except compat._task_artifacts.TaskFolderMissingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except compat._task_artifacts.TaskFolderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        compat._task_artifacts.open_folder(path, platform=compat.sys.platform, popen=compat.subprocess.Popen)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/{task_id}/folder-capability", summary="查询文件夹预览能力", dependencies=[Depends(require_api_token)])
def folder_capability_route(task_id: str, store: TaskStore = Depends(get_store)) -> dict:
    from src.handler import tasks as compat

    compat._require(store, task_id)
    return compat._task_artifacts.folder_capability(platform=compat.sys.platform)


@router.get("/{task_id}/files", summary="浏览任务文件", dependencies=[Depends(require_api_token)])
def list_task_files_route(task_id: str, path: str = "", store: TaskStore = Depends(get_store)) -> dict:
    from src.handler import tasks as compat

    compat._require(store, task_id)
    try:
        entries = compat._task_artifacts.list_files(task_id, path, task_dir=compat.task_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"path": path, "entries": entries}


@router.get("/{task_id}/file", summary="读取任务文件", dependencies=[Depends(require_api_token)])
def get_task_file_route(task_id: str, path: str, store: TaskStore = Depends(get_store)):
    from src.handler import tasks as compat

    compat._require(store, task_id)
    try:
        file_path = compat._task_artifacts.preview_file(task_id, path, task_dir=compat.task_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except compat._task_artifacts.PathSafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except compat._task_artifacts.UnsupportedPreviewError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    return FileResponse(file_path)
