"""本地资源治理路由（业务域：storage）。

提供总占用 / 任务产物分布统计、按条件预览可清理内容、执行安全清理，
以及简化的保留策略配置。清理动作复用 delete_task 路径，强制跳过 RUNNING
任务并在执行前再次校验状态，避免误删；保留策略可显式只清理产物并保留任务记录。
"""

from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
import shutil
import threading
import time
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.config import (
    AUDIO_FILENAME,
    OUTPUT_VIDEO,
    OUTPUT_VIDEO_HARD,
    OUTPUT_VIDEO_SOFT,
    ORIGINAL_SRT,
    SOURCE_VIDEO_STEM,
    TRANSLATED_SRT,
    settings,
    task_dir,
)
from src.handler.deps import get_probe_store, get_store, require_api_token
from src.handler.subtitle_editor import release_lock
from src.handler.tasks import _DELETED_MESSAGE, _mark_resource_missing, scan_missing_terminal
from src.service.drive_sync import (
    DriveSyncConflict,
    DriveSyncError,
    get_drive_sync_manager,
)
from src.store import DOWNGRADE_REASON_USER_CLEANED, ProbeStore, TaskStore, TaskRecord

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/storage", tags=["storage"])
DEFAULT_RETENTION_DAYS = 30


class TokenBucketRateLimiter:
    """按 Client IP 限流的内存 Token Bucket 实现。"""

    def __init__(
        self,
        capacity: int = 10,
        fill_rate_per_sec: float = 10.0 / 60.0,
        max_buckets: int = 1000,
    ) -> None:
        self.capacity = capacity
        self.fill_rate = fill_rate_per_sec
        self.max_buckets = max_buckets
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()  # ip -> (tokens, last_time)
        self._lock = threading.Lock()

    def check(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            tokens, last_time = self._buckets.pop(ip, (float(self.capacity), now))
            delta = now - last_time
            tokens = min(float(self.capacity), tokens + delta * self.fill_rate)

            allowed = False
            if tokens >= 1.0:
                tokens -= 1.0
                allowed = True

            if tokens < float(self.capacity):
                self._buckets[ip] = (tokens, now)

            if len(self._buckets) > self.max_buckets:
                expired_ips = [
                    k for k, (t, ts) in self._buckets.items()
                    if t + (now - ts) * self.fill_rate >= self.capacity
                ]
                for k in expired_ips:
                    del self._buckets[k]

                while len(self._buckets) > self.max_buckets:
                    self._buckets.popitem(last=False)

            return allowed

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


_cleanup_limiter = TokenBucketRateLimiter(capacity=10, fill_rate_per_sec=10.0 / 60.0)


def rate_limit_cleanup(request: Request) -> None:
    ip = request.client.host if request.client else "127.0.0.1"
    if not _cleanup_limiter.check(ip):
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后再试",
        )

# 运行中（流水线占用文件）的状态集合。治理时一律跳过，防止误删
# pipeline 正在读写 / 重新入队需要的中间产物。
_RUNNING_STATUSES = {
    "PENDING",
    "DOWNLOADING",
    "EXTRACTING",
    "TRANSCRIBING",
    "TRANSLATING",
    "BURNING",
    "SYNTHESIZING",
    "DUBBING",
}

# 产物类别：固定文件名映射到显示名/可清理性。其它文件归到 other，
# 默认按"可清理"处理（管理员主动操作即可）。
_ARTIFACT_KINDS = (
    "source",
    "audio",
    "original_srt",
    "translated_srt",
    "output",
    "other",
)


# ---------- Pydantic ----------

class StorageArtifact(BaseModel):
    """单个任务下某类产物的占用。"""

    kind: str
    name: str
    size: int


class TaskStorage(BaseModel):
    """单个任务占用的资源概览。"""

    taskId: str
    title: Optional[str] = None
    status: str
    size: int
    artifactCount: int
    artifacts: List[StorageArtifact]
    skipped: bool = False  # 治理时被跳过（如 RUNNING）


