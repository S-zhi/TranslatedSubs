"""API 层单测。用 FastAPI TestClient + 隔离的临时 DB（依赖覆盖）。

执行（pipeline）在第 1 步是占位，所以这里只验证 CRUD / 文件下载 / 校验，
不涉及真实跑流水线。
"""

from __future__ import annotations

import errno
import io
import pathlib

import pytest
from fastapi.testclient import TestClient

from src.handler import tasks as tasks_routes
from src.handler import srt as srt_routes
from src.handler.app import app
from src.handler.deps import get_probe_store, get_store
from src.store import RESOURCE_STATUS_AVAILABLE, RESOURCE_STATUS_MISSING, TaskStore

from src.handler.schemas import to_out
from src.core.downloader import ProbeResult
from src.store import ProbeStore, TaskStore, TaskRecord



@pytest.fixture
def client(tmp_path, monkeypatch):
    import dataclasses
    db_path = tmp_path / "test.db"
    store = TaskStore(db_path)
    probe_store = ProbeStore(db_path)
    # 覆盖 store 依赖 -> 临时库
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_probe_store] = lambda: probe_store
    monkeypatch.setattr("src.service.runner._store", store)
    # 下载端点用 task_dir 定位文件 -> 指向临时目录
    monkeypatch.setattr(tasks_routes, "task_dir", lambda tid: tmp_path / tid)
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path / tid)
    # 不在 API 测试里真跑流水线（执行器单独测）
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", lambda task_id: None)
    # API tests exercise request validation and persistence; model download
    # readiness is covered by the local model endpoint tests.
    monkeypatch.setattr(tasks_routes, "_ensure_local_model_ready", lambda model: None)
    # 默认 mock probe_duration 返回 10 秒，避免单元测试依赖系统 ffprobe 命令
    monkeypatch.setattr(tasks_routes, "probe_duration", lambda path, bin_path: 10.0)
    # 默认 mock 提供 DeepSeek Key 及临时 data_dir，确保其它 Task 创建测试不受影响
    monkeypatch.setattr(
        tasks_routes,
        "settings",
        dataclasses.replace(tasks_routes.settings, deepseek_api_key="sk-mock-key", data_dir=tmp_path),
    )
    with TestClient(app) as c:
        c._store = store
        c._probe_store = probe_store
        c._tmp = tmp_path
        yield c
    app.dependency_overrides.clear()


def _payload(**over):
    body = {
        "url": "https://example.com/v",
        "sourceLang": "auto",
        "targetLang": "zh-CN",
        "mode": "mono",
        "burn": "hard",
        "model": "small",
        "engine": "deepseek",
    }
    body.update(over)
    return body


# ---------- 创建 ----------

def test_create_task(client):
    r = client.post("/api/tasks", json=_payload())
    assert r.status_code == 201
    data = r.json()
    assert data["id"].startswith("task_")
    assert data["status"] == "PENDING"
    assert data["progress"] == 0
    # camelCase 字段对齐前端
    assert data["sourceLang"] == "auto"
    assert data["targetLang"] == "zh-CN"
    assert data["outputs"] is None
    assert data["createdAt"] > 0
    assert data["ttsEnabled"] is False
    assert data["ttsStatus"] == "DISABLED"


def test_create_task_persists_tts_options(client):
    response = client.post("/api/tasks", json=_payload(
        ttsEnabled=True, ttsVoice="zm_010", originalVoiceMode="lower",
    ))
    assert response.status_code == 201
    data = response.json()
    record = client._store.get(data["id"])
    assert (record.tts_enabled, record.tts_voice, record.original_voice_mode) == (1, "zm_010", "lower")
    assert data["ttsStatus"] == "PENDING"


def test_tts_failure_keeps_regular_video_output(client):
    rec = TaskRecord(
        id="task_tts_failed", url="u", source_lang="en", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        status="SUCCESS", need_subtitle=1, tts_enabled=1,
        tts_status="FAILED", tts_error="model unavailable",
    )
    output = to_out(rec)
    assert output.outputs == {
        "video": "/api/tasks/task_tts_failed/download",
        "subtitle": "/api/tasks/task_tts_failed/subtitle",
    }
    assert output.ttsStatus == "FAILED"
    assert output.ttsError == "model unavailable"


# ---------- API Token 鉴权 ----------

def test_api_token_auth_when_token_configured(client, monkeypatch):
    """配置 SUBTRANS_API_TOKEN 时，变更接口必须校验 Authorization / X-API-Token / URL Token。"""
    monkeypatch.setenv("SUBTRANS_API_TOKEN", "secret-token-123")

    # 未提供 Token -> 401
    r_no_auth = client.post("/api/tasks", json=_payload())
    assert r_no_auth.status_code == 401
    assert "未提供有效的 API Token" in r_no_auth.json()["detail"]

    # 错误 Token -> 401
    r_bad_auth = client.post("/api/tasks", json=_payload(), headers={"Authorization": "Bearer wrong-token"})
    assert r_bad_auth.status_code == 401

    # Authorization: Bearer 正确 Token -> 201
    r_bearer = client.post("/api/tasks", json=_payload(), headers={"Authorization": "Bearer secret-token-123"})
    assert r_bearer.status_code == 201

    # X-API-Token 正确 Token -> 201
    r_header = client.post("/api/tasks", json=_payload(url="https://example.com/header-token"), headers={"X-API-Token": "secret-token-123"})
    assert r_header.status_code == 201


def test_api_token_auth_read_and_download_endpoints(client, monkeypatch):
    """配置 SUBTRANS_API_TOKEN 时，数据读取和删除接口均受保护。"""
    monkeypatch.delenv("SUBTRANS_API_TOKEN", raising=False)
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mp4").write_bytes(b"SRC")
    (d / "output.mp4").write_bytes(b"OUT")
    (d / "translated.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")

    monkeypatch.setenv("SUBTRANS_API_TOKEN", "secret-token-123")

    endpoints_get = [
        "/api/tasks",
        f"/api/tasks/{cid}",
        "/api/tasks/probe/records",
        f"/api/tasks/{cid}/source",
        f"/api/tasks/{cid}/download",
        f"/api/tasks/{cid}/subtitle",
        f"/api/tasks/{cid}/subtitles",
        "/api/storage/stats",
        "/api/storage/retention",
    ]

    endpoints_head = [
        f"/api/tasks/{cid}/source",
        f"/api/tasks/{cid}/download",
    ]
    endpoints_delete = ["/api/srt/local-models/small"]

    # 未带 Token 均返回 401
    for ep in endpoints_get:
        assert client.get(ep).status_code == 401, f"{ep} 未拦截 401"
    for ep in endpoints_head:
        assert client.head(ep).status_code == 401, f"HEAD {ep} 未拦截 401"
    for ep in endpoints_delete:
        assert client.delete(ep).status_code == 401, f"DELETE {ep} 未拦截 401"
    assert client.post(
        "/api/srt/local-models/import",
        data={"name": "external"},
        files=[("files", ("model.safetensors", b"weights"))],
    ).status_code == 401
    assert client.post("/api/srt/local-models/external/check").status_code == 401
    assert client.post("/api/srt/local-models/tiny/download").status_code == 401
    assert client.post("/api/storage/cleanup_preview", json={}).status_code == 401

    # 支持 Header Authorization: Bearer
    assert client.get("/api/tasks", headers={"Authorization": "Bearer secret-token-123"}).status_code == 200
    # 支持 Header X-API-Token
    assert client.get(f"/api/tasks/{cid}", headers={"X-API-Token": "secret-token-123"}).status_code == 200
    # 支持 Query ?token=
    assert client.get(f"/api/tasks/{cid}/download?token=secret-token-123").status_code == 200
    # 支持 Query ?api_token=
    assert client.get(f"/api/tasks/{cid}/subtitle?api_token=secret-token-123").status_code == 200


def test_api_token_auth_when_token_unset(client, monkeypatch):
    """未配置 SUBTRANS_API_TOKEN 时，读取和修改接口无须鉴权直接通过。"""
    monkeypatch.delenv("SUBTRANS_API_TOKEN", raising=False)

    r = client.post("/api/tasks", json=_payload())
    assert r.status_code == 201
    cid = r.json()["id"]

    assert client.get("/api/tasks").status_code == 200
    assert client.get(f"/api/tasks/{cid}").status_code == 200


