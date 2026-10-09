"""可配置翻译引擎的存储、协议适配与 SSRF 安全测试。"""

from __future__ import annotations

import pytest
from src.core.translation_engines import EngineClient, TranslationEngineError, validate_base_url
from src.store.translation_engine_store import TranslationEngineStore


def test_translation_engine_store_seeds_deepseek_once(tmp_path):
    db = tmp_path / "engines.db"
    store = TranslationEngineStore(db)

    seeded = store.ensure_default_deepseek(
        api_key="env-key",
        base_url="https://api.deepseek.com/",
        model="deepseek-chat",
    )
    assert seeded.id == "deepseek"
    assert seeded.name == "DeepSeek"
    assert seeded.api_type == "openai_compatible"
    assert seeded.base_url == "https://api.deepseek.com"
    assert seeded.model == "deepseek-chat"
    assert seeded.has_api_key is True
    assert seeded.availability == "UNKNOWN"

    # 启动流程可重复执行，且不覆盖用户后续修改。
    store.update("deepseek", name="我的 DeepSeek")
    again = store.ensure_default_deepseek(api_key="another-key")
    assert again.id == "deepseek"
    assert again.name == "我的 DeepSeek"
    assert again.api_key == "env-key"


def test_api_key_rotation_audit_and_blank_update_is_noop(tmp_path):
    store = TranslationEngineStore(tmp_path / "engines.db")
    created = store.create(
        name="Audited", api_type="openai_compatible", base_url="https://api.openai.com",
        model="m", api_key="first-key",
    )
    assert created.api_key_rotated_at == created.created_at
    original_updated = created.updated_at
    original_rotated = created.api_key_rotated_at

    # A blank key in an update is a placeholder for the existing secret.
    unchanged = store.update(created.id, api_key="   ")
    assert unchanged.api_key == "first-key"
    assert unchanged.updated_at == original_updated
    assert unchanged.api_key_rotated_at == original_rotated

    repeated = store.update(created.id, api_key="first-key")
    assert repeated.updated_at == original_updated
    assert repeated.api_key_rotated_at == original_rotated

    replaced = store.update(created.id, api_key="second-key")
    assert replaced.api_key == "second-key"
    assert replaced.api_key_rotated_at is not None
    assert replaced.api_key_rotated_at >= original_rotated
    assert replaced.updated_at >= original_updated


def test_translation_engine_schema_migrates_rotation_timestamp(tmp_path):
    import sqlite3

    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE translation_engines (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, api_type TEXT NOT NULL,
                base_url TEXT NOT NULL, model TEXT NOT NULL, api_key TEXT,
                enabled INTEGER NOT NULL DEFAULT 1, availability TEXT NOT NULL,
                last_checked_at INTEGER, last_error TEXT, created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO translation_engines VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy", "Legacy", "openai_compatible", "https://api.openai.com", "m", "k",
             1, "UNKNOWN", None, None, 1, 1),
        )
    loaded = TranslationEngineStore(db).get("legacy")
    assert loaded is not None
    assert loaded.api_key == "k"
    assert loaded.api_key_rotated_at is None


def test_delete_clears_secret_before_removing_engine(tmp_path):
    store = TranslationEngineStore(tmp_path / "engines.db")
    created = store.create(
        name="Disposable", api_type="openai_compatible", base_url="https://api.openai.com",
        model="m", api_key="secret",
    )
    assert store.delete(created.id) is True
    assert store.get(created.id) is None


def test_translation_engine_store_persists_without_exposing_model(tmp_path):
    db = tmp_path / "engines.db"
    store = TranslationEngineStore(db)
    created = store.create(
        name="OpenAI 网关", api_type="openai_compatible",
        base_url="https://api.openai.com/v1/", model="qwen-plus", api_key="secret",
    )
    loaded = TranslationEngineStore(db).get(created.id)
    assert loaded is not None
    assert loaded.name == "OpenAI 网关"
    assert loaded.base_url == "https://api.openai.com/v1"
    assert loaded.has_api_key is True


def test_openai_compatible_payload(monkeypatch):
    seen = {}

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "OK"}}]}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return Response()

    monkeypatch.setattr("httpx.post", fake_post)
    result = EngineClient("openai_compatible", "https://example.com/v1", "demo", "key").complete("sys", "user")
    assert result == "OK"
    assert seen["url"].endswith("/v1/chat/completions")
    assert seen["headers"]["Authorization"] == "Bearer key"
    assert seen["json"]["model"] == "demo"