class StorageStats(BaseModel):
    """全量存储统计。"""

    totalBytes: int
    totalTasks: int
    runnableTaskCount: int  # 仍可清理的任务数（不含 RUNNING）
    byKind: dict[str, int]
    byTask: List[TaskStorage]


class CleanupPreviewRequest(BaseModel):
    """预览 / 执行清理的筛选条件（全部可选，全空表示匹配所有任务）。"""

    taskIds: Optional[List[str]] = None      # 指定任务 id 列表
    kinds: Optional[List[str]] = None         # 产物类别（source/audio/.../other）
    olderThanDays: Optional[int] = Field(default=None, ge=0)  # 任务创建超过 N 天
    cleanupProbeRecordsOlderThanDays: Optional[int] = Field(default=None, ge=0)  # 清理 N 天前的 probe 记录
    preserveTaskRecords: bool = False        # 仅清理产物，保留任务数据库记录


class CleanupPreviewResponse(BaseModel):
    matchedTasks: int
    matchedBytes: int
    skippedTasks: List[TaskStorage]  # 因 RUNNING 状态被跳过的任务
    targets: List[TaskStorage]       # 将被处理的任务（产物可能按 kind 过滤）
    deletedProbeRecords: int = 0     # 将被清理的 probe 记录数量


class CleanupResponse(BaseModel):
    """执行清理后的结果。"""

    deletedTasks: int
    deletedBytes: int
    skippedTasks: List[TaskStorage]
    partial: List[TaskStorage]  # 仅删了部分产物但任务被保留
    deletedProbeRecords: int = 0  # 实际删除的 probe 记录数量
    note: Optional[str] = None  # 结果说明（范围与桥接说明）


class RetentionIn(BaseModel):
    """产物保留策略。days = None 表示不限。"""

    days: Optional[int] = Field(default=None, ge=0)


class RetentionOut(BaseModel):
    days: Optional[int] = DEFAULT_RETENTION_DAYS
    updatedAt: Optional[int] = None
    lastRunAt: Optional[int] = None


class DriveSyncRequest(BaseModel):
    """任务级 Drive 同步请求；任务 ID 位于 URL，避免使用任务标题。"""

    artifactNames: Optional[List[str]] = None


# ---------- 保留策略配置（轻量：落到本地 JSON） ----------
# Issue 描述允许"简化版即可"；用 settings.data_dir/.retention.json 持久化，
# 避免拉新依赖/库表。

_RETENTION_FILE = ".retention.json"


def _retention_path() -> Path:
    return settings.data_dir / _RETENTION_FILE


def _load_retention() -> RetentionOut:
    p = _retention_path()
    if not p.exists():
        return RetentionOut()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return RetentionOut()
    if not isinstance(data, dict):
        return RetentionOut()
    days = data["days"] if "days" in data else DEFAULT_RETENTION_DAYS
    return RetentionOut(
        days=days,
        updatedAt=data.get("updatedAt"),
        lastRunAt=data.get("lastRunAt"),
    )


def _save_retention(out: RetentionOut) -> None:
    p = _retention_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "days": out.days,
                "updatedAt": out.updatedAt,
                "lastRunAt": out.lastRunAt,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ---------- 工具 ----------

def _format_bytes(n: int) -> str:
    """按人类可读格式化字节数。"""
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    if i == 0 or v >= 10:
        return f"{int(round(v))} {units[i]}"
    return f"{v:.1f} {units[i]}"