def test_create_defaults_when_minimal(client):
    r = client.post("/api/tasks", json={"url": "https://x/y"})
    assert r.status_code == 201
    data = r.json()
    assert data["targetLang"] == "zh-CN" and data["mode"] == "mono"


def test_create_missing_url_422(client):
    r = client.post("/api/tasks", json={"targetLang": "zh-CN"})
    assert r.status_code == 422


def test_create_rejects_invalid_enum_params(client, monkeypatch):
    """非法枚举参数应在创建前返回 422，且不能入队。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    assert client.post("/api/tasks", json=_payload(mode="mixed")).status_code == 422
    assert client.post("/api/tasks", json=_payload(burn="weird")).status_code == 422
    assert client.post("/api/tasks", json=_payload(engine="other")).status_code == 422
    assert enqueued == []


def test_create_rejects_empty_model_and_languages(client, monkeypatch):
    """模型和语言字段为空时应在创建前返回 422，且不能入队。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    assert client.post("/api/tasks", json=_payload(model="")).status_code == 422
    assert client.post("/api/tasks", json=_payload(sourceLang="")).status_code == 422
    assert client.post("/api/tasks", json=_payload(targetLang="")).status_code == 422
    assert enqueued == []


def test_create_task_rejects_missing_deepseek_api_key(client, monkeypatch):
    """DeepSeek 引擎在需要字幕且未配置 API Key 时应返回 422 拒绝。"""
    import dataclasses
    monkeypatch.setattr(
        tasks_routes,
        "settings",
        dataclasses.replace(tasks_routes.settings, deepseek_api_key=None),
    )

    # 1. POST /api/tasks
    r1 = client.post("/api/tasks", json=_payload(engine="deepseek", needSubtitle=True))
    assert r1.status_code == 422
    assert "缺少 DeepSeek API Key" in r1.json()["detail"]

    # 2. POST /api/tasks/upload
    r2 = _upload(client, _filename="clip.mp4", engine="deepseek", needSubtitle="true")
    assert r2.status_code == 422
    assert "缺少 DeepSeek API Key" in r2.json()["detail"]


def test_create_task_allows_missing_deepseek_api_key_when_need_subtitle_false(client, monkeypatch):
    """不需要字幕（needSubtitle=False）时，即使未配置 DeepSeek Key 也能创建任务。"""
    import dataclasses
    monkeypatch.setattr(
        tasks_routes,
        "settings",
        dataclasses.replace(tasks_routes.settings, deepseek_api_key=None),
    )

    r = client.post("/api/tasks", json=_payload(engine="deepseek", needSubtitle=False))
    assert r.status_code == 201


def test_create_task_succeeds_when_deepseek_api_key_present(client, monkeypatch):
    """已配置 DeepSeek Key 时，创建任务成功。"""
    import dataclasses
    monkeypatch.setattr(
        tasks_routes,
        "settings",
        dataclasses.replace(tasks_routes.settings, deepseek_api_key="sk-valid-key"),
    )

    r = client.post("/api/tasks", json=_payload(engine="deepseek", needSubtitle=True))
    assert r.status_code == 201


def test_create_upload_task_persists_options_and_file(client, monkeypatch):
    """上传视频创建任务时，字幕模式与烧录方式应和链接任务一样入库透传。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = client.post(
        "/api/tasks/upload",
        data={
            "sourceLang": "en",
            "targetLang": "ja",
            "mode": "bilingual",
            "burn": "soft",
            "model": "medium",
            "engine": "deepseek",
            "needSubtitle": "true",
        },
        files={"file": ("clip.mp4", b"VIDEO", "video/mp4")},
    )

    assert r.status_code == 201
    data = r.json()
    assert data["sourceType"] == "upload"
    assert data["mode"] == "bilingual"
    assert data["burn"] == "soft"
    assert enqueued == [data["id"]]
    assert (client._tmp / data["id"] / "source.mp4").read_bytes() == b"VIDEO"


# ---------- 查询 ----------

def test_list_tasks(client):
    client.post("/api/tasks", json=_payload())
    client.post("/api/tasks", json=_payload(url="https://x/2"))
    r = client.get("/api/tasks")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_list_tasks_pagination_and_cursors(client):
    c1 = client.post("/api/tasks", json=_payload(url="https://x/1")).json()["id"]
    c2 = client.post("/api/tasks", json=_payload(url="https://x/2")).json()["id"]
    c3 = client.post("/api/tasks", json=_payload(url="https://x/3")).json()["id"]

    # 分页测试
    r_limit = client.get("/api/tasks", params={"limit": 2})
    assert r_limit.status_code == 200
    res_limit = r_limit.json()
    assert len(res_limit) == 2
    assert [t["id"] for t in res_limit] == [c3, c2]

    r_offset = client.get("/api/tasks", params={"limit": 2, "offset": 1})
    assert r_offset.status_code == 200
    res_offset = r_offset.json()
    assert [t["id"] for t in res_offset] == [c2, c1]

    # 游标测试 before_id (更早)
    r_before = client.get("/api/tasks", params={"before_id": c2})
    assert r_before.status_code == 200
    assert [t["id"] for t in r_before.json()] == [c1]

    # 游标测试 after_id (更晚)
    r_after = client.get("/api/tasks", params={"after_id": c2})
    assert r_after.status_code == 200
    assert [t["id"] for t in r_after.json()] == [c3]

    # limit 范围验证 (1~200)
    assert client.get("/api/tasks", params={"limit": 0}).status_code == 422
    assert client.get("/api/tasks", params={"limit": 201}).status_code == 422
    assert client.get("/api/tasks", params={"offset": -1}).status_code == 422


def test_get_task(client):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    r = client.get(f"/api/tasks/{cid}")
    assert r.status_code == 200 and r.json()["id"] == cid


def test_get_missing_404(client):
    assert client.get("/api/tasks/nope").status_code == 404


# ---------- 删除 ----------

def test_delete_task(client):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS")
    # 造个产物目录，验证会被清理
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.mp4").write_bytes(b"x")

    assert client.delete(f"/api/tasks/{cid}").status_code == 204
    assert client.get(f"/api/tasks/{cid}").status_code == 404
    assert not d.exists()


def test_delete_running_task_returns_409(client):
    """运行中状态（非 SUCCESS/FAILED）的任务禁止删除，返回 409。"""
    running_statuses = ["PENDING", "DOWNLOADING", "EXTRACTING", "TRANSCRIBING", "TRANSLATING", "BURNING"]
    for index, status in enumerate(running_statuses):
        cid = client.post("/api/tasks", json=_payload(url=f"https://example.com/running-{index}")).json()["id"]
        client._store.update(cid, status=status)
        d = client._tmp / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / "source.mp4").write_bytes(b"SRC")

        r = client.delete(f"/api/tasks/{cid}")
        assert r.status_code == 409
        assert "任务运行中" in r.json()["detail"]
        # 验证 DB 记录与产物目录均保留
        assert client.get(f"/api/tasks/{cid}").status_code == 200
        assert d.exists()


def test_delete_missing_404(client):
    assert client.delete("/api/tasks/nope").status_code == 404


# ---------- 取消与重试 ----------

def test_cancel_running_task_succeeds(client):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="TRANSLATING", progress=70, current_step="TRANSLATING")

    r = client.post(f"/api/tasks/{cid}/cancel")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "CANCELLED"
    assert data["error"] == "用户取消"


def test_cancel_terminal_task_returns_409(client):
    for status in ["SUCCESS", "FAILED", "CANCELLED"]:
        cid = client.post("/api/tasks", json=_payload()).json()["id"]
        client._store.update(cid, status=status)
        r = client.post(f"/api/tasks/{cid}/cancel")
        assert r.status_code == 409
        assert "任务非运行状态" in r.json()["detail"]


def test_cancel_nonexistent_task_returns_404(client):
    assert client.post("/api/tasks/nope/cancel").status_code == 404


def test_delete_cancelled_task_succeeds(client):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="CANCELLED", error="用户取消")
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)

    assert client.delete(f"/api/tasks/{cid}").status_code == 204
    assert client.get(f"/api/tasks/{cid}").status_code == 404
    assert not d.exists()


def test_delete_cancelling_task_returns_409(client):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="CANCELLED", is_cancelling=1)

    response = client.delete(f"/api/tasks/{cid}")

    assert response.status_code == 409
    assert response.json()["detail"] == "任务正在取消，请稍后再试"
    assert client.get(f"/api/tasks/{cid}").status_code == 200


def test_retry_cancelled_task_succeeds(client, monkeypatch):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="CANCELLED", error="用户取消")
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = client.post(f"/api/tasks/{cid}/retry")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "PENDING"
    assert data["progress"] == 0
    assert data["error"] is None
    assert enqueued == [cid]


def test_retry_resets_status(client, monkeypatch):
    """失败任务重试时应重置状态并重新入队。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="FAILED", progress=40, error="boom")
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = client.post(f"/api/tasks/{cid}/retry")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "PENDING"
    assert data["progress"] == 0
    assert data["error"] is None
    assert enqueued == [cid]