def test_anthropic_compatible_payload(monkeypatch):
    seen = {}

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"content": [{"type": "text", "text": "OK"}]}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return Response()

    monkeypatch.setattr("httpx.post", fake_post)
    result = EngineClient("anthropic_compatible", "https://example.com", "claude-demo", "key").complete("sys", "user")
    assert result == "OK"
    assert seen["url"].endswith("/v1/messages")
    assert seen["headers"]["x-api-key"] == "key"
    assert seen["json"]["system"] == "sys"


def test_make_engine_client_custom_timeout():
    from src.core.translation_engines import make_engine_client
    config = {
        "api_type": "openai_compatible",
        "base_url": "https://example.com",
        "model": "demo",
        "api_key": "key",
    }
    client = make_engine_client(config, timeout=10)
    assert client.timeout == 10

    default_client = make_engine_client(config)
    assert default_client.timeout == 60


def test_reset_dangling_checking(tmp_path):
    db = tmp_path / "engines.db"
    store = TranslationEngineStore(db)

    # 1. 配置有 API key，被置为 CHECKING (如进程崩溃残留)
    c1 = store.create(
        name="Engine 1", api_type="openai_compatible",
        base_url="https://api.openai.com", model="m1", api_key="k1",
    )
    store.update(c1.id, availability="CHECKING")

    # 2. 未配置 API key，被置为 CHECKING
    c2 = store.create(
        name="Engine 2", api_type="openai_compatible",
        base_url="https://api.deepseek.com", model="m2", api_key=None,
    )
    # 强制设置空 key 和 CHECKING
    with store._connect() as conn:
        conn.execute("UPDATE translation_engines SET availability = 'CHECKING', api_key = '' WHERE id = ?", (c2.id,))

    # 重置悬空 CHECKING
    reset_count = store.reset_dangling_checking()
    assert reset_count == 2

    e1 = store.get(c1.id)
    assert e1.availability == "UNKNOWN"

    e2 = store.get(c2.id)
    assert e2.availability == "UNCONFIGURED"


