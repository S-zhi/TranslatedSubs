"""任务探测、资源治理和进度流业务实现。"""

from __future__ import annotations

import errno
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from src.handler.deps import get_store, require_api_token
from src.service.asset_resolver import AssetResolver, ResourceState
from src.store import TaskStore
from src.store import (
    DOWNGRADE_REASON_DISK_FAILURE,
    DOWNGRADE_REASON_UNKNOWN,
    DOWNGRADE_REASON_USER_CLEANED,
    DOWNGRADE_REASON_VOLUME_MIGRATED,
    RESOURCE_STATUS_MISSING,
)

logger = logging.getLogger(__name__)
TERMINAL = {"SUCCESS", "FAILED", "CANCELLED"}
DELETED_MESSAGE = "资源已删除"
router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def mark_resource_missing(
    store: Any,
    task_id: str,
    reason: str,
    downgrade_reason: str = DOWNGRADE_REASON_UNKNOWN,
    downgrade_errno: int | None = None,
) -> None:
    """幂等地把任务资源状态标记为 MISSING。"""

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


def scan_missing_terminal(
    store: Any,
    *,
    task_id: str | None = None,
    data_dir: str | Path | None = None,
    settings: Any,
    asset_resolver: Any = AssetResolver,
    downgrade_reason: str | None = None,
) -> list[str]:
    """扫描成功任务的磁盘产物，把丢失或不可读的任务降级。"""

    data_path = Path(data_dir if data_dir is not None else settings.data_dir)
    if task_id is not None:
        target = store.get(task_id)
        records = [target] if target is not None else []
    else:
        records = store.list()

    success_count = sum(1 for record in records if record.status == "SUCCESS")
    data_root_unavailable = False
    detected_reason: str | None = None
    detected_errno: int | None = None
    try:
        data_path.stat()
        list(data_path.iterdir())
    except FileNotFoundError:
        data_root_unavailable = True
    except NotADirectoryError as exc:
        logger.warning("scan_missing_terminal data_dir 异常: errno=%s msg=%s", exc.errno, exc)
        data_root_unavailable = True
        detected_reason = DOWNGRADE_REASON_USER_CLEANED
        detected_errno = exc.errno
    except PermissionError as exc:
        logger.warning("scan_missing_terminal data_dir 异常: errno=%s msg=%s", exc.errno, exc)
        data_root_unavailable = True
        detected_reason = DOWNGRADE_REASON_DISK_FAILURE
        detected_errno = exc.errno
    except OSError as exc:
        logger.warning("scan_missing_terminal data_dir 异常: errno=%s msg=%s", exc.errno, exc)
        data_root_unavailable = True
        detected_errno = exc.errno
        detected_reason = (
            DOWNGRADE_REASON_DISK_FAILURE
            if exc.errno in (errno.EIO, errno.ENXIO, errno.ESTALE)
            else DOWNGRADE_REASON_UNKNOWN
        )

    downgraded: list[str] = []
    for record in records:
        if record.status != "SUCCESS" or record.resource_status == RESOURCE_STATUS_MISSING:
            continue

        if record.need_subtitle:
            output_state = asset_resolver.resolve_output_video(record.id)[0]
            subtitle_state = asset_resolver.resolve_translated_srt(record.id)[0]
            if output_state == ResourceState.AVAILABLE and subtitle_state == ResourceState.AVAILABLE:
                continue
            unreadable = (
                output_state == ResourceState.UNREADABLE
                or subtitle_state == ResourceState.UNREADABLE
            )
        else:
            source_state = asset_resolver.resolve_source(record.id)[0]
            if source_state == ResourceState.AVAILABLE:
                continue
            unreadable = source_state == ResourceState.UNREADABLE

        error_message = "资源不可读" if unreadable else DELETED_MESSAGE
        audit_errno: int | None = None
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
            record.id,
            resource_status=RESOURCE_STATUS_MISSING,
            error=error_message,
            downgrade_reason=audit_reason,
            downgrade_errno=audit_errno,
            downgraded_at=int(time.time() * 1000),
        )
        downgraded.append(record.id)
    return downgraded


def probe_and_record(
    *,
    url: str,
    cookies_from_browser: bool,
    probes: Any,
    probe_video: Callable[..., Any],
    probe_batch_manager: Any,
    plugin_instances: dict[str, str] | None = None,
    quality: str | None = None,
) -> tuple[Any, Any]:
    """按下载实例执行一次探测，并持久化成功或失败记录。"""

    options = {"cookies_from_browser": cookies_from_browser}
    if plugin_instances is not None:
        options["plugin_instances"] = plugin_instances
    if quality is not None:
        options["quality"] = quality
    result = probe_video(url, **options)
    record = probes.record(
        url=url,
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
    if not (plugin_instances or {}).get("download"):
        probe_batch_manager.record_manual_probe(url, result, record.created_at)
    return result, record


def list_probe_records(*, limit: int, probes: Any) -> list[Any]:
    return probes.list(limit=limit)


def clear_probe_records(*, probes: Any) -> int:
    return probes.clear()


def delete_probe_record(*, record_id: str, probes: Any) -> bool:
    return bool(probes.delete(record_id))


def ytdlp_info(settings: Any) -> dict[str, Any]:
    """读取 yt-dlp 版本、extractor 数量和网络配置摘要。"""

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
    proxy_masked = re.sub(r"://[^@]+@", "://***:***@", str(proxy)) if proxy else None
    return {
        "version": version,
        "extractorsCount": extractors_count,
        "proxyConfigured": bool(proxy),
        "proxyMasked": proxy_masked,
        "cookiesConfigured": bool(
            settings.cookies_file and Path(settings.cookies_file).is_file()
        ),
        "browserCookiesEnabled": bool(settings.allow_cookies_from_browser),
        "cacheTtlSec": float(settings.probe_cache_ttl_sec),
    }


def sse_payload(rec: Any, *, to_out: Callable[[Any], Any]) -> str:
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


def progress_events(
    task_id: str,
    *,
    store: Any,
    settings: Any,
    to_out: Callable[[Any], Any],
    sleep: Callable[[float], None] | None = None,
) -> Iterable[str]:
    """生成任务 SSE 事件，包含心跳、终态和超时事件。"""

    last: tuple[str, int] | None = None
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
            if rec.status in TERMINAL:
                yield f"event: end\n{sse_payload(rec, to_out=to_out)}"
                return
            yield sse_payload(rec, to_out=to_out)
            last = snapshot
            last_sent = now
        elif rec.status in TERMINAL:
            yield f"event: end\n{sse_payload(rec, to_out=to_out)}"
            return
        elif now - last_sent >= 15:
            yield ":keepalive\n\n"
            last_sent = now

        if now - start_time >= timeout_sec:
            yield 'event: timeout\ndata: {"error":"stream timeout"}\n\n'
            return
        (sleep or time.sleep)(1)


@router.get("/{task_id}/stream", dependencies=[Depends(require_api_token)])
def stream_progress_route(task_id: str, store: TaskStore = Depends(get_store)):
    from src.handler import tasks as compat

    compat._require(store, task_id)
    events = compat._task_runtime.progress_events(
        task_id,
        store=store,
        settings=compat.settings,
        to_out=compat.to_out,
    )
    return StreamingResponse(events, media_type="text/event-stream")