def test_retry_running_task_returns_409(client, monkeypatch):
    """运行中任务重试应返回 409 且不能重复入队。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="DOWNLOADING", progress=10, current_step="DOWNLOADING")
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = client.post(f"/api/tasks/{cid}/retry")

    assert r.status_code == 409
    assert enqueued == []
    rec = client._store.get(cid)
    assert rec.status == "DOWNLOADING"
    assert rec.progress == 10


# ---------- 文件下载 ----------

def test_download_409_when_not_ready(client):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    assert client.get(f"/api/tasks/{cid}/download").status_code == 409
    assert client.get(f"/api/tasks/{cid}/subtitle").status_code == 409


def test_download_serves_file(client):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.mp4").write_bytes(b"VIDEO")
    (d / "translated.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")

    r = client.get(f"/api/tasks/{cid}/download")
    assert r.status_code == 200 and r.content == b"VIDEO"
    assert client.head(f"/api/tasks/{cid}/download").status_code == 204
    r2 = client.get(f"/api/tasks/{cid}/subtitle")
    assert r2.status_code == 200 and "hi" in r2.text


def test_source_video_serves_original_when_output_exists(client):
    """预览源视频时必须返回 source.*，而不是优先返回烧录后的 output.mp4。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mp4").write_bytes(b"SOURCE")
    (d / "output.mp4").write_bytes(b"OUTPUT")

    r = client.get(f"/api/tasks/{cid}/source")
    assert r.status_code == 200
    assert r.content == b"SOURCE"
    assert client.head(f"/api/tasks/{cid}/source").status_code == 204


def test_source_video_409_does_not_hide_available_subtitled_output(client):
    """源视频缺失不应连带隐藏仍可播放的带字幕成品。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.mp4").write_bytes(b"OUTPUT")
    client._store.update(cid, status="SUCCESS", progress=100)

    r = client.get(f"/api/tasks/{cid}/source")
    assert r.status_code == 409
    assert "源视频文件缺失" in r.json()["detail"]
    assert client.head(f"/api/tasks/{cid}/source").status_code == 409
    assert client._store.get(cid).resource_status == RESOURCE_STATUS_AVAILABLE


def test_success_task_exposes_outputs(client):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)
    data = client.get(f"/api/tasks/{cid}").json()
    assert data["outputs"]["video"] == f"/api/tasks/{cid}/download"
    assert data["outputs"]["subtitle"] == f"/api/tasks/{cid}/subtitle"
    # 资源可用时显式标注 AVAILABLE
    assert data["resourceStatus"] == RESOURCE_STATUS_AVAILABLE


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_srt_languages(client, monkeypatch):
    """测试获取源语言选项。"""
    monkeypatch.setattr(
        "src.handler.srt.get_video_language_options",
        lambda: ["en", "zh"],
    )
    r = client.get("/api/srt/languages")
    assert r.status_code == 200
    assert r.json() == ["en", "zh"]


def test_srt_model_weights(client, monkeypatch):
    """测试获取 Whisper 模型权重选项。"""
    monkeypatch.setattr(
        "src.handler.srt.get_whisper_model_weight_options",
        lambda: ["tiny", "small"],
    )
    r = client.get("/api/srt/model-weights")
    assert r.status_code == 200
    assert r.json() == ["tiny", "small"]


def test_delete_local_model_is_blocked_while_an_active_task_uses_it(client, monkeypatch):
    class ModelManagerStub:
        deleted = []

        def delete(self, name):
            self.deleted.append(name)
            return {"name": name, "status": "NOT_INSTALLED"}

    manager = ModelManagerStub()
    monkeypatch.setattr(srt_routes, "model_manager", manager)
    task_id = client.post("/api/tasks", json=_payload(model="whisper:small")).json()["id"]

    blocked = client.delete("/api/srt/local-models/small")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "MODEL_IN_USE"
    assert manager.deleted == []

    client._store.update(task_id, status="SUCCESS")
    deleted = client.delete("/api/srt/local-models/small")
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "NOT_INSTALLED"
    assert manager.deleted == ["small"]


def test_import_and_check_local_model_api(client, monkeypatch, tmp_path):
    from src.service import model_manager as models

    class Manager(models.LocalModelManager):
        @property
        def root(self):
            path = tmp_path / "models"
            path.mkdir(exist_ok=True)
            return path

    manager = Manager()
    monkeypatch.setattr(srt_routes, "model_manager", manager)
    monkeypatch.setattr(models, "validate_hf_model", lambda _path: None)
    response = client.post(
        "/api/srt/local-models/import",
        data={"name": "external-whisper", "label": "External Whisper"},
        files=[("files", ("pytorch_model.bin", io.BytesIO(b"weights"), "application/octet-stream"))],
    )
    assert response.status_code == 200
    assert response.json()["format"] == "huggingface"
    assert client.post("/api/srt/local-models/external-whisper/check").status_code == 200

    def invalid(_path):
        raise models.ModelValidationError("invalid contract")

    monkeypatch.setattr(models, "validate_hf_model", invalid)
    failed = client.post("/api/srt/local-models/external-whisper/check")
    assert failed.status_code == 422
    assert failed.json()["detail"]["cleaned"] is True
    assert not (manager.root / "external-whisper").exists()


def test_official_model_download_endpoint(client, monkeypatch):
    class ModelManagerStub:
        def download(self, name):
            return {"name": name, "status": "DOWNLOADING", "phase": "queued"}

    monkeypatch.setattr(srt_routes, "model_manager", ModelManagerStub())
    response = client.post("/api/srt/local-models/tiny.en/download")
    assert response.status_code == 200
    assert response.json()["status"] == "DOWNLOADING"
    assert client.post("/api/srt/local-models/not-official/download").status_code == 404


def test_srt_target_languages(client):
    """测试获取配置/后端下发的目标语言选项。"""
    from src.config import settings
    r = client.get("/api/srt/target-languages")
    assert r.status_code == 200
    assert r.json() == list(settings.target_languages)


# ---------- issue #22：终态任务产物丢失 ----------


def test_success_task_with_missing_video_hides_outputs(client):
    """SUCCESS 任务但 output.mp4 不在 → outputs 必须为空，resourceStatus=MISSING。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    # 模拟服务重启：创建一个全新的 TaskStore 读同一个 db
    fresh = TaskStore(client._store.db_path)
    # 故意不创建任何产物文件
    marked = tasks_routes.scan_missing_terminal(fresh, data_dir=client._tmp)
    assert marked == [cid]

    data = client.get(f"/api/tasks/{cid}").json()
    assert data["status"] == "SUCCESS"  # 任务历史状态保留
    assert data["resourceStatus"] == RESOURCE_STATUS_MISSING
    assert data["outputs"] is None
    assert data["error"] == "资源已删除"
    assert data["downgradeReason"] == "UNKNOWN"
    assert data["downgradedAt"] > 0


def test_scan_missing_terminal_records_disk_failure(client):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.mp4").write_bytes(b"")
    (d / "translated.srt").write_bytes(b"")

    assert tasks_routes.scan_missing_terminal(client._store, data_dir=client._tmp) == [cid]
    rec = client._store.get(cid)
    assert rec.downgrade_reason == "DISK_FAILURE"
    assert rec.error == "资源不可读"


def test_scan_missing_terminal_records_volume_migration(client, tmp_path):
    ids = [
        client.post("/api/tasks", json=_payload(url=f"https://x/{i}")).json()["id"]
        for i in range(2)
    ]
    for task_id in ids:
        client._store.update(task_id, status="SUCCESS", progress=100)

    missing_root = tmp_path / "unmounted-data"
    marked = tasks_routes.scan_missing_terminal(client._store, data_dir=missing_root)
    assert set(marked) == set(ids)
    assert {client._store.get(task_id).downgrade_reason for task_id in ids} == {"VOLUME_MIGRATED"}