def _build_cleanup_note(
    probe_requested: bool,
    deleted_probes: int,
    deleted_tasks: int,
    partial_count: int,
    deleted_bytes: int,
    preserve_task_records: bool = False,
) -> str:
    """构建清理结果桥接说明 note 字段。"""
    has_task_changes = deleted_tasks > 0 or partial_count > 0 or deleted_bytes > 0

    if not has_task_changes:
        if probe_requested:
            if deleted_probes > 0:
                return f"本次清理仅作用于 probe 记录（已清理 {deleted_probes} 条），任务产物未动"
            else:
                return "本次清理仅作用于 probe 记录（未发现过期记录），任务产物未动"
        else:
            return "未发现符合条件的待清理任务产物"

    task_desc_parts = []
    if deleted_tasks > 0:
        task_desc_parts.append(f"{deleted_tasks} 个任务")
    if partial_count > 0:
        task_desc_parts.append(f"{partial_count} 个任务的部分产物")
    task_desc = "及".join(task_desc_parts)

    bytes_str = _format_bytes(deleted_bytes)

    if preserve_task_records:
        task_desc = f"{partial_count} 个任务的产物" if partial_count > 0 else "任务产物"
        if not probe_requested:
            return f"本次清理 {task_desc}（释放 {bytes_str}），任务记录已保留"
        if deleted_probes > 0:
            return f"本次清理 {task_desc}（释放 {bytes_str}）及 {deleted_probes} 条 probe 记录，任务记录已保留"
        return f"本次清理 {task_desc}（释放 {bytes_str}），probe 记录未变动，任务记录已保留"

    if not probe_requested:
        return f"本次清理 {task_desc}（释放 {bytes_str}），未触发 probe 记录清理"

    if deleted_probes > 0:
        return f"本次清理 {task_desc}（释放 {bytes_str}）及 {deleted_probes} 条 probe 记录"
    else:
        return f"本次清理 {task_desc}（释放 {bytes_str}），probe 记录未变动"


def _classify(name: str) -> str:
    """按文件名归类到 _ARTIFACT_KINDS 之一。"""
    low = name.lower()
    if low.startswith(SOURCE_VIDEO_STEM + "."):
        return "source"
    if low == AUDIO_FILENAME.lower():
        return "audio"
    if low == ORIGINAL_SRT.lower():
        return "original_srt"
    if low == TRANSLATED_SRT.lower():
        return "translated_srt"
    if low in {OUTPUT_VIDEO.lower(), OUTPUT_VIDEO_HARD.lower(), OUTPUT_VIDEO_SOFT.lower()}:
        return "output"
    return "other"


def _dir_size(path: Path) -> int:
    """累计目录下所有常规文件的大小；不存在的目录返回 0。"""
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                # 文件可能在遍历过程中被删除，忽略即可
                continue
    return total


def _scan_artifacts(task_id: str) -> List[StorageArtifact]:
    """列出某任务目录下的所有常规文件并按类别汇总。"""
    d = task_dir(task_id)
    if not d.exists():
        return []
    artifacts: List[StorageArtifact] = []
    for child in sorted(d.iterdir()):
        if child.is_file():
            try:
                size = child.stat().st_size
            except OSError:
                continue
            artifacts.append(StorageArtifact(
                kind=_classify(child.name), name=child.name, size=size,
            ))
    return artifacts


def _age_days(rec: TaskRecord, now_ms: int) -> float:
    return max(0.0, (now_ms - (rec.created_at or now_ms)) / 1000 / 86400)


def _matches(rec: TaskRecord, body: CleanupPreviewRequest, now_ms: int) -> bool:
    if body.taskIds is not None and rec.id not in body.taskIds:
        return False
    if body.olderThanDays is not None and _age_days(rec, now_ms) < body.olderThanDays:
        return False
    return True


def _filter_kinds(artifacts: List[StorageArtifact], kinds: Optional[List[str]]) -> List[StorageArtifact]:
    if not kinds:
        return artifacts
    allow = set(kinds)
    return [a for a in artifacts if a.kind in allow]