def test_validate_engine_uses_shorter_timeout(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.handler.app import app
    from src.handler.deps import get_translation_engine_store, reset_singletons
    from src.handler.translation_engines import VALIDATE_TIMEOUT_SEC

    assert VALIDATE_TIMEOUT_SEC == 10

    db = tmp_path / "engines.db"
    store = TranslationEngineStore(db)
    rec = store.create(
        name="Gateway", api_type="openai_compatible",
        base_url="https://example.com", model="m1", api_key="sk-test",
    )

    reset_singletons()
    app.dependency_overrides[get_translation_engine_store] = lambda: store

    seen_timeout = []

    def fake_make_engine_client(config, timeout=None):
        seen_timeout.append(timeout)
        class FakeClient:
            def complete(self, sys, user, max_tokens=4):
                return "OK"
        return FakeClient()

    monkeypatch.setattr("src.handler.translation_engines.make_engine_client", fake_make_engine_client)

    with TestClient(app) as client:
        r = client.post(f"/api/settings/translation-engines/{rec.id}/validate")
        assert r.status_code == 200
        data = r.json()
        assert data["available"] is True
        assert data["availability"] == "AVAILABLE"
        assert seen_timeout == [10]

    updated = store.get(rec.id)
    assert updated.availability == "AVAILABLE"

    app.dependency_overrides.clear()
    reset_singletons()


def test_whitespace_api_key_store_handling(tmp_path):
    db = tmp_path / "engines.db"
    store = TranslationEngineStore(db)

    # 1. 传入空白串创建配置 -> 规范化为 None，UNCONFIGURED
    e1 = store.create(
        name="Engine WhiteSpace", api_type="openai_compatible",
        base_url="https://api.openai.com", model="m1", api_key="   ",
    )
    assert e1.api_key is None
    assert e1.has_api_key is False
    assert e1.availability == "UNCONFIGURED"

    # 2. 传入首尾含空格的 key 创建配置 -> 自动 strip
    e2 = store.create(
        name="Engine Padded", api_type="openai_compatible",
        base_url="https://api.deepseek.com", model="m2", api_key="  sk-padded-key  ",
    )
    assert e2.api_key == "sk-padded-key"
    assert e2.has_api_key is True
    assert e2.availability == "UNKNOWN"

    # 3. 更新已有的有效 key 为空白串 -> 保留旧 Key
    updated_e2 = store.update(e2.id, api_key="   ")
    assert updated_e2.api_key == "sk-padded-key"

    # 4. 更新无 key 的配置为空白串 -> 依然为 None，UNCONFIGURED
    updated_e1 = store.update(e1.id, api_key="   ")
    assert updated_e1.api_key is None
    assert updated_e1.availability == "UNCONFIGURED"

    # 5. reset_dangling_checking 将带有空白串的 CHECKING 状态恢复为 UNCONFIGURED
    with store._connect() as conn:
        conn.execute("UPDATE translation_engines SET availability = 'CHECKING', api_key = '   ' WHERE id = ?", (e1.id,))
    assert store.reset_dangling_checking() == 1
    assert store.get(e1.id).availability == "UNCONFIGURED"


def test_whitespace_api_key_make_engine_client():
    from src.core.translation_engines import TranslationEngineError, make_engine_client

    # 空白串报错 missing_api_key
    with pytest.raises(TranslationEngineError) as exc_info:
        make_engine_client({
            "api_type": "openai_compatible",
            "base_url": "https://example.com",
            "model": "m",
            "api_key": "   ",
        })
    assert exc_info.value.code == "missing_api_key"

    # 首尾带空格的 key 被 strip
    client = make_engine_client({
        "api_type": "openai_compatible",
        "base_url": "https://example.com",
        "model": "m",
        "api_key": "  sk-padded  ",
    })
    assert client.api_key == "sk-padded"


def test_whitespace_api_key_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.handler.app import app
    from src.handler.deps import get_store, get_translation_engine_store, reset_singletons
    from src.handler import tasks as tasks_routes
    from src.store.task_store import TaskStore

    db_path = tmp_path / "subtitles.db"
    store = TaskStore(db_path)
    engine_store = TranslationEngineStore(db_path)
    monkeypatch.setattr(tasks_routes, "_ensure_local_model_ready", lambda model: None)

    reset_singletons()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_translation_engine_store] = lambda: engine_store

    with TestClient(app) as client:
        # 1. 创建包含空白串 apiKey 的引擎
        r = client.post("/api/settings/translation-engines", json={
            "name": "Blank Key Engine",
            "apiType": "openai_compatible",
            "baseUrl": "https://example.com",
            "model": "m1",
            "apiKey": "   ",
        })
        assert r.status_code == 201
        data = r.json()
        engine_id = data["id"]
        assert data["hasApiKey"] is False
        assert data["availability"] == "UNCONFIGURED"

        # 2. 校验空白 key 引擎 -> 拦截并返回 UNCONFIGURED / missing_api_key
        r = client.post(f"/api/settings/translation-engines/{engine_id}/validate")
        assert r.status_code == 200
        vdata = r.json()
        assert vdata["available"] is False
        assert vdata["availability"] == "UNCONFIGURED"
        assert vdata["errorCode"] == "missing_api_key"

        # 3. 用该引擎建任务 -> 拦截并返回 422 提示未配置 API Key
        r = client.post("/api/tasks", json={
            "url": "https://example.com/video.mp4",
            "engine": engine_id,
            "needSubtitle": True,
        })
        assert r.status_code == 422
        assert "翻译引擎尚未配置 API Key" in r.json()["detail"]

    app.dependency_overrides.clear()
    reset_singletons()