def test_scan_missing_terminal_not_a_directory_error(client, tmp_path, caplog):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    # 造一个同名文件代替目录
    not_a_dir = tmp_path / "file-as-datadir"
    not_a_dir.write_text("i am a file")

    with caplog.at_level("WARNING"):
        marked = tasks_routes.scan_missing_terminal(client._store, data_dir=not_a_dir)

    assert marked == [cid]
    rec = client._store.get(cid)
    assert rec.resource_status == RESOURCE_STATUS_MISSING
    assert rec.downgrade_reason == "USER_CLEANED"
    assert rec.downgrade_errno is not None
    assert "scan_missing_terminal data_dir 异常" in caplog.text

    data = client.get(f"/api/tasks/{cid}").json()
    assert data["downgradeReason"] == "USER_CLEANED"
    assert data["downgradeErrno"] == rec.downgrade_errno


def test_scan_missing_terminal_stat_os_error(client, tmp_path, monkeypatch, caplog):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)
    data_dir = tmp_path / "stat-error-dir"
    data_dir.mkdir()

    original_stat = pathlib.Path.stat

    def fake_stat(path, *args, **kwargs):
        if path == data_dir:
            raise OSError(errno.EIO, "Input/output error")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr("pathlib.Path.stat", fake_stat)

    with caplog.at_level("WARNING"):
        marked = tasks_routes.scan_missing_terminal(client._store, data_dir=data_dir)

    assert marked == [cid]
    rec = client._store.get(cid)
    assert rec.downgrade_reason == "DISK_FAILURE"
    assert rec.downgrade_errno == errno.EIO
    assert "scan_missing_terminal data_dir 异常" in caplog.text


def test_scan_missing_terminal_permission_error(client, tmp_path, monkeypatch, caplog):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    data_dir = tmp_path / "perm-denied-dir"
    data_dir.mkdir()

    def fake_iterdir(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("pathlib.Path.iterdir", fake_iterdir)

    with caplog.at_level("WARNING"):
        marked = tasks_routes.scan_missing_terminal(client._store, data_dir=data_dir)

    assert marked == [cid]
    rec = client._store.get(cid)
    assert rec.resource_status == RESOURCE_STATUS_MISSING
    assert rec.downgrade_reason == "DISK_FAILURE"
    assert rec.downgrade_errno == 13
    assert "scan_missing_terminal data_dir 异常" in caplog.text


def test_scan_missing_terminal_os_error_io(client, tmp_path, monkeypatch, caplog):
    import errno
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    data_dir = tmp_path / "eio-dir"
    data_dir.mkdir()

    def fake_iterdir(*args, **kwargs):
        err = OSError("Input/output error")
        err.errno = errno.EIO
        raise err

    monkeypatch.setattr("pathlib.Path.iterdir", fake_iterdir)

    with caplog.at_level("WARNING"):
        marked = tasks_routes.scan_missing_terminal(client._store, data_dir=data_dir)

    assert marked == [cid]
    rec = client._store.get(cid)
    assert rec.resource_status == RESOURCE_STATUS_MISSING
    assert rec.downgrade_reason == "DISK_FAILURE"
    assert rec.downgrade_errno == errno.EIO
    assert "scan_missing_terminal data_dir 异常" in caplog.text


def test_scan_missing_terminal_os_error_generic(client, tmp_path, monkeypatch, caplog):
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    data_dir = tmp_path / "generic-oserror-dir"
    data_dir.mkdir()

    def fake_iterdir(*args, **kwargs):
        err = OSError("Unknown error")
        err.errno = 999
        raise err

    monkeypatch.setattr("pathlib.Path.iterdir", fake_iterdir)

    with caplog.at_level("WARNING"):
        marked = tasks_routes.scan_missing_terminal(client._store, data_dir=data_dir)

    assert marked == [cid]
    rec = client._store.get(cid)
    assert rec.resource_status == RESOURCE_STATUS_MISSING
    assert rec.downgrade_reason == "UNKNOWN"
    assert rec.downgrade_errno == 999
    assert "scan_missing_terminal data_dir 异常" in caplog.text

def test_scan_missing_terminal_empty_data_dir_does_not_mark_volume_migration(client, tmp_path):
    """数据目录存在但为空（新装系统/空挂载）时，有多条 SUCCESS 任务不应被误标记为 VOLUME_MIGRATED。"""
    ids = [
        client.post("/api/tasks", json=_payload(url=f"https://x/{i}")).json()["id"]
        for i in range(2)
    ]
    for task_id in ids:
        client._store.update(task_id, status="SUCCESS", progress=100)

    empty_dir = tmp_path / "empty_data"
    empty_dir.mkdir(parents=True, exist_ok=True)

    marked = tasks_routes.scan_missing_terminal(client._store, data_dir=empty_dir)
    assert set(marked) == set(ids)
    assert {client._store.get(task_id).downgrade_reason for task_id in ids} == {"UNKNOWN"}



def test_success_download_only_task_with_missing_source_hides_outputs(client):
    """needSubtitle=False 的"仅下载"任务丢失 source.* 也算资源已丢失。"""
    cid = client.post("/api/tasks", json=_payload(needSubtitle=False)).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    # 不落 source.* 文件，直接扫
    marked = tasks_routes.scan_missing_terminal(client._store, data_dir=client._tmp)
    assert marked == [cid]

    data = client.get(f"/api/tasks/{cid}").json()
    assert data["resourceStatus"] == RESOURCE_STATUS_MISSING
    assert data["outputs"] is None


def test_scan_missing_terminal_is_idempotent(client):
    """重复跑扫描不会再次写库，状态保持稳定。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    assert tasks_routes.scan_missing_terminal(client._store, data_dir=client._tmp) == [cid]
    # 第二次扫描，状态已经是 MISSING，应该返回空列表并不修改 updated_at 之外的字段
    rec_before = client._store.get(cid)
    assert tasks_routes.scan_missing_terminal(client._store, data_dir=client._tmp) == []
    rec_after = client._store.get(cid)
    # updated_at 在第二次扫描不应该被刷新（因为没有写）
    assert rec_before.updated_at == rec_after.updated_at
    assert rec_after.resource_status == RESOURCE_STATUS_MISSING


def test_scan_missing_terminal_single_task_id(client):
    """传入 task_id 时仅扫描并降级指定任务，不影响其它任务。"""
    cid1 = client.post("/api/tasks", json=_payload(url="https://x/1")).json()["id"]
    cid2 = client.post("/api/tasks", json=_payload(url="https://x/2")).json()["id"]

    client._store.update(cid1, status="SUCCESS", progress=100)
    client._store.update(cid2, status="SUCCESS", progress=100)

    # 仅针对 cid1 扫描
    marked = tasks_routes.scan_missing_terminal(client._store, task_id=cid1, data_dir=client._tmp)
    assert marked == [cid1]

    # cid1 被降级，cid2 依然保持 AVAILABLE
    rec1 = client._store.get(cid1)
    rec2 = client._store.get(cid2)
    assert rec1.resource_status == RESOURCE_STATUS_MISSING
    assert rec2.resource_status == RESOURCE_STATUS_AVAILABLE


def test_scan_missing_terminal_skips_non_success(client):
    """非 SUCCESS 终态（如 FAILED）即使文件不存在也不应被降级。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="FAILED", progress=40, error="boom")

    assert tasks_routes.scan_missing_terminal(client._store, data_dir=client._tmp) == []
    rec = client._store.get(cid)
    assert rec.status == "FAILED"
    assert rec.resource_status == RESOURCE_STATUS_AVAILABLE
    assert rec.error == "boom"


def test_scan_missing_terminal_keeps_success_when_artifacts_present(client):
    """SUCCESS + 文件齐备时不应被误判为 MISSING。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.mp4").write_bytes(b"VIDEO")
    (d / "translated.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")

    assert tasks_routes.scan_missing_terminal(client._store, data_dir=client._tmp) == []
    data = client.get(f"/api/tasks/{cid}").json()
    assert data["resourceStatus"] == RESOURCE_STATUS_AVAILABLE
    assert data["outputs"]["video"] == f"/api/tasks/{cid}/download"


def test_download_missing_video_marks_resource_missing(client):
    """下载端点遇到 SUCCESS 任务但文件不在，应降级为 MISSING 并返回'资源已删除'。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    r = client.get(f"/api/tasks/{cid}/download")
    assert r.status_code == 409
    assert r.json()["detail"] == "资源已删除"

    rec = client._store.get(cid)
    assert rec.resource_status == RESOURCE_STATUS_MISSING
    # 详情接口同步反映新状态
    data = client.get(f"/api/tasks/{cid}").json()
    assert data["resourceStatus"] == RESOURCE_STATUS_MISSING
    assert data["outputs"] is None