def _collect_targets(
    store: TaskStore, body: CleanupPreviewRequest,
) -> tuple[List[TaskRecord], List[TaskRecord], dict[str, List[StorageArtifact]]]:
    """按筛选条件收集：candidate 任务、按 kind 过滤后的 artifact 列表、RUNNING 任务。"""
    now_ms = int(time.time() * 1000)
    candidates: List[TaskRecord] = []
    skipped: List[TaskRecord] = []
    artifacts_by_task: dict[str, List[StorageArtifact]] = {}
    for rec in store.list():
        if not _matches(rec, body, now_ms):
            continue
        if rec.status in _RUNNING_STATUSES:
            skipped.append(rec)
            continue
        candidates.append(rec)
        artifacts_by_task[rec.id] = _filter_kinds(_scan_artifacts(rec.id), body.kinds)
    return candidates, skipped, artifacts_by_task


def _build_task_storage(
    rec: TaskRecord, artifacts: List[StorageArtifact], *, skipped: bool = False,
) -> TaskStorage:
    return TaskStorage(
        taskId=rec.id,
        title=rec.title,
        status=rec.status,
        size=sum(a.size for a in artifacts),
        artifactCount=len(artifacts),
        artifacts=artifacts,
        skipped=skipped,
    )


# ---------- 端点 ----------


def _require_drive_task(task_id: str, store: TaskStore) -> TaskRecord:
    if not task_id or Path(task_id).name != task_id or "/" in task_id or "\\" in task_id:
        raise HTTPException(status_code=400, detail="task_id 必须是安全的唯一任务 ID")
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if record.status in _RUNNING_STATUSES:
        raise HTTPException(status_code=409, detail="运行中的任务不能同步本地产物")
    return record


def _drive_artifact_snapshot(task_id: str, body: DriveSyncRequest) -> list[dict[str, object]]:
    artifacts = _scan_artifacts(task_id)
    if body.artifactNames:
        selected = set(body.artifactNames)
        artifacts = [artifact for artifact in artifacts if artifact.name in selected]
    return [artifact.model_dump() for artifact in artifacts]