def test_create_engine_automatically_validates(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.handler.app import app
    from src.handler.deps import get_translation_engine_store, reset_singletons

    store = TranslationEngineStore(tmp_path / "engines.db")
    reset_singletons()
    app.dependency_overrides[get_translation_engine_store] = lambda: store
    calls = []

    def fake_make_engine_client(config, timeout=None):
        calls.append(timeout)
        class FakeClient:
            def complete(self, system, user, max_tokens=4):
                return "OK"
        return FakeClient()

    monkeypatch.setattr("src.handler.translation_engines.make_engine_client", fake_make_engine_client)
    with TestClient(app) as client:
        response = client.post("/api/settings/translation-engines", json={
            "name": "Automatic Gateway", "apiType": "openai_compatible",
            "baseUrl": "https://example.com", "model": "m1", "apiKey": "sk-test",
        })

    assert response.status_code == 201
    checked = store.get(response.json()["id"])
    assert checked.availability == "AVAILABLE"
    assert checked.last_checked_at is not None
    assert checked.last_error is None
    assert calls == [10]
    app.dependency_overrides.clear()
    reset_singletons()


def test_create_engine_records_automatic_validation_failure(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.handler.app import app
    from src.handler.deps import get_translation_engine_store, reset_singletons

    store = TranslationEngineStore(tmp_path / "engines.db")
    reset_singletons()
    app.dependency_overrides[get_translation_engine_store] = lambda: store

    def fake_make_engine_client(config, timeout=None):
        raise TranslationEngineError("bad credentials", code="unauthorized")

    monkeypatch.setattr("src.handler.translation_engines.make_engine_client", fake_make_engine_client)
    with TestClient(app) as client:
        response = client.post("/api/settings/translation-engines", json={
            "name": "Invalid Gateway", "apiType": "openai_compatible",
            "baseUrl": "https://example.com", "model": "m1", "apiKey": "sk-test",
        })

    assert response.status_code == 201
    checked = store.get(response.json()["id"])
    assert checked.availability == "UNAVAILABLE"
    assert checked.last_error == "bad credentials"
    app.dependency_overrides.clear()
    reset_singletons()


# ---------- SSRF & API KEY LEAKAGE TESTS ----------

def test_validate_base_url_ssrf_and_protocols(monkeypatch):
    """测试 validate_base_url 对回环/私网/链路本地/云元数据/非法协议/非法主机的拦截。"""
    # 有效公共 URL
    assert validate_base_url("https://api.deepseek.com/") == "https://api.deepseek.com"
    assert validate_base_url("https://example.com/v1") == "https://example.com/v1"

    # 非法协议
    for bad_scheme in ["ftp://example.com", "file:///etc/passwd", "gopher://example.com", "httpx://example.com"]:
        with pytest.raises(TranslationEngineError) as exc_info:
            validate_base_url(bad_scheme)
        assert exc_info.value.code == "invalid_base_url"

    # 空或无效 host
    for bad_url in ["", "  ", "http://", "https://:8080"]:
        with pytest.raises(TranslationEngineError) as exc_info:
            validate_base_url(bad_url)
        assert exc_info.value.code == "invalid_base_url"

    # 私网 / 回环 / 链路本地 / 云元数据 IP 拦截
    ssrf_ips = [
        "http://127.0.0.1:8000",
        "http://127.0.0.2:8000",
        "http://10.0.0.1",
        "http://172.16.0.1",
        "http://192.168.1.1",
        "http://169.254.169.254/latest/meta-data",  # 云元数据地址
        "http://0.0.0.0",
        "http://[::1]:8000",
        "http://[fc00::1]",
        "http://[fe80::1]",
    ]
    for ssrf_url in ssrf_ips:
        with pytest.raises(TranslationEngineError) as exc_info:
            validate_base_url(ssrf_url)
        assert exc_info.value.code == "ssrf_blocked"

    # 内部域名拦截
    forbidden_hosts = [
        "http://localhost:8000",
        "http://service.local",
        "http://internal.lan",
        "http://dev.home.arpa",
    ]
    for fhost in forbidden_hosts:
        with pytest.raises(TranslationEngineError) as exc_info:
            validate_base_url(fhost)
        assert exc_info.value.code == "ssrf_blocked"

    # 域名解析到内网 IP 拦截
    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host == "ssrf-domain.test":
            return [(2, 1, 6, "", ("127.0.0.1", 0))]
        if host == "cloud-metadata.test":
            return [(2, 1, 6, "", ("169.254.169.254", 0))]
        import socket
        return socket.getaddrinfo("example.com", None)

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    with pytest.raises(TranslationEngineError) as exc_info:
        validate_base_url("https://ssrf-domain.test")
    assert exc_info.value.code == "ssrf_blocked"

    with pytest.raises(TranslationEngineError) as exc_info:
        validate_base_url("https://cloud-metadata.test")
    assert exc_info.value.code == "ssrf_blocked"


def test_update_engine_base_url_host_change_clears_api_key(tmp_path, monkeypatch):
    """测试 PUT 修改 baseUrl 主机/域名时，未提供新 apiKey 会自动清空原密钥，防止 API Key 泄露到新服务器。"""
    from fastapi.testclient import TestClient
    from src.handler.app import app
    from src.handler.deps import get_translation_engine_store, reset_singletons

    db = tmp_path / "engines.db"
    store = TranslationEngineStore(db)

    # 创建一个正常配置了 deepseek 和密钥的引擎
    rec = store.create(
        name="DeepSeek Engine",
        api_type="openai_compatible",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        api_key="sk-real-secret-key-12345",
    )
    assert rec.has_api_key is True

    reset_singletons()
    app.dependency_overrides[get_translation_engine_store] = lambda: store

    post_calls = []

    def fake_post(url, **kwargs):
        post_calls.append({"url": url, "headers": kwargs.get("headers")})
        class Response:
            def raise_for_status(self): pass
            def json(self): return {"choices": [{"message": {"content": "OK"}}]}
        return Response()

    monkeypatch.setattr("httpx.post", fake_post)

    with TestClient(app) as client:
        # 攻击场景：将 baseUrl 改为外部第三方服务器 https://example.com，同时不传 apiKey（apiKey 为 None 或未填）
        put_resp = client.put(f"/api/settings/translation-engines/{rec.id}", json={
            "name": "Changed Engine",
            "apiType": "openai_compatible",
            "baseUrl": "https://example.com",
            "model": "m1",
            "enabled": True,
        })
        assert put_resp.status_code == 200
        put_data = put_resp.json()
        assert put_data["baseUrl"] == "https://example.com"
        assert put_data["hasApiKey"] is False  # 密码已被自动清空，防止外泄
        assert put_data["availability"] == "UNCONFIGURED"

        # 再次触发 validate 检测
        val_resp = client.post(f"/api/settings/translation-engines/{rec.id}/validate")
        assert val_resp.status_code == 200
        val_data = val_resp.json()
        assert val_data["available"] is False
        assert val_data["availability"] == "UNCONFIGURED"
        assert val_data["errorCode"] == "missing_api_key"

        # 校验：没有向新的 baseUrl 发起任何带有真实 sk-real-secret-key-12345 的 HTTP 请求
        for call in post_calls:
            headers = call.get("headers") or {}
            auth_hdr = headers.get("Authorization", "") + headers.get("x-api-key", "")
            assert "sk-real-secret-key-12345" not in auth_hdr

    app.dependency_overrides.clear()
    reset_singletons()


def test_api_rejects_ssrf_url_on_create_and_update(tmp_path):
    """测试 API 层在 POST 和 PUT baseUrl 时遇到 SSRF / 内网地址直接返回 422。"""
    from fastapi.testclient import TestClient
    from src.handler.app import app
    from src.handler.deps import get_translation_engine_store, reset_singletons

    db = tmp_path / "engines.db"
    store = TranslationEngineStore(db)

    reset_singletons()
    app.dependency_overrides[get_translation_engine_store] = lambda: store

    with TestClient(app) as client:
        # 1. 尝试 POST 内网地址 127.0.0.1
        r1 = client.post("/api/settings/translation-engines", json={
            "name": "SSRF Engine",
            "apiType": "openai_compatible",
            "baseUrl": "http://127.0.0.1:8000",
            "model": "m1",
            "apiKey": "sk-test",
        })
        assert r1.status_code == 422
        assert "禁止访问私有/回环/链路本地 IP 地址" in r1.json()["detail"]

        # 2. 尝试 POST 云元数据地址 169.254.169.254
        r2 = client.post("/api/settings/translation-engines", json={
            "name": "SSRF Engine 2",
            "apiType": "openai_compatible",
            "baseUrl": "http://169.254.169.254/latest/meta-data",
            "model": "m1",
            "apiKey": "sk-test",
        })
        assert r2.status_code == 422
        assert "禁止访问私有/回环/链路本地 IP 地址" in r2.json()["detail"]

        # 先建一个正常配置
        valid_rec = store.create(
            name="Valid Engine", api_type="openai_compatible",
            base_url="https://api.deepseek.com", model="m1", api_key="sk-test",
        )

        # 3. 尝试 PUT 修改为内网地址
        r3 = client.put(f"/api/settings/translation-engines/{valid_rec.id}", json={
            "name": "Valid Engine",
            "apiType": "openai_compatible",
            "baseUrl": "http://10.0.0.1/v1",
            "model": "m1",
        })
        assert r3.status_code == 422
        assert "禁止访问私有/回环/链路本地 IP 地址" in r3.json()["detail"]

    app.dependency_overrides.clear()
    reset_singletons()
