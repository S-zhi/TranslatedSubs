"""本地产物与 Google Drive 之间的按任务串行同步。

浏览器只负责发起批次和查看进度；本模块在 Python 服务侧读取任务目录，
通过本机 Google Drive sidecar 的 HTTP 接口逐个传输文件。这样不会把大文件
先完整读入浏览器，也不会把任务名称当作 Drive 文件夹的唯一键。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import httpx

from src.config import artifacts_present, settings, task_dir
from src.store import RESOURCE_STATUS_AVAILABLE, TaskStore

logger = logging.getLogger(__name__)

DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
UPLOAD_CHUNK_BYTES = 16 * 1024 * 1024
DOWNLOAD_BUFFER_BYTES = 1024 * 1024
TERMINAL_STATES = {"SUCCESS", "FAILED", "CANCELLED"}
RUNNING_TASK_STATES = {
    "PENDING",
    "DOWNLOADING",
    "EXTRACTING",
    "TRANSCRIBING",
    "TRANSLATING",
    "BURNING",
    "SYNTHESIZING",
    "DUBBING",
}


class DriveSyncError(RuntimeError):
    """可安全展示给本地资源页面的同步错误。"""


class DriveSyncConflict(DriveSyncError):
    """批次冲突或任务当前不可同步。"""


@dataclass
class SyncEntry:
    name: str
    size: int
    kind: str = "other"
    mime: str = "application/octet-stream"
    state: str = "PENDING"
    completed_bytes: int = 0
    error: str = ""
    remote_id: str = ""
    sidecar_batch_id: str = ""
    sidecar_entry_id: str = ""
    upload_id: str = ""
    transfer_id: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SyncEntry":
        return cls(
            name=str(value.get("name") or ""),
            size=max(0, int(value.get("size") or 0)),
            kind=str(value.get("kind") or "other"),
            mime=str(value.get("mime") or "application/octet-stream"),
            state=str(value.get("state") or "PENDING"),
            completed_bytes=max(0, int(value.get("completedBytes", value.get("completed_bytes", 0)) or 0)),
            error=str(value.get("error") or ""),
            remote_id=str(value.get("remoteId", value.get("remote_id", "")) or ""),
            sidecar_batch_id=str(value.get("sidecarBatchId", value.get("sidecar_batch_id", "")) or ""),
            sidecar_entry_id=str(value.get("sidecarEntryId", value.get("sidecar_entry_id", "")) or ""),
            upload_id=str(value.get("uploadId", value.get("upload_id", "")) or ""),
            transfer_id=str(value.get("transferId", value.get("transfer_id", "")) or ""),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size": self.size,
            "kind": self.kind,
            "mime": self.mime,
            "state": self.state,
            "completedBytes": self.completed_bytes,
            "error": self.error or None,
            "remoteId": self.remote_id or None,
            "sidecarBatchId": self.sidecar_batch_id or None,
            "sidecarEntryId": self.sidecar_entry_id or None,
            "uploadId": self.upload_id or None,
            "transferId": self.transfer_id or None,
        }


@dataclass
class DriveBatch:
    id: str
    task_id: str
    direction: str
    state: str = "PENDING"
    folder_id: str = ""
    folder_name: str = ""
    entries: list[SyncEntry] = field(default_factory=list)
    total_bytes: int = 0
    completed_bytes: int = 0
    error: str = ""
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DriveBatch":
        entries = value.get("entries") or []
        return cls(
            id=str(value.get("id") or ""),
            task_id=str(value.get("taskId", value.get("task_id", "")) or ""),
            direction=str(value.get("direction") or "UPLOAD"),
            state=str(value.get("state") or "PENDING"),
            folder_id=str(value.get("folderId", value.get("folder_id", "")) or ""),
            folder_name=str(value.get("folderName", value.get("folder_name", "")) or ""),
            entries=[SyncEntry.from_dict(item) for item in entries if isinstance(item, dict)],
            total_bytes=max(0, int(value.get("totalBytes", value.get("total_bytes", 0)) or 0)),
            completed_bytes=max(0, int(value.get("completedBytes", value.get("completed_bytes", 0)) or 0)),
            error=str(value.get("error") or ""),
            created_at=int(value.get("createdAt", value.get("created_at", 0)) or 0),
            updated_at=int(value.get("updatedAt", value.get("updated_at", 0)) or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_payload(self) -> dict[str, Any]:
        pending = [entry.name for entry in self.entries if entry.state != "SUCCESS"]
        failed = [entry.name for entry in self.entries if entry.state == "FAILED"]
        return {
            "batchId": self.id,
            "taskId": self.task_id,
            "direction": self.direction,
            "state": self.state,
            "folderId": self.folder_id or None,
            "folderName": self.folder_name or self.task_id,
            "totalEntries": len(self.entries),
            "completedEntries": sum(entry.state == "SUCCESS" for entry in self.entries),
            "totalBytes": self.total_bytes,
            "completedBytes": self.completed_bytes,
            "pendingArtifacts": pending,
            "failedArtifacts": failed,
            "entries": [entry.to_payload() for entry in self.entries],
            "error": self.error or None,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


class _BatchRepository:
    """线程安全的 JSON 批次仓库；写入采用临时文件 + 原子替换。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._batches: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            self._batches = {
                str(key): value
                for key, value in data.items()
                if isinstance(value, dict)
            }

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(self._batches, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def get(self, batch_id: str) -> Optional[DriveBatch]:
        with self._lock:
            value = self._batches.get(batch_id)
            return DriveBatch.from_dict(value) if value else None

    def all(self) -> list[DriveBatch]:
        with self._lock:
            return [DriveBatch.from_dict(value) for value in self._batches.values()]

    def put(self, batch: DriveBatch) -> DriveBatch:
        with self._lock:
            self._batches[batch.id] = batch.to_dict()
            self._save_locked()
        return batch


class DriveSidecarClient:
    """调用本机 Google Drive sidecar 的最小同步客户端。"""

    def __init__(self, *, base_url: str | None = None, timeout: float | None = None):
        self.base_url = (
            base_url or os.getenv("SUBTRANS_DRIVE_API_BASE_URL", "http://127.0.0.1:8787")
        ).rstrip("/")
        self.timeout = timeout or max(10.0, float(os.getenv("SUBTRANS_DRIVE_TIMEOUT", "600")))
        self.client = httpx.Client(timeout=self.timeout)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "DriveSidecarClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        token = settings.api_token
        return {"Accept": "application/json", **({"X-API-Token": token} if token else {})}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(self._headers())
        headers.update(kwargs.pop("headers", {}) or {})
        try:
            response = self.client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise DriveSyncError("无法连接 Google Drive sidecar，请确认 8787 端口已启动") from exc
        if response.is_error:
            detail = ""
            try:
                body = response.json()
                detail = body.get("error") or body.get("detail") or ""
            except ValueError:
                detail = response.text
            raise DriveSyncError(f"Google Drive 服务返回 {response.status_code}: {str(detail)[:300]}")
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._request(method, path, **kwargs)
        try:
            value = response.json()
        except ValueError as exc:
            raise DriveSyncError("Google Drive 服务返回了无效 JSON") from exc
        return value if isinstance(value, dict) else {}

    def list_files(self, parent_id: str = "") -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, str] = {"pageSize": "1000"}
            if parent_id:
                params["parentId"] = parent_id
            if page_token:
                params["pageToken"] = page_token
            value = self._json("GET", "/api/drive/files", params=params)
            page = value.get("files") or []
            files.extend(item for item in page if isinstance(item, dict))
            page_token = str(value.get("nextPageToken") or "")
            if not page_token:
                return files

    def find_task_folder(self, task_id: str) -> Optional[dict[str, Any]]:
        matches = [
            item
            for item in self.list_files()
            if item.get("mimeType") == DRIVE_FOLDER_MIME
            and not item.get("trashed")
            and (item.get("appProperties") or {}).get("subtitles_ai_task_id") == task_id
        ]
        if len(matches) > 1:
            raise DriveSyncConflict(f"task_id {task_id} 对应多个 Google Drive 文件夹")
        return matches[0] if matches else None

    def ensure_task_folder(self, task_id: str) -> dict[str, Any]:
        existing = self.find_task_folder(task_id)
        if existing:
            return existing
        value = self._json(
            "POST",
            "/api/drive/task-folders",
            json={"taskId": task_id},
        )
        folder = value.get("folder")
        if not isinstance(folder, dict) or not folder.get("id"):
            raise DriveSyncError("Google Drive 未返回任务文件夹 ID")
        return folder

    def create_folder_upload(
        self,
        entries: list[dict[str, Any]],
        *,
        parent_id: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/api/drive/folder-uploads",
            headers={"Content-Type": "application/json", "Idempotency-Key": client_request_id},
            json={
                "parentId": parent_id,
                "clientRequestId": client_request_id,
                "entries": entries,
            },
        )

    def create_entry_upload(
        self,
        batch_id: str,
        entry_id: str,
        *,
        size: int,
        name: str,
        mime: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/api/drive/folder-uploads/{batch_id}/entries/{entry_id}/upload",
            headers={
                "X-Upload-Length": str(size),
                "X-File-Name": name,
                "X-File-Mime": mime or "application/octet-stream",
            },
        )

    def upload_offset(self, upload_id: str) -> int:
        response = self._request("HEAD", f"/api/drive/uploads/{upload_id}")
        try:
            return max(0, int(response.headers.get("Upload-Offset", "0")))
        except ValueError:
            return 0

    def upload_chunk(self, upload_id: str, chunk: bytes, offset: int) -> tuple[int, str]:
        response = self._request(
            "PATCH",
            f"/api/drive/uploads/{upload_id}",
            headers={
                "Content-Type": "application/octet-stream",
                "X-Upload-Offset": str(offset),
            },
            content=chunk,
        )
        try:
            next_offset = int(response.headers.get("Upload-Offset", offset + len(chunk)))
        except ValueError:
            next_offset = offset + len(chunk)
        return next_offset, response.headers.get("X-Transfer-ID", "")

    def folder_entry_retry(self, batch_id: str, entry_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/api/drive/folder-uploads/{batch_id}/entries/{entry_id}/retry",
        )

    def transfer(self, transfer_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/drive/transfers/{transfer_id}")

    def transfer_action(self, transfer_id: str, action: str) -> dict[str, Any]:
        return self._json("POST", f"/api/drive/transfers/{transfer_id}/{action}")

    def download(self, file_id: str, range_header: str):
        headers = self._headers()
        headers["Range"] = range_header
        try:
            response = self.client.stream(
                "GET",
                f"{self.base_url}/api/drive/files/{file_id}/download",
                headers=headers,
            )
            return response
        except httpx.HTTPError as exc:
            raise DriveSyncError("无法连接 Google Drive 下载接口") from exc


class DriveSyncManager:
    """全局单 worker 编排器；每个任务批次内的产物严格串行。"""

    def __init__(self, *, repository: _BatchRepository | None = None):
        self.repository = repository or _BatchRepository(settings.data_dir / ".drive-sync-batches.json")
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drive-sync")
        self._lock = threading.RLock()
        self._futures: dict[str, Future[Any]] = {}
        self._cancel_events: dict[str, threading.Event] = {}

    def _now(self) -> int:
        return int(time.time() * 1000)

    def _put(self, batch: DriveBatch) -> DriveBatch:
        batch.updated_at = self._now()
        if batch.created_at == 0:
            batch.created_at = batch.updated_at
        batch.total_bytes = sum(entry.size for entry in batch.entries)
        batch.completed_bytes = sum(
            min(entry.size, entry.completed_bytes)
            for entry in batch.entries
        )
        return self.repository.put(batch)

    def _active_for_task(self, task_id: str, direction: str) -> Optional[DriveBatch]:
        for batch in self.repository.all():
            if batch.task_id == task_id and batch.direction == direction and batch.state not in TERMINAL_STATES:
                return batch
        return None

    def start_upload(self, task_id: str, artifacts: Iterable[dict[str, Any]], store: TaskStore) -> DriveBatch:
        with self._lock:
            active = self._active_for_task(task_id, "UPLOAD")
            if active:
                return active
            entries = [
                SyncEntry(
                    name=str(item["name"]),
                    size=max(0, int(item.get("size") or 0)),
                    kind=str(item.get("kind") or "other"),
                    mime=str(item.get("mime") or "application/octet-stream"),
                )
                for item in artifacts
            ]
            if not entries:
                raise DriveSyncConflict("任务没有可上传的产物")
            batch = DriveBatch(
                id="drive_batch_" + uuid.uuid4().hex[:12],
                task_id=task_id,
                direction="UPLOAD",
                folder_name=task_id,
                entries=entries,
                created_at=self._now(),
            )
            self._put(batch)
            self._submit(batch.id, lambda: self._run_upload(batch.id, store))
            return batch

    def start_download(self, task_id: str, store: TaskStore) -> DriveBatch:
        with self._lock:
            active = self._active_for_task(task_id, "DOWNLOAD")
            if active:
                return active
            batch = DriveBatch(
                id="drive_batch_" + uuid.uuid4().hex[:12],
                task_id=task_id,
                direction="DOWNLOAD",
                folder_name=task_id,
                created_at=self._now(),
            )
            self._put(batch)
            self._submit(batch.id, lambda: self._run_download(batch.id, store))
            return batch

    def _submit(self, batch_id: str, callback: Callable[[], Any]) -> None:
        cancel_event = threading.Event()
        self._cancel_events[batch_id] = cancel_event
        future = self.executor.submit(callback)
        self._futures[batch_id] = future

        def cleanup(_future: Future[Any]) -> None:
            with self._lock:
                self._futures.pop(batch_id, None)
                self._cancel_events.pop(batch_id, None)

        future.add_done_callback(cleanup)

    def get(self, batch_id: str) -> Optional[DriveBatch]:
        return self.repository.get(batch_id)

    def cancel(self, batch_id: str) -> DriveBatch:
        batch = self.repository.get(batch_id)
        if not batch:
            raise DriveSyncError("同步批次不存在")
        if batch.state in TERMINAL_STATES:
            return batch
        event = self._cancel_events.get(batch_id)
        if event:
            event.set()
        batch.state = "CANCELLED"
        batch.error = "用户取消"
        self._put(batch)
        return batch

    def retry(self, batch_id: str, store: TaskStore) -> DriveBatch:
        with self._lock:
            batch = self.repository.get(batch_id)
            if not batch:
                raise DriveSyncError("同步批次不存在")
            for entry in batch.entries:
                if entry.state == "FAILED":
                    entry.state = "PENDING"
                    entry.error = ""
            batch.state = "PENDING"
            batch.error = ""
            self._put(batch)
            if batch.direction == "UPLOAD":
                self._submit(batch.id, lambda: self._run_upload(batch.id, store))
            else:
                self._submit(batch.id, lambda: self._run_download(batch.id, store))
            return batch

    def _cancelled(self, batch_id: str) -> bool:
        event = self._cancel_events.get(batch_id)
        return bool(event and event.is_set())

    def _update(self, batch_id: str, mutate: Callable[[DriveBatch], None]) -> Optional[DriveBatch]:
        batch = self.repository.get(batch_id)
        if not batch:
            return None
        if batch.state == "CANCELLED":
            return batch
        mutate(batch)
        return self._put(batch)

    def _mark_failed(self, batch_id: str, error: Exception | str) -> None:
        message = str(error)
        def mutate(batch: DriveBatch) -> None:
            # Cancellation is a user decision and must not be overwritten by a
            # late network/file error from the worker thread.
            if batch.state == "CANCELLED":
                return
            batch.state = "FAILED"
            batch.error = message

        self._update(batch_id, mutate)

    def _finish(self, batch_id: str) -> None:
        def mutate(batch: DriveBatch) -> None:
            if batch.state == "CANCELLED":
                return
            if batch.entries and all(entry.state == "SUCCESS" for entry in batch.entries):
                batch.state = "SUCCESS"
            elif any(entry.state == "FAILED" for entry in batch.entries):
                batch.state = "FAILED"
                if not batch.error:
                    batch.error = "部分产物处理失败，可重试失败项"
            else:
                batch.state = "SUCCESS" if not batch.entries else "TRANSFERRING"

        self._update(batch_id, mutate)

    def _validate_task(self, task_id: str, store: TaskStore) -> None:
        record = store.get(task_id)
        if record is None:
            raise DriveSyncConflict("任务不存在")
        if record.status in RUNNING_TASK_STATES:
            raise DriveSyncConflict("运行中的任务不能同步本地产物")

    def _run_upload(self, batch_id: str, store: TaskStore) -> None:
        batch = self.repository.get(batch_id)
        if not batch:
            return
        try:
            self._validate_task(batch.task_id, store)
            with DriveSidecarClient() as client:
                folder = client.ensure_task_folder(batch.task_id)
                folder_id = str(folder.get("id") or "")
                self._update(batch_id, lambda current: (
                    setattr(current, "folder_id", folder_id),
                    setattr(current, "folder_name", batch.task_id),
                    setattr(current, "state", "TRANSFERRING"),
                ))
                current = self.repository.get(batch_id)
                if not current:
                    return
                remote_files = {
                    str(item.get("name")): item
                    for item in client.list_files(folder_id)
                    if item.get("mimeType") != DRIVE_FOLDER_MIME and item.get("name")
                }
                pending = []
                for entry in current.entries:
                    if entry.state == "SUCCESS":
                        continue
                    remote = remote_files.get(entry.name)
                    if remote:
                        remote_size = _int_value(remote.get("size"))
                        if remote_size == entry.size:
                            self._set_entry(batch_id, entry.name, state="SUCCESS", completed_bytes=entry.size, remote_id=str(remote.get("id") or ""))
                            continue
                        self._set_entry(batch_id, entry.name, state="FAILED", error="Drive 中已存在同名产物且大小不一致")
                        continue
                    if entry.size <= 0:
                        self._set_entry(batch_id, entry.name, state="FAILED", error="不能同步空产物")
                        continue
                    pending.append(entry)

                current = self.repository.get(batch_id)
                pending = [entry for entry in (current.entries if current else []) if entry.state == "PENDING" and entry.size > 0]
                if pending:
                    sidecar_batch_id = next(
                        (entry.sidecar_batch_id for entry in pending if entry.sidecar_batch_id),
                        "",
                    )
                    if not sidecar_batch_id:
                        manifest = [
                            {"relativePath": entry.name, "name": entry.name, "size": entry.size, "mime": entry.mime}
                            for entry in pending
                        ]
                        created = client.create_folder_upload(
                            manifest,
                            parent_id=folder_id,
                            client_request_id=f"{batch_id}:entries",
                        )
                        sidecar_batch = created.get("batch") or {}
                        sidecar_batch_id = str(sidecar_batch.get("id") or "")
                        remote_entries = created.get("entries") or []
                        if not sidecar_batch_id:
                            raise DriveSyncError("sidecar 未返回产物批次 ID")
                        by_path = {
                            str(item.get("relative_path", item.get("relativePath", ""))): item
                            for item in remote_entries
                            if isinstance(item, dict)
                        }
                        for entry in pending:
                            remote_entry = by_path.get(entry.name)
                            if not remote_entry:
                                self._set_entry(batch_id, entry.name, state="FAILED", error="sidecar 未返回产物条目")
                                continue
                            self._set_entry(
                                batch_id,
                                entry.name,
                                sidecar_batch_id=sidecar_batch_id,
                                sidecar_entry_id=str(remote_entry.get("id") or ""),
                            )

                current = self.repository.get(batch_id)
                if not current:
                    return
                for entry in current.entries:
                    if entry.state != "PENDING":
                        continue
                    if self._cancelled(batch_id):
                        return
                    try:
                        self._upload_entry(client, batch_id, entry, batch.task_id)
                    except Exception as exc:  # keep successful siblings and continue the queue
                        if self._cancelled(batch_id):
                            return
                        logger.warning("Drive upload failed: task=%s file=%s err=%s", batch.task_id, entry.name, exc)
                        self._set_entry(batch_id, entry.name, state="FAILED", error=str(exc))
                self._finish(batch_id)
        except Exception as exc:
            logger.warning("Drive upload batch failed: batch=%s err=%s", batch_id, exc)
            if self._cancelled(batch_id):
                return
            self._mark_failed(batch_id, exc)

    def _upload_entry(self, client: DriveSidecarClient, batch_id: str, entry: SyncEntry, task_id: str) -> None:
        path = task_dir(task_id) / entry.name
        if path.name != entry.name or not path.is_file():
            raise DriveSyncError(f"本地产物不存在: {entry.name}")
        if path.stat().st_size != entry.size:
            raise DriveSyncError(f"本地产物大小发生变化: {entry.name}")
        if not entry.sidecar_batch_id or not entry.sidecar_entry_id:
            raise DriveSyncError(f"sidecar 产物条目信息缺失: {entry.name}")
        if not entry.upload_id:
            created = client.create_entry_upload(
                entry.sidecar_batch_id,
                entry.sidecar_entry_id,
                size=entry.size,
                name=entry.name,
                mime=entry.mime,
            )
            self._set_entry(
                batch_id,
                entry.name,
                upload_id=str(created.get("id") or ""),
                completed_bytes=_int_value(created.get("offset")),
            )
            entry = self._entry(batch_id, entry.name) or entry
        offset = client.upload_offset(entry.upload_id)
        self._set_entry(batch_id, entry.name, completed_bytes=offset)
        transfer_id = entry.transfer_id
        with path.open("rb") as source:
            source.seek(offset)
            while offset < entry.size:
                if self._cancelled(batch_id):
                    if transfer_id:
                        try:
                            client.transfer_action(transfer_id, "cancel")
                        except DriveSyncError:
                            pass
                    return
                chunk = source.read(min(UPLOAD_CHUNK_BYTES, entry.size - offset))
                if not chunk:
                    raise DriveSyncError(f"读取本地产物提前结束: {entry.name}")
                try:
                    offset, new_transfer_id = client.upload_chunk(entry.upload_id, chunk, offset)
                except DriveSyncError as exc:
                    if "409" in str(exc):
                        offset = client.upload_offset(entry.upload_id)
                        source.seek(offset)
                        continue
                    raise
                transfer_id = new_transfer_id or transfer_id
                self._set_entry(batch_id, entry.name, completed_bytes=offset, transfer_id=transfer_id)
        if offset < entry.size:
            raise DriveSyncError(f"上传偏移未到文件末尾: {entry.name}")
        if not transfer_id:
            raise DriveSyncError(f"sidecar 未返回 Drive transfer ID: {entry.name}")
        self._wait_transfer(client, batch_id, transfer_id, entry.name)
        if self._cancelled(batch_id):
            return
        transfer = client.transfer(transfer_id)
        self._set_entry(
            batch_id,
            entry.name,
            state="SUCCESS",
            completed_bytes=entry.size,
            remote_id=str(transfer.get("file_id", transfer.get("fileId", "")) or ""),
            error="",
        )

    def _wait_transfer(self, client: DriveSidecarClient, batch_id: str, transfer_id: str, name: str) -> None:
        deadline = time.monotonic() + max(60.0, float(os.getenv("SUBTRANS_DRIVE_TRANSFER_TIMEOUT", "7200")))
        while time.monotonic() < deadline:
            if self._cancelled(batch_id):
                try:
                    client.transfer_action(transfer_id, "cancel")
                except DriveSyncError:
                    pass
                return
            transfer = client.transfer(transfer_id)
            state = str(transfer.get("state") or "")
            if state == "SUCCESS":
                return
            if state in {"FAILED", "CANCELLED"}:
                raise DriveSyncError(f"Drive 传输失败: {name} ({transfer.get('error') or state})")
            time.sleep(0.5)
        raise DriveSyncError(f"Drive 传输超时: {name}")

    def _run_download(self, batch_id: str, store: TaskStore) -> None:
        batch = self.repository.get(batch_id)
        if not batch:
            return
        try:
            self._validate_task(batch.task_id, store)
            with DriveSidecarClient() as client:
                folder = client.find_task_folder(batch.task_id)
                if not folder or not folder.get("id"):
                    raise DriveSyncError(f"未找到任务文件夹: {batch.task_id}")
                folder_id = str(folder["id"])
                remote_files = [
                    item
                    for item in client.list_files(folder_id)
                    if item.get("mimeType") != DRIVE_FOLDER_MIME and item.get("name")
                ]
                if not remote_files:
                    raise DriveSyncError("任务文件夹中没有可下载产物")
                current = self.repository.get(batch_id) or batch
                if not current.entries:
                    current.folder_id = folder_id
                    current.folder_name = batch.task_id
                    current.entries = [
                        SyncEntry(
                            name=str(item["name"]),
                            size=_int_value(item.get("size")),
                            mime=str(item.get("mimeType") or "application/octet-stream"),
                            remote_id=str(item.get("id") or ""),
                        )
                        for item in sorted(remote_files, key=lambda value: str(value.get("name") or ""))
                    ]
                    self._put(current)
                else:
                    current.folder_id = folder_id
                    self._put(current)
                for remote in remote_files:
                    name = str(remote.get("name") or "")
                    entry = self._entry(batch_id, name)
                    if not entry or entry.state == "SUCCESS":
                        continue
                    if self._cancelled(batch_id):
                        return
                    try:
                        self._download_entry(client, batch_id, entry, batch.task_id, remote)
                    except Exception as exc:
                        if self._cancelled(batch_id):
                            return
                        logger.warning("Drive download failed: task=%s file=%s err=%s", batch.task_id, name, exc)
                        self._set_entry(batch_id, name, state="FAILED", error=str(exc))
                self._finish(batch_id)
                if (self.repository.get(batch_id) or batch).state == "SUCCESS":
                    record = store.get(batch.task_id)
                    if record and artifacts_present(
                        batch.task_id,
                        data_dir=settings.data_dir,
                        need_subtitle=bool(record.need_subtitle),
                    ):
                        store.update(
                            batch.task_id,
                            resource_status=RESOURCE_STATUS_AVAILABLE,
                            error=None,
                            error_code=None,
                            downgrade_reason=None,
                            downgrade_errno=None,
                            downgraded_at=None,
                        )
        except Exception as exc:
            logger.warning("Drive download batch failed: batch=%s err=%s", batch_id, exc)
            if self._cancelled(batch_id):
                return
            self._mark_failed(batch_id, exc)

    def _download_entry(
        self,
        client: DriveSidecarClient,
        batch_id: str,
        entry: SyncEntry,
        task_id: str,
        remote: dict[str, Any],
    ) -> None:
        if Path(entry.name).name != entry.name or entry.name in {"", ".", ".."}:
            raise DriveSyncError(f"Drive 产物文件名不安全: {entry.name}")
        total = _int_value(remote.get("size"))
        if total <= 0:
            raise DriveSyncError(f"Drive 产物为空或缺少大小: {entry.name}")
        destination_dir = task_dir(task_id)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / entry.name
        part = destination.with_name(destination.name + ".part")
        offset = part.stat().st_size if part.exists() else 0
        if offset > total:
            part.unlink(missing_ok=True)
            offset = 0
        self._set_entry(batch_id, entry.name, completed_bytes=offset, state="TRANSFERRING")
        while offset < total:
            if self._cancelled(batch_id):
                return
            range_header = f"bytes={offset}-"
            with client.download(str(remote.get("id") or entry.remote_id), range_header) as response:
                if response.status_code not in (200, 206):
                    raise DriveSyncError(f"Drive 下载返回 {response.status_code}: {entry.name}")
                if offset > 0 and response.status_code == 200:
                    part.unlink(missing_ok=True)
                    offset = 0
                    continue
                with part.open("ab" if offset else "wb") as target:
                    for chunk in response.iter_bytes(DOWNLOAD_BUFFER_BYTES):
                        if self._cancelled(batch_id):
                            return
                        if not chunk:
                            continue
                        target.write(chunk)
                        offset += len(chunk)
                        self._set_entry(batch_id, entry.name, completed_bytes=offset, state="TRANSFERRING")
            if offset < total:
                continue
        if part.stat().st_size != total:
            raise DriveSyncError(f"Drive 下载大小校验失败: {entry.name}")
        expected_md5 = str(remote.get("md5Checksum") or "")
        if expected_md5 and _md5(part) != expected_md5:
            raise DriveSyncError(f"Drive MD5 校验失败: {entry.name}")
        os.replace(part, destination)
        self._set_entry(batch_id, entry.name, state="SUCCESS", completed_bytes=total, remote_id=str(remote.get("id") or ""), error="")

    def _entry(self, batch_id: str, name: str) -> Optional[SyncEntry]:
        batch = self.repository.get(batch_id)
        if not batch:
            return None
        return next((entry for entry in batch.entries if entry.name == name), None)

    def _set_entry(self, batch_id: str, name: str, **changes: Any) -> None:
        def mutate(batch: DriveBatch) -> None:
            if batch.state == "CANCELLED":
                return
            entry = next((item for item in batch.entries if item.name == name), None)
            if entry is None:
                return
            for key, value in changes.items():
                setattr(entry, key, value)

        self._update(batch_id, mutate)


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(DOWNLOAD_BUFFER_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


_manager: DriveSyncManager | None = None
_manager_lock = threading.Lock()


def get_drive_sync_manager() -> DriveSyncManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = DriveSyncManager()
        return _manager
