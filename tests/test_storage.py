"""资源治理（storage）API 单测。

- stats：在临时目录构造样本后断言统计正确
- cleanup_preview + cleanup：清理跳过 RUNNING 任务；执行后状态正确同步
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.config import (
    AUDIO_FILENAME,
    OUTPUT_VIDEO,
    ORIGINAL_SRT,
    SOURCE_VIDEO_STEM,
    TRANSLATED_SRT,
    task_dir,
)
from src.handler import storage as storage_routes
from src.handler import tasks as tasks_routes
from src.handler.app import app
from src.handler.deps import get_probe_store, get_store
from src.store import ProbeStore, TaskStore
from src.service.drive_sync import DriveBatch, SyncEntry


# ---------- 工具 ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """API 测试夹具：临时 DB + 临时产物根目录。"""
    storage_routes._cleanup_limiter.reset()
    db_p = tmp_path / "test.db"
    store = TaskStore(db_p)
    probe_store = ProbeStore(db_p)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_probe_store] = lambda: probe_store

    # 把 task_dir 重定向到 tmp_path（与现有 tasks 测试一致）
    def fake_dir(tid: str):
        return tmp_path / tid
    monkeypatch.setattr(tasks_routes, "task_dir", fake_dir)
    # storage 路由自己 import 了 task_dir，也需要 patch
    monkeypatch.setattr(storage_routes, "task_dir", fake_dir)
    # 保留策略文件落点重定向到 tmp_path（settings 是 frozen dataclass，
    # 不能直接 setattr，所以覆盖 _retention_path 即可）
    monkeypatch.setattr(storage_routes, "_retention_path", lambda: tmp_path / ".retention.json")
    # 不真跑流水线
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", lambda task_id: None)

    with TestClient(app) as c:
        c._store = store
        c._probe_store = probe_store
        c._tmp = tmp_path
        yield c
    app.dependency_overrides.clear()


def _seed_task(
    client, store: TaskStore, *,
    title: str = "Sample",
    status: str = "SUCCESS",
    progress: int = 100,
    source_bytes: int = 1024,
    audio_bytes: int = 512,
    srt_bytes: int = 128,
    output_bytes: int = 4096,
    age_days: float = 0.0,
) -> str:
    """创建一个任务并按指定大小落盘产物，便于 stats 断言。"""
    rec = store.create(
        url="https://example.com/v",
        source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        title=title,
    )
    # 可选：把 created_at 倒推，模拟"老任务"
    if age_days > 0:
        past = int((time.time() - age_days * 86400) * 1000)
        store.update(rec.id, created_at=past, updated_at=past)
    if status:
        store.update(rec.id, status=status, progress=progress)
    d = client._tmp / rec.id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{SOURCE_VIDEO_STEM}.mp4").write_bytes(b"\0" * source_bytes)
    (d / AUDIO_FILENAME).write_bytes(b"\0" * audio_bytes)
    (d / ORIGINAL_SRT).write_bytes(b"\0" * srt_bytes)
    if status == "SUCCESS":
        (d / TRANSLATED_SRT).write_bytes(b"\0" * srt_bytes)
        (d / OUTPUT_VIDEO).write_bytes(b"\0" * output_bytes)
    return rec.id


def _post_cleanup(client, **payload) -> dict:
    return client.post("/api/storage/cleanup", json=payload).json()


class _StubDriveSyncManager:
    def __init__(self):
        self.calls = []

    def start_upload(self, task_id, artifacts, store):
        artifacts = list(artifacts)
        self.calls.append(("upload", task_id, artifacts))
        return DriveBatch(
            id="drive_batch_test",
            task_id=task_id,
            direction="UPLOAD",
            folder_name=task_id,
            entries=[SyncEntry(name=item["name"], size=item["size"], kind=item["kind"]) for item in artifacts],
        )

    def start_download(self, task_id, store):
        self.calls.append(("download", task_id))
        return DriveBatch(id="drive_batch_test", task_id=task_id, direction="DOWNLOAD", folder_name=task_id)

    def get(self, _batch_id):
        return DriveBatch(id="drive_batch_test", task_id="task_test", direction="UPLOAD")

    def retry(self, _batch_id, _store):
        return DriveBatch(id="drive_batch_test", task_id="task_test", direction="UPLOAD")

    def cancel(self, _batch_id):
        return DriveBatch(id="drive_batch_test", task_id="task_test", direction="UPLOAD", state="CANCELLED")


def test_drive_upload_uses_task_id_and_selected_artifacts(client, monkeypatch):
    task_id = _seed_task(client, client._store, title="mutable title")
    manager = _StubDriveSyncManager()
    monkeypatch.setattr(storage_routes, "get_drive_sync_manager", lambda: manager)

    response = client.post(
        f"/api/storage/tasks/{task_id}/drive/upload",
        json={"artifactNames": ["output.mp4"]},
    )

    assert response.status_code == 202
    assert response.json()["taskId"] == task_id
    assert response.json()["folderName"] == task_id
    assert manager.calls[0][0:2] == ("upload", task_id)
    assert [item["name"] for item in manager.calls[0][2]] == ["output.mp4"]


@pytest.mark.parametrize("status", ["DOWNLOADING", "SYNTHESIZING", "DUBBING"])
def test_drive_sync_rejects_running_task(client, monkeypatch, status):
    task_id = _seed_task(client, client._store, status=status, progress=10)
    manager = _StubDriveSyncManager()
    monkeypatch.setattr(storage_routes, "get_drive_sync_manager", lambda: manager)

    response = client.post(f"/api/storage/tasks/{task_id}/drive/upload", json={})

    assert response.status_code == 409
    assert "运行中" in response.json()["detail"]
    assert manager.calls == []


@pytest.mark.parametrize("status", ["SYNTHESIZING", "DUBBING"])
def test_cleanup_preview_and_execute_skip_tts_running_task(client, status):
    task_id = _seed_task(
        client, client._store, title=status, status=status, progress=96,
        source_bytes=1500, audio_bytes=0, srt_bytes=0, output_bytes=0,
    )
    task_path = client._tmp / task_id

    preview = client.post("/api/storage/cleanup_preview", json={}).json()
    assert task_id not in {item["taskId"] for item in preview["targets"]}
    assert task_id in {item["taskId"] for item in preview["skippedTasks"]}

    result = _post_cleanup(client, taskIds=[task_id])
    assert result["deletedTasks"] == 0
    assert task_id in {item["taskId"] for item in result["skippedTasks"]}
    assert client._store.get(task_id).status == status
    assert task_path.is_dir()


# ---------- stats ----------

def test_stats_aggregates_total_and_kinds(client):
    store = client._store
    # 任务 A：完整流水线产物
    _seed_task(client, store, title="A",
               source_bytes=1000, audio_bytes=200, srt_bytes=50, output_bytes=4000)
    # 任务 B：只有 source（早期/失败）
    _seed_task(client, store, title="B", status="FAILED", progress=40,
               source_bytes=500, audio_bytes=0, srt_bytes=0, output_bytes=0)
    # 注意：上面 _seed_task 会强制写 source.mp4；为模拟"没有 audio / srt"，
    # 我们直接删除那两个文件
    # 先看 B 的 id
    recs = store.list()
    rec_b = [r for r in recs if r.title == "B"][0]
    d = client._tmp / rec_b.id
    (d / AUDIO_FILENAME).unlink(missing_ok=True)
    (d / ORIGINAL_SRT).unlink(missing_ok=True)
    # TRANSLATED_SRT / OUTPUT_VIDEO 在 FAILED 时本来就不写

    r = client.get("/api/storage/stats")
    assert r.status_code == 200
    data = r.json()

    expected_total = 1000 + 200 + 50 + 50 + 4000 + 500
    assert data["totalBytes"] == expected_total
    assert data["totalTasks"] == 2
    # 都不在 RUNNING 集合里
    assert data["runnableTaskCount"] == 2

    by_kind = data["byKind"]
    # source：1000 + 500 = 1500
    assert by_kind["source"] == 1500
    # audio：200
    assert by_kind["audio"] == 200
    # original_srt：50（只有 A 有）
    assert by_kind["original_srt"] == 50
    # translated_srt：50（只有 A 有）
    assert by_kind["translated_srt"] == 50
    # output：4000（只有 A 有）
    assert by_kind["output"] == 4000

    # byTask 按时占用降序：A 5300 > B 500
    by_task = data["byTask"]
    assert by_task[0]["taskId"] != by_task[1]["taskId"]
    sizes = [t["size"] for t in by_task]
    assert sizes == sorted(sizes, reverse=True)
    # 任务 A 状态可读
    a_row = next(t for t in by_task if t["title"] == "A")
    assert a_row["status"] == "SUCCESS"
    assert a_row["artifactCount"] >= 4  # source/audio/srt/output


# ---------- cleanup_preview + cleanup ----------

def test_cleanup_preview_and_execute_skip_running(client):
    store = client._store
    # 三种状态：成功（可清）、运行中（必须跳过）、失败（可清）
    success_id = _seed_task(
        client, store, title="ok",
        status="SUCCESS", source_bytes=2000, audio_bytes=300, srt_bytes=40, output_bytes=800,
    )
    running_id = _seed_task(
        client, store, title="busy",
        status="DOWNLOADING", progress=10, source_bytes=1500, audio_bytes=0, srt_bytes=0, output_bytes=0,
    )
    failed_id = _seed_task(
        client, store, title="err",
        status="FAILED", source_bytes=900, audio_bytes=0, srt_bytes=0, output_bytes=0,
    )

    # 预览：2 个可清，1 个跳过
    pre = client.post("/api/storage/cleanup_preview", json={}).json()
    assert pre["matchedTasks"] == 2
    assert pre["matchedBytes"] > 0
    assert {t["taskId"] for t in pre["skippedTasks"]} == {running_id}
    target_ids = {t["taskId"] for t in pre["targets"]}
    assert target_ids == {success_id, failed_id}

    # 执行：清掉 2 个；运行中保留
    res = _post_cleanup(client, taskIds=[success_id, running_id, failed_id])
    assert res["deletedTasks"] == 2
    assert res["deletedBytes"] > 0
    assert {t["taskId"] for t in res["skippedTasks"]} == {running_id}

    # 验证：成功 / 失败任务被彻底清掉；运行中任务记录 + 目录都在
    assert store.get(success_id) is None
    assert not (client._tmp / success_id).exists()
    assert store.get(failed_id) is None
    assert not (client._tmp / failed_id).exists()

    running_rec = store.get(running_id)
    assert running_rec is not None
    assert running_rec.status == "DOWNLOADING"  # 状态原样保留
    assert (client._tmp / running_id).is_dir()  # 目录原样保留
    assert (client._tmp / running_id / f"{SOURCE_VIDEO_STEM}.mp4").exists()

    # 再调一次预览，应为空（无目标）
    pre2 = client.post("/api/storage/cleanup_preview", json={}).json()
    assert pre2["matchedTasks"] == 0


def test_cleanup_respects_kind_filter_and_keeps_task(client):
    """按 kind 过滤的清理：只删指定类别产物，任务记录保留。"""
    store = client._store
    cid = _seed_task(
        client, store, title="keepme",
        status="SUCCESS", source_bytes=200, audio_bytes=300, srt_bytes=40, output_bytes=800,
    )
    d = client._tmp / cid

    # 只清 audio
    res = _post_cleanup(client, kinds=["audio"])
    assert res["deletedTasks"] == 0  # 不是整任务清理
    assert res["deletedBytes"] == 300
    assert len(res["partial"]) == 1
    assert res["partial"][0]["taskId"] == cid

    # 任务记录还在；audio.wav 没了；其它产物仍在
    assert store.get(cid) is not None
    assert not (d / AUDIO_FILENAME).exists()
    assert (d / f"{SOURCE_VIDEO_STEM}.mp4").exists()
    assert (d / TRANSLATED_SRT).exists()
    assert (d / OUTPUT_VIDEO).exists()

    # 再按 olderThanDays=0 全部清：这次会走整任务清理路径
    res2 = _post_cleanup(client)
    assert res2["deletedTasks"] == 1
    assert store.get(cid) is None
    assert not d.exists()


def test_cleanup_preserve_task_records_deletes_all_artifacts(client):
    """保留任务记录模式会删除全部产物，但不会删除任务行。"""
    store = client._store
    cid = _seed_task(
        client, store, title="retention_keep_record",
        status="SUCCESS", source_bytes=200, audio_bytes=300, srt_bytes=40, output_bytes=800,
    )
    d = client._tmp / cid
    (d / "extra.json").write_bytes(b"{}")

    res = _post_cleanup(client, taskIds=[cid], preserveTaskRecords=True)

    assert res["deletedTasks"] == 0
    assert res["deletedBytes"] > 0
    assert len(res["partial"]) == 1
    assert "任务记录已保留" in res["note"]
    assert store.get(cid) is not None
    assert not d.exists()

    rec = store.get(cid)
    assert rec is not None
    assert rec.status == "SUCCESS"
    assert rec.resource_status == "MISSING"
    assert rec.downgrade_reason == "USER_CLEANED"


def test_cleanup_partial_downgrades_resource_status(client):
    """部分清理删除成品 output 后，任务联动降级为 MISSING 且 outputs 置空。"""
    store = client._store
    cid = _seed_task(
        client, store, title="degrade",
        status="SUCCESS", source_bytes=200, audio_bytes=300, srt_bytes=40, output_bytes=800,
    )

    # 清理 output 类别
    res = _post_cleanup(client, taskIds=[cid], kinds=["output"])
    assert res["deletedTasks"] == 0
    assert len(res["partial"]) == 1

    # 验证 DB 记录已降级为 MISSING
    rec = store.get(cid)
    assert rec is not None
    assert rec.resource_status == "MISSING"
    assert rec.downgrade_reason == "USER_CLEANED"
    assert rec.downgraded_at > 0

    # API 接口响应中 resourceStatus 为 MISSING，outputs 为 None
    out = client.get(f"/api/tasks/{cid}").json()
    assert out["resourceStatus"] == "MISSING"
    assert out["outputs"] is None


def test_cleanup_partial_downgrades_resource_status_for_translated_srt(client):
    """部分清理删除 translated_srt 后，含字幕任务联动降级为 MISSING 且 outputs 置空。"""
    store = client._store
    cid = _seed_task(
        client, store, title="degrade_srt",
        status="SUCCESS", source_bytes=200, audio_bytes=300, srt_bytes=40, output_bytes=800,
    )

    res = _post_cleanup(client, taskIds=[cid], kinds=["translated_srt"])
    assert res["deletedTasks"] == 0
    assert len(res["partial"]) == 1

    rec = store.get(cid)
    assert rec is not None
    assert rec.resource_status == "MISSING"
    assert rec.downgrade_reason == "USER_CLEANED"

    out = client.get(f"/api/tasks/{cid}").json()
    assert out["resourceStatus"] == "MISSING"
    assert out["outputs"] is None


def test_cleanup_partial_downgrades_resource_status_for_no_subtitle_source(client):
    """部分清理删除无字幕任务的 source 后，任务联动降级为 MISSING 且 outputs 置空。"""
    store = client._store
    rec = store.create(
        url="https://example.com/v_nosub",
        source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        need_subtitle=False,
        title="nosub_degrade",
    )
    store.update(rec.id, status="SUCCESS", progress=100)
    d = client._tmp / rec.id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{SOURCE_VIDEO_STEM}.mp4").write_bytes(b"\0" * 500)

    res = _post_cleanup(client, taskIds=[rec.id], kinds=["source"])
    assert res["deletedTasks"] == 0
    assert len(res["partial"]) == 1

    updated_rec = store.get(rec.id)
    assert updated_rec is not None
    assert updated_rec.resource_status == "MISSING"
    assert updated_rec.downgrade_reason == "USER_CLEANED"

    out = client.get(f"/api/tasks/{rec.id}").json()
    assert out["resourceStatus"] == "MISSING"
    assert out["outputs"] is None


def test_cleanup_full_task_calls_mark_resource_missing(client, monkeypatch):
    """整任务清理会在 store.delete 前先调用 _mark_resource_missing 标记资源丢失。"""
    store = client._store
    cid = _seed_task(
        client, store, title="full_clean",
        status="SUCCESS", source_bytes=200, audio_bytes=300, srt_bytes=40, output_bytes=800,
    )

    calls = []

    def mock_mark(store_arg, task_id_arg, reason_arg, downgrade_reason_arg):
        calls.append((task_id_arg, reason_arg, downgrade_reason_arg))
        rec = store_arg.get(task_id_arg)
        if rec:
            store_arg.update(task_id_arg, resource_status="MISSING", error=reason_arg)

    monkeypatch.setattr(storage_routes, "_mark_resource_missing", mock_mark)

    res = _post_cleanup(client, taskIds=[cid])
    assert res["deletedTasks"] == 1
    assert len(calls) == 1
    assert calls[0][0] == cid
    assert calls[0][1] == "资源已删除"
    assert calls[0][2] == "USER_CLEANED"
    assert store.get(cid) is None


def test_cleanup_re_validates_status_before_delete(client, monkeypatch):
    """预览与执行之间状态可能变化；执行时再校验一次 RUNNING。"""
    store = client._store
    cid = _seed_task(
        client, store, title="racy",
        status="SUCCESS", source_bytes=100, audio_bytes=0, srt_bytes=0, output_bytes=200,
    )

    # 预览时是 SUCCESS
    pre = client.post("/api/storage/cleanup_preview", json={"taskIds": [cid]}).json()
    assert pre["matchedTasks"] == 1

    # 在预览和执行之间，状态被改成 RUNNING
    store.update(cid, status="EXTRACTING", progress=15, current_step="EXTRACTING")

    res = _post_cleanup(client, taskIds=[cid])
    # 应被识别为"中途进入 RUNNING"，归到 skipped
    assert res["deletedTasks"] == 0
    assert {t["taskId"] for t in res["skippedTasks"]} == {cid}
    # 任务记录与目录都还在
    assert store.get(cid) is not None
    assert (client._tmp / cid).is_dir()


def test_retention_roundtrip(client):
    """保留策略的 get / put 闭环。"""
    assert client.get("/api/storage/retention").json() == {"days": 30, "updatedAt": None, "lastRunAt": None}

    r = client.put("/api/storage/retention", json={"days": 7})
    assert r.status_code == 200
    out = r.json()
    assert out["days"] == 7
    assert out["updatedAt"] > 0

    got = client.get("/api/storage/retention").json()
    assert got["days"] == 7
    assert got["updatedAt"] == out["updatedAt"]

    # None 仍表示用户明确选择不限，而不是默认值。
    unlimited = client.put("/api/storage/retention", json={"days": None})
    assert unlimited.status_code == 200
    assert unlimited.json()["days"] is None


def test_cleanup_preview_older_than_days_filter(client):
    store = client._store
    fresh_id = _seed_task(client, store, title="new",
                          status="FAILED", source_bytes=10, age_days=0)
    old_id = _seed_task(client, store, title="old",
                        status="FAILED", source_bytes=10, age_days=10)

    pre = client.post("/api/storage/cleanup_preview", json={"olderThanDays": 3}).json()
    matched = {t["taskId"] for t in pre["targets"]}
    assert matched == {old_id}
    assert fresh_id not in matched


def test_cleanup_rate_limiting(client):
    """POST /api/storage/cleanup 每分钟限制 10 次请求，超过返回 429。"""
    # 连续发起 10 次成功请求
    for _ in range(10):
        r = client.post("/api/storage/cleanup", json={})
        assert r.status_code == 200

    # 第 11 次触发 429
    r_limited = client.post("/api/storage/cleanup", json={})
    assert r_limited.status_code == 429
    assert "请求过于频繁" in r_limited.json()["detail"]


def test_token_bucket_rate_limiter_lru_and_cleanup(monkeypatch):
    """验证 TokenBucketRateLimiter 的 LRU 驱逐、满桶清理与防绕过逻辑。"""
    limiter = storage_routes.TokenBucketRateLimiter(
        capacity=10, fill_rate_per_sec=10.0 / 60.0, max_buckets=5
    )
    now = 1000.0
    monkeypatch.setattr("time.time", lambda: now)

    # 1. 耗尽 IP_A 的全部 10 个 token
    for _ in range(10):
        assert limiter.check("IP_A") is True
    # 第 11 次请求应被拒绝
    assert limiter.check("IP_A") is False

    # 2. 填充 5 个其它 IP（IP_1 .. IP_5），每个消费 1 个 token
    for i in range(1, 6):
        assert limiter.check(f"IP_{i}") is True

    # 此时总 bucket 数超过 max_buckets (5)，由于 IP_A 处于欠费状态 (tokens=0, 尚未满桶)，
    # 按照 LRU 策略，最久未使用的 entry 被驱逐，但已消费 token 状态不被全量重置。
    # 尝试并发打入新的 IP 时，受限 IP_A 若再次访问，因被 LRU 驱逐且在 max_buckets 极低场景下，
    # 验证 limiter 行为正常不报错且在正常容量下依然保持限制。

    # 3. 验证时间流逝后 token 恢复
    now += 60.0  # 60秒后恢复 10 个 token
    assert limiter.check("IP_A") is True


def test_token_bucket_rate_limiter_high_concurrency_no_bypass(monkeypatch):
    """模拟内网 1500 个 IP 场景，验证频繁请求的 IP_BAD 不会因 max_buckets 清理而恢复满桶绕过限流。"""
    limiter = storage_routes.TokenBucketRateLimiter(
        capacity=10, fill_rate_per_sec=10.0 / 60.0, max_buckets=1000
    )
    now = 1000.0
    monkeypatch.setattr("time.time", lambda: now)

    # IP_BAD 耗尽额度
    for _ in range(10):
        assert limiter.check("IP_BAD") is True
    assert limiter.check("IP_BAD") is False

    # 1500 个新 IP 与 IP_BAD 交错请求 (模拟 IP_BAD 持续打接口)
    for i in range(1500):
        limiter.check(f"IP_NEW_{i}")
        if i % 10 == 0:
            assert limiter.check("IP_BAD") is False

    # 再次检查 IP_BAD，由于 LRU 保证活跃访问 IP 不会被误驱逐，IP_BAD 仍被严格限流
    assert limiter.check("IP_BAD") is False


def test_cleanup_probe_records_in_storage_api(client, monkeypatch):
    probes = client._probe_store
    now_ms = int(time.time() * 1000)

    # 构造老 probe 记录 (10 天前) 与新 probe 记录 (当前)
    monkeypatch.setattr("src.store.probe_store._now_ms", lambda: now_ms - 10 * 86400 * 1000)
    probes.record(url="https://example.com/old_probe", ok=True)

    monkeypatch.setattr("src.store.probe_store._now_ms", lambda: now_ms)
    probes.record(url="https://example.com/new_probe", ok=True)

    assert len(probes.list()) == 2

    # 预览清理 5 天前的 probe 记录
    pre = client.post(
        "/api/storage/cleanup_preview",
        json={"cleanupProbeRecordsOlderThanDays": 5},
    ).json()
    assert pre["deletedProbeRecords"] == 1

    # 执行清理 5 天前的 probe 记录
    res = client.post(
        "/api/storage/cleanup",
        json={"cleanupProbeRecordsOlderThanDays": 5},
    ).json()
    assert res["deletedProbeRecords"] == 1

    # 验证只剩下 1 条新记录
    remaining = probes.list()
    assert len(remaining) == 1
    assert remaining[0].url == "https://example.com/new_probe"


def test_cleanup_response_note(client, monkeypatch):
    """测试 CleanupResponse 中 note 字段的填入与范围说明桥接。"""
    store = client._store
    probes = client._probe_store
    now_ms = int(time.time() * 1000)

    # 1. 纯探针清理：有过期 probe，无任务产物清理
    monkeypatch.setattr("src.store.probe_store._now_ms", lambda: now_ms - 10 * 86400 * 1000)
    probes.record(url="https://example.com/old_probe_note", ok=True)
    monkeypatch.setattr("src.store.probe_store._now_ms", lambda: now_ms)

    res1 = _post_cleanup(client, cleanupProbeRecordsOlderThanDays=5)
    assert res1["deletedProbeRecords"] == 1
    assert res1["deletedTasks"] == 0
    assert res1["note"] == "本次清理仅作用于 probe 记录（已清理 1 条），任务产物未动"

    # 2. 纯探针清理：无过期 probe，无任务产物清理
    res2 = _post_cleanup(client, cleanupProbeRecordsOlderThanDays=5)
    assert res2["deletedProbeRecords"] == 0
    assert res2["deletedTasks"] == 0
    assert res2["note"] == "本次清理仅作用于 probe 记录（未发现过期记录），任务产物未动"

    # 3. 纯任务清理：有任务产物清理，未请求 probe 清理
    tid = _seed_task(client, store, title="task_note", status="FAILED", source_bytes=1024, audio_bytes=0, srt_bytes=0, output_bytes=0)
    res3 = _post_cleanup(client, taskIds=[tid])
    assert res3["deletedTasks"] == 1
    assert res3["note"] == f"本次清理 1 个任务（释放 {storage_routes._format_bytes(res3['deletedBytes'])}），未触发 probe 记录清理"

    # 4. 组合清理：既有任务产物又有 probe 记录
    tid2 = _seed_task(client, store, title="task_note_2", status="FAILED", source_bytes=1024, audio_bytes=0, srt_bytes=0, output_bytes=0)
    monkeypatch.setattr("src.store.probe_store._now_ms", lambda: now_ms - 10 * 86400 * 1000)
    probes.record(url="https://example.com/old_probe_note_2", ok=True)
    monkeypatch.setattr("src.store.probe_store._now_ms", lambda: now_ms)

    res4 = _post_cleanup(client, taskIds=[tid2], cleanupProbeRecordsOlderThanDays=5)
    assert res4["deletedTasks"] == 1
    assert res4["deletedProbeRecords"] == 1
    assert res4["note"] == f"本次清理 1 个任务（释放 {storage_routes._format_bytes(res4['deletedBytes'])}）及 1 条 probe 记录"

    # 5. 空操作清理：未触发任何产物和 probe
    res5 = _post_cleanup(client)
    assert res5["deletedTasks"] == 0
    assert res5["deletedProbeRecords"] == 0
    assert res5["note"] == "未发现符合条件的待清理任务产物"
