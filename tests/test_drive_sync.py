"""任务级 Drive 同步编排器的串行队列测试。"""

from __future__ import annotations

import hashlib
import threading
import time

import pytest

from src.service import drive_sync
from src.service.drive_sync import DriveSyncConflict, DriveSyncManager, _BatchRepository
from src.store import TaskStore


class _FakeSidecar:
    calls: list[tuple[str, str]] = []

    def __init__(self):
        self.upload_offsets: dict[str, int] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def ensure_task_folder(self, task_id):
        self.calls.append(("folder", task_id))
        return {"id": "folder-1", "name": task_id}

    def find_task_folder(self, task_id):
        return {"id": "folder-1", "name": task_id}

    def list_files(self, _parent_id=""):
        self.calls.append(("list", "folder-1"))
        return []

    def create_folder_upload(self, entries, *, parent_id, client_request_id):
        self.calls.append(("manifest", ",".join(item["name"] for item in entries)))
        return {
            "batch": {"id": "sidecar-batch-1"},
            "entries": [
                {"id": f"sidecar-entry-{item['name']}", "relativePath": item["name"]}
                for item in entries
            ],
        }

    def create_entry_upload(self, _batch_id, entry_id, *, size, name, mime):
        self.calls.append(("create-upload", name))
        upload_id = f"upload-{name}"
        self.upload_offsets[upload_id] = 0
        return {"id": upload_id, "offset": 0}

    def upload_offset(self, upload_id):
        return self.upload_offsets[upload_id]

    def upload_chunk(self, upload_id, chunk, offset):
        assert offset == self.upload_offsets[upload_id]
        self.upload_offsets[upload_id] += len(chunk)
        self.calls.append(("chunk", upload_id))
        return self.upload_offsets[upload_id], f"transfer-{upload_id}"

    def transfer(self, transfer_id):
        return {"state": "SUCCESS", "file_id": f"drive-{transfer_id}"}

    def transfer_action(self, *_):
        return {"state": "CANCELLED"}


class _FakeDownloadResponse:
    status_code = 206

    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def iter_bytes(self, _chunk_size):
        yield self.payload


class _FakeDownloadSidecar(_FakeSidecar):
    def __init__(self, payload: bytes):
        super().__init__()
        self.payload = payload
        self.download_ranges: list[str] = []

    def list_files(self, _parent_id=""):
        return [{
            "id": "remote-1",
            "name": "restored.srt",
            "size": len(self.payload),
            "mimeType": "text/plain",
            "md5Checksum": hashlib.md5(self.payload).hexdigest(),
        }]

    def download(self, _file_id, range_header):
        self.download_ranges.append(range_header)
        offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
        return _FakeDownloadResponse(self.payload[offset:])


def _wait_for_terminal(manager, batch_id):
    deadline = time.time() + 3
    while time.time() < deadline:
        batch = manager.get(batch_id)
        if batch and batch.state in {"SUCCESS", "FAILED", "CANCELLED"}:
            return batch
        time.sleep(0.01)
    raise AssertionError("batch did not reach a terminal state")


def test_upload_processes_entries_serially_and_clears_pending(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks.db")
    record = store.create(
        url="https://example.com/video",
        source_lang="auto",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="small",
        engine="deepseek",
        title="mutable title",
    )
    store.update(record.id, status="SUCCESS", progress=100)
    task_path = tmp_path / record.id
    task_path.mkdir()
    (task_path / "a.txt").write_bytes(b"a")
    (task_path / "b.txt").write_bytes(b"bb")
    monkeypatch.setattr(drive_sync, "task_dir", lambda _task_id: task_path)
    fake = _FakeSidecar()
    monkeypatch.setattr(drive_sync, "DriveSidecarClient", lambda: fake)

    manager = DriveSyncManager(repository=_BatchRepository(tmp_path / "batches.json"))
    batch = manager.start_upload(
        record.id,
        [
            {"name": "a.txt", "size": 1, "kind": "other", "mime": "text/plain"},
            {"name": "b.txt", "size": 2, "kind": "other", "mime": "text/plain"},
        ],
        store,
    )
    result = _wait_for_terminal(manager, batch.id)

    assert result.state == "SUCCESS"
    assert result.completed_bytes == 3
    assert result.entries[0].state == "SUCCESS"
    assert result.entries[1].state == "SUCCESS"
    assert [name for kind, name in fake.calls if kind == "create-upload"] == ["a.txt", "b.txt"]
    assert fake.calls.index(("create-upload", "a.txt")) < fake.calls.index(("create-upload", "b.txt"))
    manager.executor.shutdown(wait=True)


def test_retry_waits_for_cancelled_worker_cleanup(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    record = store.create(
        url="https://example.com/video",
        source_lang="auto",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="small",
        engine="deepseek",
        title="retry task",
    )
    store.update(record.id, status="SUCCESS", progress=100)
    manager = DriveSyncManager(repository=_BatchRepository(tmp_path / "batches.json"))
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def worker(batch_id, _store):
        calls.append(batch_id)
        if len(calls) == 1:
            started.set()
            assert release.wait(timeout=3)
        manager._finish(batch_id)

    manager._run_upload = worker
    batch = manager.start_upload(
        record.id,
        [{"name": "a.txt", "size": 1, "kind": "other", "mime": "text/plain"}],
        store,
    )
    try:
        assert started.wait(timeout=3)
        cancelled = manager.cancel(batch.id)
        assert cancelled.state == "CANCELLED"
        with pytest.raises(DriveSyncConflict, match="后台收尾"):
            manager.retry(batch.id, store)

        release.set()
        deadline = time.time() + 3
        while batch.id in manager._futures and time.time() < deadline:
            time.sleep(0.01)
        assert batch.id not in manager._futures

        manager.retry(batch.id, store)
        deadline = time.time() + 3
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert calls == [batch.id, batch.id]
    finally:
        release.set()
        manager.executor.shutdown(wait=True)


def test_download_resumes_part_file_and_clears_each_entry(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks.db")
    record = store.create(
        url="https://example.com/video",
        source_lang="auto",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="small",
        engine="deepseek",
        title="download task",
    )
    store.update(record.id, status="SUCCESS", progress=100)
    task_path = tmp_path / record.id
    task_path.mkdir()
    (task_path / "restored.srt.part").write_bytes(b"he")
    fake = _FakeDownloadSidecar(b"hello")
    monkeypatch.setattr(drive_sync, "task_dir", lambda _task_id: task_path)
    monkeypatch.setattr(drive_sync, "DriveSidecarClient", lambda: fake)

    manager = DriveSyncManager(repository=_BatchRepository(tmp_path / "batches.json"))
    batch = manager.start_download(record.id, store)
    result = _wait_for_terminal(manager, batch.id)

    assert result.state == "SUCCESS"
    assert result.entries[0].state == "SUCCESS"
    assert result.entries[0].completed_bytes == 5
    assert result.to_payload()["pendingArtifacts"] == []
    assert fake.download_ranges == ["bytes=2-"]
    assert (task_path / "restored.srt").read_bytes() == b"hello"
    assert not (task_path / "restored.srt.part").exists()
    manager.executor.shutdown(wait=True)