@router.post(
    "/tasks/{task_id}/drive/upload",
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
def start_drive_upload(
    task_id: str,
    body: DriveSyncRequest = DriveSyncRequest(),
    store: TaskStore = Depends(get_store),
) -> dict:
    """异步、逐产物上传任务目录到 task_id 对应的 Drive 文件夹。"""
    _require_drive_task(task_id, store)
    snapshot = _drive_artifact_snapshot(task_id, body)
    if not snapshot:
        raise HTTPException(status_code=400, detail="任务没有可上传的产物")
    try:
        batch = get_drive_sync_manager().start_upload(task_id, snapshot, store)
    except DriveSyncConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DriveSyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return batch.to_payload()


@router.post(
    "/tasks/{task_id}/drive/download",
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
def start_drive_download(
    task_id: str,
    _body: DriveSyncRequest = DriveSyncRequest(),
    store: TaskStore = Depends(get_store),
) -> dict:
    """异步、逐产物从 task_id 对应的 Drive 文件夹恢复到本地任务目录。"""
    _require_drive_task(task_id, store)
    try:
        batch = get_drive_sync_manager().start_download(task_id, store)
    except DriveSyncConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DriveSyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return batch.to_payload()


@router.get(
    "/drive/batches/{batch_id}",
    dependencies=[Depends(require_api_token)],
)
def get_drive_batch(batch_id: str) -> dict:
    batch = get_drive_sync_manager().get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Drive 同步批次不存在")
    return batch.to_payload()


@router.post(
    "/drive/batches/{batch_id}/retry",
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
def retry_drive_batch(
    batch_id: str,
    store: TaskStore = Depends(get_store),
) -> dict:
    try:
        batch = get_drive_sync_manager().retry(batch_id, store)
    except DriveSyncError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return batch.to_payload()


@router.post(
    "/drive/batches/{batch_id}/cancel",
    dependencies=[Depends(require_api_token)],
)
def cancel_drive_batch(batch_id: str) -> dict:
    try:
        batch = get_drive_sync_manager().cancel(batch_id)
    except DriveSyncError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return batch.to_payload()

@router.get("/stats", response_model=StorageStats, dependencies=[Depends(require_api_token)])
def get_stats(store: TaskStore = Depends(get_store)) -> StorageStats:
    """全量统计：总占用、类别分布、按任务的占用排行。"""
    by_kind: dict[str, int] = {k: 0 for k in _ARTIFACT_KINDS}
    by_task: List[TaskStorage] = []
    total = 0
    runnable = 0
    for rec in store.list():
        artifacts = _scan_artifacts(rec.id)
        size = sum(a.size for a in artifacts)
        total += size
        for a in artifacts:
            by_kind[a.kind] = by_kind.get(a.kind, 0) + a.size
        if rec.status not in _RUNNING_STATUSES:
            runnable += 1
        by_task.append(_build_task_storage(rec, artifacts))
    # 按占用降序，方便前端直接展示
    by_task.sort(key=lambda t: t.size, reverse=True)
    return StorageStats(
        totalBytes=total,
        totalTasks=len(by_task),
        runnableTaskCount=runnable,
        byKind=by_kind,
        byTask=by_task,
    )


@router.post("/cleanup_preview", response_model=CleanupPreviewResponse, dependencies=[Depends(require_api_token)])
def cleanup_preview(
    body: CleanupPreviewRequest,
    store: TaskStore = Depends(get_store),
    probes: ProbeStore = Depends(get_probe_store),
) -> CleanupPreviewResponse:
    """预览将受影响的任务与产物。"""
    candidates, skipped, artifacts_by_task = _collect_targets(store, body)
    targets: List[TaskStorage] = []
    matched = 0
    matched_bytes = 0
    for rec in candidates:
        artifacts = artifacts_by_task[rec.id]
        size = sum(a.size for a in artifacts)
        if size == 0 and not artifacts:
            # 没有任何产物可清，跳过；保留空记录则不显示
            continue
        matched += 1
        matched_bytes += size
        targets.append(_build_task_storage(rec, artifacts))

    probe_count = 0
    if body.cleanupProbeRecordsOlderThanDays is not None:
        cutoff = int(time.time() * 1000) - body.cleanupProbeRecordsOlderThanDays * 86400 * 1000
        probe_count = len([r for r in probes.list(limit=0) if r.created_at < cutoff])

    return CleanupPreviewResponse(
        matchedTasks=matched,
        matchedBytes=matched_bytes,
        skippedTasks=[_build_task_storage(r, _scan_artifacts(r.id), skipped=True) for r in skipped],
        targets=targets,
        deletedProbeRecords=probe_count,
    )


def execute_cleanup(
    body: CleanupPreviewRequest,
    store: TaskStore,
    probes: ProbeStore,
) -> CleanupResponse:
    """核心清理逻辑，可由 API 端点或保留策略调度器复用。"""
    candidates, skipped, artifacts_by_task = _collect_targets(store, body)
    skipped_storage = [
        _build_task_storage(r, _scan_artifacts(r.id), skipped=True) for r in skipped
    ]

    deleted_tasks = 0
    deleted_bytes = 0
    partial: List[TaskStorage] = []

    for rec in candidates:
        # 二次校验：状态可能在预览与执行之间发生变化
        current = store.get(rec.id)
        if current is None:
            continue
        if current.status in _RUNNING_STATUSES:
            skipped_storage.append(
                _build_task_storage(current, _scan_artifacts(current.id), skipped=True)
            )
            continue
        artifacts = artifacts_by_task[rec.id]
        d = task_dir(rec.id)
        if not d.exists():
            # 目录不存在，统计上记为 0；视为一个空任务被清理
            if not artifacts:
                continue

        # kind 为空时默认是整任务清理（删除目录 + 记录）；自动保留策略
        # 显式设置 preserveTaskRecords，确保只删产物并保留任务记录。
        full_task_cleanup = not body.kinds and not body.preserveTaskRecords
        if full_task_cleanup:
            freed = _dir_size(d)
            # 联动降级：删除记录前先将 resource_status 设为 MISSING
            _mark_resource_missing(
                store, rec.id, _DELETED_MESSAGE, DOWNGRADE_REASON_USER_CLEANED
            )
            # 复用 delete_task 同等的清理动作：删记录 + 删目录
            store.delete(rec.id)
            shutil.rmtree(d, ignore_errors=True)
            release_lock(rec.id)
            deleted_tasks += 1
            deleted_bytes += freed
            continue

        # 部分清理：按文件删除指定类别
        removed: List[StorageArtifact] = []
        for a in artifacts:
            fp = d / a.name
            try:
                if fp.exists() and fp.is_file():
                    fp.unlink()
                    removed.append(a)
            except OSError as e:
                logger.warning("删除产物失败 %s: %s", fp, e)
        if removed:
            # 单任务联动降级检测：如果有文件被删除，触发 scan_missing_terminal
            scan_missing_terminal(
                store,
                task_id=rec.id,
                downgrade_reason=DOWNGRADE_REASON_USER_CLEANED,
            )
            # 产物全部清理后移除空目录；任务记录仍保留在数据库中。
            try:
                if d.exists() and not any(d.iterdir()):
                    d.rmdir()
            except OSError as e:
                logger.debug("清理空任务目录失败 %s: %s", d, e)
            # 任务保留在 DB，仅记录 partial（剩余的产物可能已被清空），使用更新后的记录
            updated = store.get(rec.id) or current
            remaining = _scan_artifacts(rec.id)
            partial.append(_build_task_storage(updated, remaining))
            deleted_bytes += sum(a.size for a in removed)
            release_lock(rec.id)

    deleted_probes = 0
    if body.cleanupProbeRecordsOlderThanDays is not None:
        deleted_probes = probes.cleanup_older_than_days(body.cleanupProbeRecordsOlderThanDays)

    note = _build_cleanup_note(
        probe_requested=body.cleanupProbeRecordsOlderThanDays is not None,
        deleted_probes=deleted_probes,
        deleted_tasks=deleted_tasks,
        partial_count=len(partial),
        deleted_bytes=deleted_bytes,
        preserve_task_records=body.preserveTaskRecords,
    )

    return CleanupResponse(
        deletedTasks=deleted_tasks,
        deletedBytes=deleted_bytes,
        skippedTasks=skipped_storage,
        partial=partial,
        deletedProbeRecords=deleted_probes,
        note=note,
    )


@router.post(
    "/cleanup",
    response_model=CleanupResponse,
    dependencies=[Depends(require_api_token), Depends(rate_limit_cleanup)],
)
def cleanup(
    body: CleanupPreviewRequest,
    store: TaskStore = Depends(get_store),
    probes: ProbeStore = Depends(get_probe_store),
) -> CleanupResponse:
    """执行清理。复用 delete_task 风格的清理路径，但允许仅删指定类别产物。

    安全策略：
      1. RUNNING 状态任务一律跳过，并在响应中返回；
      2. 真正落盘前再次校验当前状态（防止预览后到执行之间状态变化）。
    """
    return execute_cleanup(body, store, probes)


@router.get("/retention", response_model=RetentionOut, dependencies=[Depends(require_api_token)])
def get_retention() -> RetentionOut:
    """读取产物保留策略（days=None 表示不限，未配置时默认为 30 天）。"""
    return _load_retention()


@router.put(
    "/retention",
    response_model=RetentionOut,
    dependencies=[Depends(require_api_token)],
)
def put_retention(body: RetentionIn) -> RetentionOut:
    """写入产物保留策略。自动清理只删除产物并保留任务记录。"""
    current = _load_retention()
    out = RetentionOut(
        days=body.days,
        updatedAt=int(time.time() * 1000),
        lastRunAt=current.lastRunAt,
    )
    _save_retention(out)

    if body.days is not None and body.days > 0:
        from src.service.retention_scheduler import trigger_retention_cleanup_async
        trigger_retention_cleanup_async()

    return out