def test_download_missing_subtitle_marks_resource_missing(client):
    """仅字幕缺失：下载视频成功，下载字幕时降级。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.mp4").write_bytes(b"VIDEO")
    # 不写 translated.srt
    client._store.update(cid, status="SUCCESS", progress=100)

    r = client.get(f"/api/tasks/{cid}/subtitle")
    assert r.status_code == 409
    assert r.json()["detail"] == "资源已删除"

    rec = client._store.get(cid)
    assert rec.resource_status == RESOURCE_STATUS_MISSING


def test_download_dubbed_video_returns_generated_file(client):
    cid = client.post("/api/tasks", json=_payload(ttsEnabled=True)).json()["id"]
    client._store.update(
        cid, status="SUCCESS", progress=100, tts_status="SUCCESS",
        output_dubbed_video="output_dubbed.mp4",
    )
    dubbed = client._tmp / cid / "output_dubbed.mp4"
    dubbed.parent.mkdir(parents=True, exist_ok=True)
    dubbed.write_bytes(b"DUBBED VIDEO")

    response = client.get(f"/api/tasks/{cid}/dubbed")
    assert response.status_code == 200
    assert response.content == b"DUBBED VIDEO"
    assert response.headers["content-type"] == "video/mp4"
    assert client.head(f"/api/tasks/{cid}/dubbed").status_code == 204


def test_download_dubbed_video_reports_unavailable_artifact(client):
    cid = client.post("/api/tasks", json=_payload(ttsEnabled=True)).json()["id"]
    client._store.update(
        cid, status="SUCCESS", progress=100, tts_status="SUCCESS",
        output_dubbed_video="output_dubbed.mp4",
    )

    response = client.get(f"/api/tasks/{cid}/dubbed")
    assert response.status_code == 409
    assert response.json()["detail"] == "配音视频不可用"
    assert client.head(f"/api/tasks/{cid}/dubbed").status_code == 409


def test_download_keeps_not_generated_message_for_running_task(client):
    """非 SUCCESS 任务（流水线还在跑）下载应仍返回'尚未生成'，不动 resource_status。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="TRANSLATING", progress=50, current_step="TRANSLATING")

    r = client.get(f"/api/tasks/{cid}/download")
    assert r.status_code == 409
    assert r.json()["detail"] == "成品视频尚未生成"

    rec = client._store.get(cid)
    # 运行中任务不应当被降级
    assert rec.resource_status == RESOURCE_STATUS_AVAILABLE
    assert rec.status == "TRANSLATING"


def test_list_tasks_reflects_resource_missing(client):
    """列表接口也要反映 resourceStatus，MISSING 任务的 outputs 为 None。"""
    a = client.post("/api/tasks", json=_payload()).json()["id"]
    b = client.post("/api/tasks", json=_payload(url="https://x/2")).json()["id"]

    # a 完整，b 缺文件
    d = client._tmp / a
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.mp4").write_bytes(b"VIDEO")
    (d / "translated.srt").write_text("x", encoding="utf-8")
    client._store.update(a, status="SUCCESS", progress=100)
    client._store.update(b, status="SUCCESS", progress=100)

    marked = tasks_routes.scan_missing_terminal(client._store, data_dir=client._tmp)
    assert sorted(marked) == [b]

    listed = {item["id"]: item for item in client.get("/api/tasks").json()}
    assert listed[a]["resourceStatus"] == RESOURCE_STATUS_AVAILABLE
    assert listed[a]["outputs"]["video"] == f"/api/tasks/{a}/download"
    assert listed[b]["resourceStatus"] == RESOURCE_STATUS_MISSING
    assert listed[b]["outputs"] is None


# ---------- CORS ----------

