"""字幕编辑路由（parse / save / re-burn）单测。

不真跑 ffmpeg：re-burn 仅替换核心 FFmpeg 调用，仍经真实 PluginManager 校验。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.handler import subtitle_editor as editor_routes
from src.handler.app import app
from src.handler.deps import get_store
from src.store import TaskStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    from src.handler import storage as storage_routes
    storage_routes._cleanup_limiter.reset()
    store = TaskStore(tmp_path / "test.db")
    app.dependency_overrides[get_store] = lambda: store
    # 把 task_dir 指向临时根，避免污染真实 data/
    monkeypatch.setattr(editor_routes, "task_dir", lambda tid: tmp_path / tid)
    from src.service import asset_resolver, subtitle_reburn
    from src.core import subtitle_burner
    monkeypatch.setattr(asset_resolver, "task_dir", lambda tid: tmp_path / tid)
    monkeypatch.setattr(subtitle_reburn, "task_dir", lambda tid: tmp_path / tid)
    monkeypatch.setattr(subtitle_burner, "ensure_task_dir", lambda tid: tmp_path / tid)

    # 创建一条带字幕的任务
    cid = store.create(
        url="https://example.com/v",
        source_lang="en",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="small",
        engine="deepseek",
    ).id
    store.update(cid, status="SUCCESS", progress=100)
    d = tmp_path / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "original.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,500\nHello\n\n"
        "2\n00:00:01,500 --> 00:00:03,000\nWorld\n",
        encoding="utf-8",
    )
    (d / "translated.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,500\n你好\n\n"
        "2\n00:00:01,500 --> 00:00:03,000\n世界\n",
        encoding="utf-8",
    )

    with TestClient(app) as c:
        c._store = store
        c._tid = cid
        c._tmp = tmp_path
        yield c
    app.dependency_overrides.clear()


# ---------- GET /api/tasks/{id}/subtitles ----------

def test_get_subtitles_returns_both_locales(client):
    r = client.get(f"/api/tasks/{client._tid}/subtitles")
    assert r.status_code == 200
    data = r.json()
    assert data["taskId"] == client._tid
    assert data["hasOriginal"] is True
    assert data["hasTranslated"] is True
    assert len(data["original"]) == 2
    assert len(data["translated"]) == 2
    # 字段命名对齐前端
    assert data["original"][0]["text"] == "Hello"
    assert data["original"][0]["start"] == 0.0
    assert data["original"][0]["end"] == 1.5
    # 每条都应有 id 字段供前端稳定引用
    assert all("id" in e and e["id"].startswith("sub_") for e in data["original"])


def test_get_subtitles_409_when_neither_exists(tmp_path):
    """两个 SRT 都不存在时返回 409，提示用户先跑流水线。"""
    store = TaskStore(tmp_path / "test.db")
    app.dependency_overrides[get_store] = lambda: store

    cid = store.create(
        url="https://example.com/v",
        source_lang="en",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="small",
        engine="deepseek",
    ).id
    (tmp_path / cid).mkdir(parents=True, exist_ok=True)
    # 不写任何 srt

    with TestClient(app) as c:
        r = c.get(f"/api/tasks/{cid}/subtitles")
    assert r.status_code == 409
    app.dependency_overrides.clear()


def test_get_subtitles_only_original(client, tmp_path):
    """只生成 original 时，translated 数组为空但 hasTranslated=False。"""
    (tmp_path / client._tid / "translated.srt").unlink()
    r = client.get(f"/api/tasks/{client._tid}/subtitles")
    assert r.status_code == 200
    data = r.json()
    assert data["hasOriginal"] is True
    assert data["hasTranslated"] is False
    assert data["translated"] == []


def test_get_subtitles_404(client):
    assert client.get("/api/tasks/nope/subtitles").status_code == 404


# ---------- PUT /api/tasks/{id}/subtitles ----------

def test_save_subtitles_overwrites_default(client):
    """不传 version 时，覆盖 translated.srt。"""
    body = {
        "locale": "translated",
        "entries": [
            {"id": "a1", "index": 1, "start": 0.0, "end": 1.0, "text": "早上好"},
            {"id": "a2", "index": 2, "start": 1.0, "end": 2.0, "text": "再见"},
        ],
    }
    r = client.put(f"/api/tasks/{client._tid}/subtitles", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["path"] == "translated.srt"
    assert data["count"] == 2

    # 落盘文件被覆盖
    content = (client._tmp / client._tid / "translated.srt").read_text(encoding="utf-8")
    assert "早上好" in content
    assert "再见" in content
    # 旧内容已不再出现（"你好" 是 fixture 里的旧文本）
    assert "你好" not in content

    # 再次 GET 应当读到新内容
    r2 = client.get(f"/api/tasks/{client._tid}/subtitles")
    texts = [e["text"] for e in r2.json()["translated"]]
    assert texts == ["早上好", "再见"]


def test_save_subtitles_version_writes_sidecar(client):
    """传 version=v2 时，写入 translated.v2.srt，原文件保留。"""
    body = {
        "locale": "translated",
        "version": "v2",
        "entries": [
            {"id": "x", "index": 1, "start": 0.0, "end": 1.0, "text": "新版本"},
        ],
    }
    r = client.put(f"/api/tasks/{client._tid}/subtitles", json=body)
    assert r.status_code == 200
    assert r.json()["path"] == "translated.v2.srt"

    d = client._tmp / client._tid
    assert (d / "translated.v2.srt").exists()
    # 原文件保留
    assert (d / "translated.srt").exists()
    assert "世界" in (d / "translated.srt").read_text(encoding="utf-8")


def test_save_subtitles_preserves_gbk_encoding(client):
    """当原文件为 GBK 编码时，保存更新后仍保持 GBK 编码。"""
    d = client._tmp / client._tid
    gbk_file = d / "original.srt"
    gbk_content = "1\n00:00:00,000 --> 00:00:01,500\n旧GBK文本\n".encode("gbk")
    gbk_file.write_bytes(gbk_content)

    body = {
        "locale": "original",
        "entries": [
            {"id": "g1", "index": 1, "start": 0.0, "end": 1.5, "text": "新GBK编辑文本"},
        ],
    }
    r = client.put(f"/api/tasks/{client._tid}/subtitles", json=body)
    assert r.status_code == 200

    raw_saved = gbk_file.read_bytes()
    # 验证保存后的文本能用 GBK 成功解码，且包含新文本
    saved_text = raw_saved.decode("gbk")
    assert "新GBK编辑文本" in saved_text


def test_save_subtitles_sorts_by_start(client):
    """前端发乱序条目，磁盘上也应按 start 升序写回。"""
    body = {
        "locale": "translated",
        "entries": [
            {"id": "2", "index": 99, "start": 5.0, "end": 6.0, "text": "later"},
            {"id": "1", "index": 1, "start": 0.5, "end": 1.0, "text": "earlier"},
        ],
    }
    r = client.put(f"/api/tasks/{client._tid}/subtitles", json=body)
    assert r.status_code == 200

    content = (client._tmp / client._tid / "translated.srt").read_text(encoding="utf-8")
    assert content.index("earlier") < content.index("later")


def test_save_subtitles_rejects_invalid_locale(client):
    body = {
        "locale": "junk",
        "entries": [{"id": "1", "index": 1, "start": 0.0, "end": 1.0, "text": "x"}],
    }
    r = client.put(f"/api/tasks/{client._tid}/subtitles", json=body)
    assert r.status_code == 422


def test_save_subtitles_rejects_end_before_start(client):
    body = {
        "locale": "translated",
        "entries": [
            {"id": "1", "index": 1, "start": 5.0, "end": 1.0, "text": "x"},
        ],
    }
    r = client.put(f"/api/tasks/{client._tid}/subtitles", json=body)
    assert r.status_code == 422


def test_save_subtitles_rejects_unsafe_version(client):
    body = {
        "locale": "translated",
        "version": "../../etc/passwd",
        "entries": [{"id": "1", "index": 1, "start": 0.0, "end": 1.0, "text": "x"}],
    }
    r = client.put(f"/api/tasks/{client._tid}/subtitles", json=body)
    assert r.status_code == 422


def test_save_subtitles_rejects_empty_entries(client):
    """空字幕不能保存，避免下游生成无字幕的假成功任务。"""
    original = (client._tmp / client._tid / "translated.srt").read_text(encoding="utf-8")

    r = client.put(
        f"/api/tasks/{client._tid}/subtitles",
        json={"locale": "translated", "entries": []},
    )

    assert r.status_code == 422
    assert (client._tmp / client._tid / "translated.srt").read_text(encoding="utf-8") == original


def test_save_subtitles_404(client):
    r = client.put(
        "/api/tasks/nope/subtitles",
        json={"locale": "translated", "entries": [{"id": "1", "index": 1, "start": 0.0, "end": 1.0, "text": "x"}]},
    )
    assert r.status_code == 404


def test_save_subtitles_releases_lock_on_disk_error(client, monkeypatch):
    """写盘异常也必须释放底层锁并移除任务锁条目。"""
    from src.handler.subtitle_editor import _write_locks

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(editor_routes, "write_srt", fail_write)
    body = {
        "locale": "translated",
        "entries": [{"id": "1", "index": 1, "start": 0.0, "end": 1.0, "text": "x"}],
    }

    with pytest.raises(OSError, match="disk full"):
        client.put(f"/api/tasks/{client._tid}/subtitles", json=body)

    assert client._tid not in _write_locks


@pytest.mark.parametrize("status", ["PENDING", "TRANSLATING", "BURNING", "DUBBING"])
def test_save_subtitles_rejects_active_tasks(client, status):
    client._store.update(client._tid, status=status)
    target = client._tmp / client._tid / "translated.srt"
    before = target.read_bytes()
    response = client.put(f"/api/tasks/{client._tid}/subtitles", json={
        "locale": "translated",
        "entries": [{"index": 1, "start": 0, "end": 1, "text": "blocked"}],
    })
    assert response.status_code == 409
    assert target.read_bytes() == before
    assert client._store.get(client._tid).status == status


def test_save_subtitles_rechecks_status_after_lock_acquisition(client, monkeypatch):
    from contextlib import contextmanager

    original_lock = editor_routes.task_write_lock
    target = client._tmp / client._tid / "translated.srt"
    before = target.read_bytes()

    @contextmanager
    def lock_then_transition(task_id):
        with original_lock(task_id):
            client._store.update(task_id, status="PENDING")
            yield

    monkeypatch.setattr(editor_routes, "task_write_lock", lock_then_transition)
    response = client.put(f"/api/tasks/{client._tid}/subtitles", json={
        "locale": "translated", "entries": [{"index": 1, "start": 0, "end": 1, "text": "x"}],
    })
    assert response.status_code == 409
    assert target.read_bytes() == before


def test_save_subtitles_rolls_back_file_when_db_finish_fails(client, monkeypatch):
    target = client._tmp / client._tid / "translated.srt"
    before = target.read_bytes()
    original_finish = client._store.finish_operation
    calls = []

    def finish(task_id, token, **fields):
        calls.append(token)
        if len(calls) == 1:
            raise OSError("database full")
        return original_finish(task_id, token, **fields)

    monkeypatch.setattr(client._store, "finish_operation", finish)
    with pytest.raises(OSError, match="database full"):
        client.put(f"/api/tasks/{client._tid}/subtitles", json={
            "locale": "translated", "entries": [{"index": 1, "start": 0, "end": 1, "text": "x"}],
        })
    assert target.read_bytes() == before
    assert client._store.get(client._tid).status == "SUCCESS"
    assert not list(target.parent.glob(".subtitle-edit-*"))
    assert not list(target.parent.glob(".reburn-backup-*"))


# ---------- POST /api/tasks/{id}/subtitles/burn ----------

def test_reburn_calls_burn_subtitles(client, tmp_path, monkeypatch):
    """re-burn 应基于 translated.srt + source.* 调用 burn_subtitles。"""
    called = {}

    def fake_burn(video, srt, task_id, on_progress=None, *, mode="hard", output_path=None):
        called["video"] = str(video)
        called["srt"] = str(srt)
        called["task_id"] = task_id
        called["mode"] = mode
        out = Path(output_path)
        assert out.name.startswith(".reburn-")
        out.write_bytes(b"\x00\x00\x00\x18ftypisomNEW-VIDEO")
        from src.core.subtitle_burner import BurnResult
        return BurnResult(output_path=out, mode=mode, filesize=out.stat().st_size)

    monkeypatch.setattr("src.core.subtitle_burner.burn_subtitles", fake_burn)

    # 准备 source.mp4，否则路由会 409
    (tmp_path / client._tid / "source.mp4").write_bytes(b"OLD")

    r = client.post(f"/api/tasks/{client._tid}/subtitles/burn", json={})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["outputPath"] == "output_hard.mp4"
    assert called["video"].endswith("source.mp4")
    assert called["srt"].endswith("translated.srt")
    # 不传 mode 时按任务的 burn 设置（默认 hard）
    assert called["mode"] == "hard"

    # DB 里 output_video 字段也被刷新
    rec = client._store.get(client._tid)
    assert rec.output_video is not None
    assert rec.output_video.endswith("output_hard.mp4")


def test_reburn_accepts_mode_override(client, tmp_path, monkeypatch):
    """请求体里的 mode 覆盖任务默认 burn 设置。"""
    called = {}

    def fake_burn(video, srt, task_id, on_progress=None, *, mode="hard", output_path=None):
        called["mode"] = mode
        from src.core.subtitle_burner import BurnResult
        out = Path(output_path)
        out.write_bytes(b"\x00\x00\x00\x18ftypisom")
        return BurnResult(output_path=out, mode=mode, filesize=out.stat().st_size)

    monkeypatch.setattr("src.core.subtitle_burner.burn_subtitles", fake_burn)
    (tmp_path / client._tid / "source.mp4").write_bytes(b"OLD")

    r = client.post(f"/api/tasks/{client._tid}/subtitles/burn", json={"mode": "soft"})
    assert r.status_code == 200
    assert called["mode"] == "soft"


def test_reburn_409_when_no_srt(client, tmp_path):
    (client._tmp / client._tid / "translated.srt").unlink()
    (tmp_path / client._tid / "source.mp4").write_bytes(b"OLD")
    r = client.post(f"/api/tasks/{client._tid}/subtitles/burn", json={})
    assert r.status_code == 409


def test_reburn_409_when_no_source(client):
    r = client.post(f"/api/tasks/{client._tid}/subtitles/burn", json={})
    assert r.status_code == 409


def test_reburn_400_on_burn_error(client, tmp_path, monkeypatch):
    """burn_subtitles 抛 BurnError 时路由应返回 400。"""
    from src.core.subtitle_burner import BurnError

    def fake_burn(video, srt, task_id, on_progress=None, *, mode="hard", output_path=None):
        raise BurnError("测试失败")

    monkeypatch.setattr("src.core.subtitle_burner.burn_subtitles", fake_burn)
    (tmp_path / client._tid / "source.mp4").write_bytes(b"OLD")

    r = client.post(f"/api/tasks/{client._tid}/subtitles/burn", json={})
    assert r.status_code == 400
    assert "测试失败" in r.json()["detail"]


def test_write_locks_leak_cleanup(client, monkeypatch):
    """测试创建任务并保存字幕后触发锁生成，然后删除任务或清理任务，验证 _write_locks 是否成功回收。"""
    from src.handler.subtitle_editor import _write_locks, _lock_for
    from src.handler import storage as storage_routes

    # 重写 task_dir 为 fake_dir 以便 storage.py 能正确定位到 tmp_path 目录下的任务
    def fake_dir(tid: str):
        return client._tmp / tid
    monkeypatch.setattr(storage_routes, "task_dir", fake_dir)

    # 1. 直接触发锁创建
    task_id = client._tid
    lock = _lock_for(task_id)
    assert task_id in _write_locks

    # 2. 调用 DELETE 接口删除任务，验证锁已被移除（需处于终态）
    client._store.update(task_id, status="SUCCESS")
    r = client.delete(f"/api/tasks/{task_id}")
    assert r.status_code == 204
    assert task_id not in _write_locks

    # 3. 再创建一个新任务，测试 storage/cleanup 清理是否也回收锁
    new_tid = client._store.create(
        url="https://example.com/v2",
        source_lang="en",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="small",
        engine="deepseek",
        title="another_task"
    ).id
    client._store.update(new_tid, status="SUCCESS", progress=100)

    # 建立对应目录和产物
    d = client._tmp / new_tid
    d.mkdir(parents=True, exist_ok=True)
    from src.config import SOURCE_VIDEO_STEM
    (d / f"{SOURCE_VIDEO_STEM}.mp4").write_bytes(b"VIDEO")

    # 触发锁创建
    _lock_for(new_tid)
    assert new_tid in _write_locks

    # 调用 POST /api/storage/cleanup
    cleanup_payload = {"taskIds": [new_tid]}
    r_cleanup = client.post("/api/storage/cleanup", json=cleanup_payload)
    assert r_cleanup.status_code == 200
    assert r_cleanup.json()["deletedTasks"] == 1

    # 验证锁字典已经被清除了该任务的锁，防止内存泄漏
    assert new_tid not in _write_locks


def test_storage_partial_cleanup_releases_lock(client, monkeypatch):
    """测试 storage/cleanup partial 模式清理时也会释放写锁。"""
    from src.handler.subtitle_editor import _write_locks, _lock_for
    from src.handler import storage as storage_routes

    def fake_dir(tid: str):
        return client._tmp / tid
    monkeypatch.setattr(storage_routes, "task_dir", fake_dir)

    task_id = client._tid
    client._store.update(task_id, status="SUCCESS", progress=100)

    # 确保任务目录下有 audio 文件供 partial 清理匹配
    d = client._tmp / task_id
    from src.config import AUDIO_FILENAME
    (d / AUDIO_FILENAME).write_bytes(b"AUDIO")

    _lock_for(task_id)
    assert task_id in _write_locks

    # 执行 partial 清理（例如只清理 audio）
    r_cleanup = client.post("/api/storage/cleanup", json={"taskIds": [task_id], "kinds": ["audio"]})
    assert r_cleanup.status_code == 200
    assert r_cleanup.json()["deletedTasks"] == 0
    assert task_id not in _write_locks


def test_write_locks_capacity_pruning(monkeypatch):
    """测试锁字典在达到容量上限时自动清理未占用的闲置锁（ref_count <= 0）。"""
    from src.handler.subtitle_editor import _write_locks, _lock_for, release_lock, task_write_lock

    # 设置小容量上限便于测试
    monkeypatch.setattr("src.handler.subtitle_editor._MAX_WRITE_LOCKS", 3)

    # 清理可能残留的锁
    for k in list(_write_locks.keys()):
        release_lock(k)

    _lock_for("t1")
    _lock_for("t2")
    _lock_for("t3")
    assert len(_write_locks) == 3

    # 使用 task_write_lock 使 t2 具有活跃 ref_count
    with task_write_lock("t2"):
        # 再次获取新任务锁，触发容量清理，t1/t3（ref_count <= 0）应被清理，t2（活跃持有中）与 t4 应保留
        _lock_for("t4")
        assert "t1" not in _write_locks
        assert "t3" not in _write_locks
        assert "t2" in _write_locks
        assert "t4" in _write_locks

    for k in list(_write_locks.keys()):
        release_lock(k)