def test_cors_allows_local_workbench_origin(client):
    r = client.options(
        "/api/tasks",
        headers={
            "Origin": "http://localhost:5273",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5273"


def test_cors_rejects_untrusted_origin(client):
    r = client.options(
        "/api/tasks",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


# ---------- /api/tasks/probe 探针端点 ----------

def test_probe_returns_probe_result_shape(client, monkeypatch):
    """POST /api/tasks/probe 应把 probe_video 的 ProbeResult 翻成 TaskProbeOut，并落记录。"""
    fake = ProbeResult(
        ok=True,
        title="P",
        extractor="Generic",
        duration=9.0,
        formats_count=2,
        webpage_url="https://x/v",
        available_qualities=["1080p", "720p"],
        formats=[{"formatId": "f1", "resolution": "1080p"}],
        thumbnail="https://x/thumb.jpg",
        uploader="Test Creator",
    )
    monkeypatch.setattr(tasks_routes, "probe_video", lambda url, **kw: fake)

    r = client.post("/api/tasks/probe", json={"url": "https://x/v"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["title"] == "P"
    assert data["extractor"] == "Generic"
    assert data["duration"] == 9.0
    assert data["formatsCount"] == 2
    assert data["webpageUrl"] == "https://x/v"
    assert data["availableQualities"] == ["1080p", "720p"]
    assert len(data["formats"]) == 1
    assert data["thumbnail"] == "https://x/thumb.jpg"
    assert data["uploader"] == "Test Creator"
    assert data["reason"] is None and data["detail"] is None
    assert data["cached"] is False
    # 落库：调用一次后历史记录里应能查到此条
    records = client._probe_store.list()
    assert len(records) == 1
    assert records[0].url == "https://x/v"
    assert bool(records[0].ok) is True
    assert records[0].title == "P"
    assert records[0].formats_count == 2
    assert records[0].available_qualities == ["1080p", "720p"]
    assert records[0].thumbnail == "https://x/thumb.jpg"
    assert records[0].uploader == "Test Creator"


def test_manual_probe_updates_shared_startup_status(client, monkeypatch):
    fake = ProbeResult(ok=True, title="Manual YouTube", formats_count=1)
    monkeypatch.setattr(tasks_routes, "probe_video", lambda url, **kw: fake)

    response = client.post(
        "/api/tasks/probe",
        json={"url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ"},
    )
    assert response.status_code == 200

    status = client.get("/api/tasks/probe/startup-status").json()
    youtube = next(site for site in status["sites"] if site["id"] == "youtube")
    assert youtube["status"] == "ok"
    assert youtube["source"] == "manual"
    assert youtube["result"]["title"] == "Manual YouTube"


def test_probe_forwards_whitelisted_browser_cookie_source(client, monkeypatch):
    import dataclasses

    captured = {}
    fake = ProbeResult(ok=True, title="P", formats_count=1)
    monkeypatch.setattr(
        tasks_routes,
        "settings",
        dataclasses.replace(tasks_routes.settings, _allow_cookies_from_browser=True),
    )

    def fake_probe(url, **kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(tasks_routes, "probe_video", fake_probe)
    r = client.post(
        "/api/tasks/probe",
        json={"url": "https://x/v", "cookiesFromBrowser": "chrome"},
    )
    assert r.status_code == 200
    assert captured == {"cookies_from_browser": "chrome"}
    assert "cookiesFromBrowser" not in r.json()
    assert "profile" not in r.text.lower()


def test_probe_rejects_invalid_browser_cookie_source(client):
    r = client.post(
        "/api/tasks/probe",
        json={"url": "https://x/v", "cookiesFromBrowser": "/tmp/profile"},
    )
    assert r.status_code == 422


def test_probe_rejects_browser_cookies_when_disabled(client, monkeypatch):
    import dataclasses

    monkeypatch.setattr(
        tasks_routes,
        "settings",
        dataclasses.replace(tasks_routes.settings, _allow_cookies_from_browser=False),
    )
    r = client.post(
        "/api/tasks/probe",
        json={"url": "https://x/v", "cookiesFromBrowser": "chrome"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "BROWSER_COOKIES_DISABLED"
    assert "profile" not in r.text.lower()


def test_create_task_with_custom_quality(client):
    """POST /api/tasks 支持指定 quality 参数。"""
    payload = _payload(quality="1080p")
    r = client.post("/api/tasks", json=payload)
    assert r.status_code == 201
    data = r.json()
    assert data["quality"] == "1080p"
    rec = client._store.get(data["id"])
    assert rec.quality == "1080p"


def test_probe_failure_response(client, monkeypatch):
    """失败时透传 reason / detail，并落一条 ok=False 的历史。"""
    fake = ProbeResult(ok=False, reason="yt-dlp 暂不支持这个网站或链接", detail="Unsupported URL")
    monkeypatch.setattr(tasks_routes, "probe_video", lambda url, **kw: fake)

    r = client.post("/api/tasks/probe", json={"url": "ftp://x/"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert data["reason"] == "yt-dlp 暂不支持这个网站或链接"
    assert data["detail"] == "Unsupported URL"
    assert data["formatsCount"] == 0
    # 失败也要落库：方便用户回看错误信息
    records = client._probe_store.list()
    assert len(records) == 1
    assert bool(records[0].ok) is False
    assert records[0].reason == "yt-dlp 暂不支持这个网站或链接"
    assert records[0].detail == "Unsupported URL"


def test_probe_rejects_empty_url_422(client):
    """url 字段 min_length=1：空串应被 422 拒绝。"""
    assert client.post("/api/tasks/probe", json={"url": ""}).status_code == 422


def test_probe_rejects_missing_url_422(client):
    assert client.post("/api/tasks/probe", json={}).status_code == 422


# ---------- /api/tasks/probe/records 端点 ----------

def test_list_probe_records_default_limit(client, monkeypatch):
    """GET /api/tasks/probe/records 默认 limit=50，按时间倒序。"""
    fake = ProbeResult(ok=True, title="T", formats_count=1)
    monkeypatch.setattr(tasks_routes, "probe_video", lambda url, **kw: fake)
    for i in range(3):
        r = client.post("/api/tasks/probe", json={"url": f"https://x/{i}"})
        assert r.status_code == 200

    res = client.get("/api/tasks/probe/records")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 3
    # 倒序：最后写入的应排第一
    assert data[0]["url"] == "https://x/2"
    assert data[2]["url"] == "https://x/0"
    # 字段对齐前端契约
    assert set(data[0].keys()) == {
        "id", "url", "ok", "title", "extractor", "duration",
        "formatsCount", "webpageUrl", "reason", "detail", "createdAt",
        "language", "availableQualities", "formats", "thumbnail", "uploader",
    }


def test_list_probe_records_respects_limit(client, monkeypatch):
    fake = ProbeResult(ok=True, title="T", formats_count=1)
    monkeypatch.setattr(tasks_routes, "probe_video", lambda url, **kw: fake)
    for i in range(3):
        client.post("/api/tasks/probe", json={"url": f"https://x/{i}"})

    res = client.get("/api/tasks/probe/records", params={"limit": 2})
    assert res.status_code == 200
    assert len(res.json()) == 2


def test_list_probe_records_rejects_invalid_limit(client):
    """limit 越界（<1 或 >500）应被 422 拒绝。"""
    assert client.get("/api/tasks/probe/records", params={"limit": 0}).status_code == 422
    assert client.get("/api/tasks/probe/records", params={"limit": 501}).status_code == 422


def test_clear_probe_records(client, monkeypatch):
    fake = ProbeResult(ok=True, title="T", formats_count=1)
    monkeypatch.setattr(tasks_routes, "probe_video", lambda url, **kw: fake)
    for i in range(2):
        client.post("/api/tasks/probe", json={"url": f"https://x/{i}"})

    res = client.delete("/api/tasks/probe/records")
    assert res.status_code == 200
    assert res.json() == {"deleted": 2}
    # 清空后列表为空
    assert client.get("/api/tasks/probe/records").json() == []
    # 再次清空返回 0，不报错
    res2 = client.delete("/api/tasks/probe/records")
    assert res2.status_code == 200
    assert res2.json() == {"deleted": 0}


def test_delete_probe_record(client, monkeypatch):
    fake = ProbeResult(ok=True, title="T", formats_count=1)
    monkeypatch.setattr(tasks_routes, "probe_video", lambda url, **kw: fake)
    client.post("/api/tasks/probe", json={"url": "https://x/v"})

    rid = client._probe_store.list()[0].id
    res = client.delete(f"/api/tasks/probe/records/{rid}")
    assert res.status_code == 204
    assert client.get("/api/tasks/probe/records").json() == []


def test_delete_probe_record_404_when_missing(client):
    res = client.delete("/api/tasks/probe/records/probe_doesnotexist")
    assert res.status_code == 404


def test_get_probe_startup_status(client):
    res = client.get("/api/tasks/probe/startup-status")
    assert res.status_code == 200
    data = res.json()
    assert data["state"] in {"idle", "disabled", "running", "completed"}
    assert data["total"] == 10
    assert len(data["sites"]) == 10
    assert {site["id"] for site in data["sites"]} == {
        "youtube", "vimeo", "dailymotion", "twitch", "tiktok",
        "twitter", "instagram", "acfun", "niconico", "pornhub",
    }


def test_get_ytdlp_info(client):
    """GET /api/tasks/probe/ytdlp-info 应返回提取器数量、版本和配置参数。"""
    res = client.get("/api/tasks/probe/ytdlp-info")
    assert res.status_code == 200
    data = res.json()
    assert "version" in data
    assert "extractorsCount" in data
    assert data["extractorsCount"] > 0
    assert "proxyConfigured" in data
    assert "cacheTtlSec" in data
    assert isinstance(data["cacheTtlSec"], (int, float))
    assert isinstance(data["browserCookiesEnabled"], bool)


# ---------- /api/tasks/upload 上传端点边界 ----------

def _upload(client, **fields):
    """构造上传 multipart 请求；name 字段走默认值。"""
    defaults = {
        "sourceLang": "en", "targetLang": "ja", "mode": "mono",
        "burn": "hard", "model": "small", "engine": "deepseek",
        "needSubtitle": "true",
    }
    defaults.update(fields)
    return client.post(
        "/api/tasks/upload",
        data=defaults,
        files={"file": (fields["_filename"], fields.get("_content", b"VIDEO"), "video/mp4")},
    )


def test_upload_rejects_unsupported_extension_400(client, monkeypatch):
    """非视频扩展名应在创建记录前返回 400，且不入队、不落盘。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = _upload(client, _filename="clip.txt", _content=b"x", mode="mono")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "UNSUPPORTED_FORMAT"
    assert "不支持的视频格式" in detail["message"]
    assert ".mp4" in detail["limits"]["supportedFormats"]
    assert detail["suggestion"]
    assert enqueued == []


def test_upload_rejects_no_extension_400(client, monkeypatch):
    """无扩展名视为未知格式，返回 400。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = _upload(client, _filename="clip", _content=b"x")
    assert r.status_code == 400
    assert "未知" in r.json()["detail"]["message"]
    assert enqueued == []


def test_upload_normalizes_uppercase_extension(client, monkeypatch):
    """扩展名大小写不敏感：.MP4 与 .Mp4 都应被接受。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = _upload(client, _filename="A.MP4", _content=b"VIDEO")
    assert r.status_code == 201
    data = r.json()
    rec = client._store.get(data["id"])
    assert (client._tmp / data["id"] / f"source.mp4").exists()
    assert rec.title == "A"


def _count_task_dirs(base: Path) -> int:
    """统计 base 下形如 task_* 的目录数量，用于验证清理是否彻底。"""
    return sum(1 for p in base.iterdir() if p.is_dir() and p.name.startswith("task_"))


def test_upload_empty_file_400_and_cleanup(client, monkeypatch):
    """0 字节文件应被拒为 400，且任务记录与产物目录应被清理。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)
    released = []
    monkeypatch.setattr(tasks_routes, "release_lock", released.append)

    before = _count_task_dirs(client._tmp)
    r = _upload(client, _filename="clip.mp4", _content=b"")
    assert r.status_code == 400
    assert "空" in r.json()["detail"]

    # 不能留任何孤儿任务或任务目录
    assert client.get("/api/tasks").json() == []
    assert _count_task_dirs(client._tmp) == before
    assert enqueued == []
    assert released == []


def test_upload_save_failure_500_and_cleanup(client, monkeypatch):
    """落盘失败时记为 500，任务记录与任务目录应被清掉，避免孤儿。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)
    released = []
    monkeypatch.setattr(tasks_routes, "release_lock", released.append)

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.open", boom)

    before = _count_task_dirs(client._tmp)
    r = _upload(client, _filename="clip.mp4", _content=b"VIDEO")
    assert r.status_code == 500
    assert "保存上传文件失败" in r.json()["detail"]

    assert client.get("/api/tasks").json() == []
    assert _count_task_dirs(client._tmp) == before
    assert enqueued == []
    assert released == []


def test_upload_uses_filename_stem_as_title(client, monkeypatch):
    """title 取自文件名 stem（去后缀）。"""
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", lambda tid: None)

    r = _upload(client, _filename="my-clip.mp4", _content=b"VIDEO")
    assert r.status_code == 201
    rec = client._store.get(r.json()["id"])
    assert rec.title == "my-clip"


def test_upload_title_uses_stem_for_dotted_name(client, monkeypatch):
    """以点开头的文件名（Path 中 stem 非空但带点）也应按 stem 入库。"""
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", lambda tid: None)

    # '..mp4' -> Path('..mp4').suffix == '.mp4'、stem == '.' （不会被 "" 兜底）
    r = _upload(client, _filename="..mp4", _content=b"VIDEO")
    assert r.status_code == 201
    rec = client._store.get(r.json()["id"])
    assert rec.title == "."


def test_upload_persists_need_subtitle_false(client, monkeypatch):
    """needSubtitle=false 应入库为 0，并能在响应里读到。"""
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", lambda tid: None)

    r = _upload(client, _filename="clip.mp4", _content=b"VIDEO", needSubtitle="false")
    assert r.status_code == 201
    data = r.json()
    assert data["sourceType"] == "upload"
    assert data["needSubtitle"] is False

    rec = client._store.get(data["id"])
    assert rec.source_type == "upload"
    assert rec.need_subtitle == 0


def test_upload_invalid_enum_param_422(client, monkeypatch):
    """上传端点也对 mode/burn/engine 枚举做校验，非法值应 422。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    for bad in [{"mode": "mixed"}, {"burn": "weird"}, {"engine": "other"}]:
        r = _upload(client, _filename="clip.mp4", _content=b"VIDEO", **bad)
        assert r.status_code == 422, f"应被 422 拒绝: {bad}"
    assert enqueued == []


def test_upload_calls_enqueue_with_task_id(client, monkeypatch):
    """上传成功时 enqueue_pipeline 收到新建的 task_id。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = _upload(client, _filename="clip.mp4", _content=b"VIDEO")
    data = r.json()
    assert r.status_code == 201
    assert enqueued == [data["id"]]


def test_upload_allows_file_when_multipart_content_length_exceeds_max(client, monkeypatch):
    """multipart 总长度超过上限时，只要实际视频未超限仍应允许上传。"""
    import dataclasses

    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)
    s = dataclasses.replace(tasks_routes.settings, max_upload_mb=1)
    monkeypatch.setattr(tasks_routes, "settings", s)

    # 模拟 multipart 封装开销使请求总长度超过 1MB，但文件实际只有 4 字节。
    r = client.post(
        "/api/tasks/upload",
        headers={"Content-Length": str(2 * 1024 * 1024)},
        data={
            "sourceLang": "en", "targetLang": "ja", "mode": "mono",
            "burn": "hard", "model": "small", "engine": "deepseek",
            "needSubtitle": "true",
        },
        files={"file": ("clip.mp4", b"VIDEO", "video/mp4")},
    )
    assert r.status_code == 201
    assert enqueued == [r.json()["id"]]


def test_upload_streaming_bytes_exceeds_max_413_and_cleanup(client, monkeypatch):
    """流式写入过程中累计大小超过 max_upload_mb 触发 413 并清理落盘文件及 DB 记录。"""
    import dataclasses
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)
    s = dataclasses.replace(tasks_routes.settings, max_upload_mb=1)
    monkeypatch.setattr(tasks_routes, "settings", s)

    # 构造超过 1MB (1024 * 1024) 的内容 (1.5MB)
    large_content = b"X" * (1500 * 1024)
    before = _count_task_dirs(client._tmp)

    # 不发 Content-Length，以强制走流式 copy 字节统计逻辑
    r = client.post(
        "/api/tasks/upload",
        data={
            "sourceLang": "en", "targetLang": "ja", "mode": "mono",
            "burn": "hard", "model": "small", "engine": "deepseek",
            "needSubtitle": "true",
        },
        files={"file": ("clip.mp4", large_content, "video/mp4")},
    )
    assert r.status_code == 413
    detail = r.json()["detail"]
    assert detail["code"] == "UPLOAD_TOO_LARGE"
    assert "超过最大限制" in detail["message"]
    assert enqueued == []
    assert client.get("/api/tasks").json() == []
    assert _count_task_dirs(client._tmp) == before


def test_upload_creates_atomic_source_file_and_no_part_remaining(client, monkeypatch):
    """上传成功后，正式文件 source.mp4 存在，且不残留 .part 文件。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)

    r = _upload(client, _filename="clip.mp4", _content=b"ATOMIC_VIDEO")
    assert r.status_code == 201
    tid = r.json()["id"]
    tdir = client._tmp / tid

    assert (tdir / "source.mp4").exists()
    assert (tdir / "source.mp4").read_bytes() == b"ATOMIC_VIDEO"
    assert not (tdir / "source.mp4.part").exists()


def test_upload_probe_duration_none_400_and_cleanup(client, monkeypatch):
    """probe_duration 返回 None 时（如 ffprobe 缺失或解析失败）返回 400 并清理记录与目录。"""
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)
    monkeypatch.setattr(tasks_routes, "probe_duration", lambda path, bin_path: None)

    before = _count_task_dirs(client._tmp)
    r = _upload(client, _filename="corrupted.mp4", _content=b"CORRUPTED_VIDEO_HEADER")
    assert r.status_code == 400
    assert "无法解析视频时长" in r.json()["detail"]
    assert enqueued == []
    assert client.get("/api/tasks").json() == []
    assert _count_task_dirs(client._tmp) == before


def test_upload_duration_exceeds_max_400_and_cleanup(client, monkeypatch):
    """视频时长超过 max_video_minutes 时返回 400 并清理已建记录与目录。"""
    import dataclasses
    enqueued = []
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", enqueued.append)
    s = dataclasses.replace(tasks_routes.settings, max_video_minutes=10)
    monkeypatch.setattr(tasks_routes, "settings", s)
    # mock probe_duration 返回 601 秒 (10.01 分钟 > 10 分钟)
    monkeypatch.setattr(tasks_routes, "probe_duration", lambda path, bin_path: 601.0)

    before = _count_task_dirs(client._tmp)
    r = _upload(client, _filename="clip.mp4", _content=b"VIDEO")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "UPLOAD_DURATION_EXCEEDED"
    assert "视频时长" in detail["message"]
    assert "超过最大限制" in detail["message"]
    assert detail["limits"] == {"maxMinutes": 10, "durationMinutes": 10.0}
    assert detail["suggestion"]
    assert enqueued == []
    assert client.get("/api/tasks").json() == []
    assert _count_task_dirs(client._tmp) == before


# ---------- to_out() 链路新增字段透出 ----------

def test_to_out_exposes_source_type_and_need_subtitle():
    """TaskOut 应携带 sourceType / needSubtitle，与前端契约对齐。"""
    rec = TaskRecord(
        id="task_abc", url="u", source_lang="en", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        source_type="upload", need_subtitle=0,
    )
    out = to_out(rec)
    assert out.sourceType == "upload"
    assert out.needSubtitle is False


def test_to_out_need_subtitle_field_defaults_to_true():
    """need_subtitle 默认 1（True）应翻为 needSubtitle=true。"""
    rec = TaskRecord(
        id="t", url="u", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
    )
    out = to_out(rec)
    assert out.sourceType == "url"
    assert out.needSubtitle is True


def test_to_out_success_with_subtitle_when_need_subtitle_true():
    """SUCCESS + need_subtitle=True：outputs 应同时含 video / subtitle。"""
    rec = TaskRecord(
        id="task_x", url="u", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        status="SUCCESS", need_subtitle=1,
    )
    out = to_out(rec)
    assert out.outputs == {
        "video": "/api/tasks/task_x/download",
        "subtitle": "/api/tasks/task_x/subtitle",
    }


def test_to_out_success_without_subtitle_when_need_subtitle_false():
    """SUCCESS + need_subtitle=False：outputs 只含 video（仅下载场景无字幕）。"""
    rec = TaskRecord(
        id="task_y", url="u", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        status="SUCCESS", need_subtitle=0,
    )
    out = to_out(rec)
    assert out.outputs == {"video": "/api/tasks/task_y/download"}
    assert "subtitle" not in out.outputs


def test_to_out_non_success_no_outputs():
    """非 SUCCESS 状态不应有 outputs，避免把未烧录路径暴露给前端。"""
    rec = TaskRecord(
        id="task_z", url="u", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        status="BURNING", progress=90, current_step="BURNING",
        need_subtitle=1,
    )
    out = to_out(rec)
    assert out.outputs is None


# ---------- _resolve_video 下载回退 ----------

def test_resolve_video_prefers_output_mp4(client, monkeypatch, tmp_path):
    """output.mp4 存在时优先返回它（烧录成品）。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mp4").write_bytes(b"SRC")
    (d / "output.mp4").write_bytes(b"OUT")

    assert tasks_routes._resolve_video(cid) == d / "output.mp4"


def test_resolve_video_falls_back_to_source_when_no_output(client, tmp_path):
    """output.mp4 缺失但 source.* 存在 -> 回退到 source.*。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mp4").write_bytes(b"SRC")

    assert tasks_routes._resolve_video(cid) == d / "source.mp4"


def test_resolve_video_supports_other_source_extension(client, tmp_path):
    """source.* 兼容任意扩展名（mkv / mov / webm 等）。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mkv").write_bytes(b"SRC")

    assert tasks_routes._resolve_video(cid) == d / "source.mkv"


def test_resolve_video_returns_none_when_missing(client, tmp_path):
    """目录或文件都不存在时返回 None。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    assert tasks_routes._resolve_video(cid) is None


def test_resolve_video_ignores_part_file(client, tmp_path):
    """yt-dlp 下载中的 source.<ext>.part 临时文件必须被过滤，不能作为成品/源视频返回。"""
    cid = client.post("/api/tasks", json=_payload(needSubtitle=False)).json()["id"]
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mp4.part").write_bytes(b"PARTIAL_BYTES")

    assert tasks_routes._resolve_video(cid) is None


def test_download_video_returns_409_during_downloading_with_part_file(client, tmp_path):
    """仅下载模式在 DOWNLOADING 期间存在 .part 临时文件时，调 /download 必须返回 409 拒绝。"""
    cid = client.post("/api/tasks", json=_payload(needSubtitle=False)).json()["id"]
    client._store.update(cid, status="DOWNLOADING", progress=30)
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mp4.part").write_bytes(b"PARTIAL_BYTES")

    r = client.get(f"/api/tasks/{cid}/download")
    assert r.status_code == 409
    assert r.json()["detail"] == "成品视频尚未生成"


def test_download_409_when_only_source_no_output(client, tmp_path):
    """仅下载模式（无 output.mp4）下走 /download 不应 409：可回退到 source.*。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.mp4").write_bytes(b"SRC")

    r = client.get(f"/api/tasks/{cid}/download")
    assert r.status_code == 200
    assert r.content == b"SRC"


def test_download_subtitle_409_when_need_subtitle_false(client, tmp_path):
    """need_subtitle=False 的任务不应能下载字幕（链路不会生成）。"""
    cid = client.post("/api/tasks", json=_payload(needSubtitle=False)).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100, need_subtitle=0)
    # 即便前端误调，物理文件也不应被读到
    assert client.get(f"/api/tasks/{cid}/subtitle").status_code == 409


def test_success_task_outputs_skip_subtitle_when_need_subtitle_false(client):
    """SUCCESS 且 needSubtitle=False 时 GET 任务的 outputs 不应含 subtitle 键。"""
    cid = client.post("/api/tasks", json=_payload(needSubtitle=False)).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100, need_subtitle=0)
    data = client.get(f"/api/tasks/{cid}").json()
    assert data["outputs"] == {"video": f"/api/tasks/{cid}/download"}
    assert "subtitle" not in data["outputs"]


# ---------- POST /api/tasks/{id}/folder 端点安全测试 ----------

def test_open_task_folder_succeeds(client, monkeypatch):
    """合法 task_id 且目录存在时，调用 _open_folder 成功打开。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    d = client._tmp / cid
    d.mkdir(parents=True, exist_ok=True)

    opened = []
    monkeypatch.setattr(tasks_routes.subprocess, "Popen", lambda cmd, **kw: opened.append(cmd))

    r = client.post(f"/api/tasks/{cid}/folder")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert len(opened) == 1
    assert str(d.resolve()) in opened[0]


def test_open_task_folder_invalid_task_id_400(client):
    """非 task_ 前缀或含非法字符的 task_id 应返回 400。"""
    invalid_ids = [
        "invalid_id",
        "task_123\\sub",
        "task_123;rm",
        "task_123&calc",
        "task_123|calc",
    ]
    for bad_id in invalid_ids:
        r = client.post(f"/api/tasks/{bad_id}/folder")
        assert r.status_code == 400
        assert "task_id 格式不符合规范" in r.json()["detail"]


def test_open_task_folder_not_found_404(client):
    """不存在的任务返回 404。"""
    r = client.post("/api/tasks/task_12345678/folder")
    assert r.status_code == 404


def test_open_task_folder_dir_not_generated_409(client):
    """任务存在但磁盘目录尚未生成时返回 409。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    # 保证目录不存在
    d = client._tmp / cid
    if d.exists():
        d.rmdir()

    r = client.post(f"/api/tasks/{cid}/folder")
    assert r.status_code == 409
    assert "任务目录尚未生成" in r.json()["detail"]


def test_open_task_folder_path_traversal_400(client, monkeypatch):
    """即便通过 DB 校验，若 task_dir 解出的路径在 data_dir 之外，应返回 400 拒绝。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    outside_dir = client._tmp.parent / "outside"
    outside_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(tasks_routes, "task_dir", lambda tid: outside_dir)

    r = client.post(f"/api/tasks/{cid}/folder")
    assert r.status_code == 400
    assert "任务目录路径非法" in r.json()["detail"]


# ---------- SSE 进度流端点与事件 ----------

def test_sse_stream_terminal_emits_end_event(client):
    """终态任务进 SSE 流应立即推送 event: end 并结束。"""
    cid = client.post("/api/tasks", json=_payload()).json()["id"]
    client._store.update(cid, status="SUCCESS", progress=100)

    with client.stream("GET", f"/api/tasks/{cid}/stream") as res:
        lines = [line for line in res.iter_lines() if line]

    assert res.status_code == 200
    assert any(line == "event: end" for line in lines)
    assert any("data: {" in line for line in lines)


def test_sse_stream_timeout_emits_timeout_event(client, monkeypatch):
    """到达 SUBTRANS_STREAM_TIMEOUT_SEC 超时时间后应推送 event: timeout 并断开。"""
    import dataclasses
    from src.config import settings
    cid = client.post("/api/tasks", json=_payload()).json()["id"]

    # 替换超时配置为 0 秒（立即触发 timeout）
    monkeypatch.setattr(tasks_routes, "settings", dataclasses.replace(settings, stream_timeout_sec=0))

    with client.stream("GET", f"/api/tasks/{cid}/stream") as res:
        lines = [line for line in res.iter_lines() if line]

    assert res.status_code == 200
    assert "event: timeout" in lines
    assert 'data: {"error":"stream timeout"}' in lines


def test_sse_stream_keepalive(client, monkeypatch):
    """连续无状态变化时，应推送心跳保活行 :keepalive。"""
    import time
    cid = client.post("/api/tasks", json=_payload()).json()["id"]

    # 模拟 time.time，让第二次循环判定 15 秒已过，触发 keepalive
    times = [100.0, 100.0, 100.0, 120.0, 120.0]
    monkeypatch.setattr(time, "time", lambda: times.pop(0) if times else 120.0)

    sleep_count = 0
    def fake_sleep(sec):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 2:
            client._store.update(cid, status="SUCCESS", progress=100)

    monkeypatch.setattr(time, "sleep", fake_sleep)

    with client.stream("GET", f"/api/tasks/{cid}/stream") as res:
        lines = [line for line in res.iter_lines() if line]

    assert res.status_code == 200
    assert ":keepalive" in lines


def test_create_task_duplicate_url_returns_existing_task_id(client):
    first = client.post("/api/tasks", json=_payload())
    duplicate = client.post("/api/tasks", json=_payload())
    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "TASK_ALREADY_RUNNING"
    assert duplicate.json()["detail"]["taskId"] == first.json()["id"]
