from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.config.config_repository import set_config_provider
from src.config.redis_store import ConfigUnavailable
from src.handler import deps
from src.handler.app import create_app
from src.plugins import get_plugin_manager
from src.store.task_store import TaskStore

BASE = "/api/settings/plugins"


@pytest.fixture
def plugin_client(tmp_path, memory_config_provider):
    task_store = TaskStore(tmp_path / "tasks.db")
    app = create_app()
    app.dependency_overrides[deps.get_store] = lambda: task_store
    # No lifespan startup: management API tests must not run media recovery,
    # background workers, local model warmup, or Redis subscriptions.
    client = TestClient(app)
    yield client, memory_config_provider, task_store
    client.close()


def _payload(**overrides):
    return {
        "name": "下载备用实例", "stage": "download", "plugin_id": "builtin.ytdlp",
        "implementation_version": "1", "config": {"retries": 2}, **overrides,
    }


def _create(client, provider, **overrides):
    return client.post(
        f"{BASE}/instances", headers={"If-Match": str(provider.read().revision)},
        json=_payload(**overrides),
    )


def _task(store, instance_id, *, status="PENDING", schema_version=2):
    plan = json.dumps({"schema_version": schema_version, "slots": {
        "download": {"instance_id": instance_id, "config": {"retries": 2}},
    }})
    record = store.create(
        url="https://example.com/video", source_lang="en", target_lang="zh-CN",
        mode="subtitle", burn="soft", model="small", engine="remote",
        plugin_plan_json=plan,
    )
    if status != "PENDING":
        store.update(record.id, status=status)
    return record.id, plan


def test_catalog_and_crud_defaults_return_global_revision(plugin_client, monkeypatch):
    client, provider, _ = plugin_client
    def fail_session(*args, **kwargs):
        pytest.fail("management API must not create plugin sessions")
    for registered in get_plugin_manager()._registry.entries():
        monkeypatch.setattr(registered.factory, "create_session", fail_session)
    catalog = client.get(f"{BASE}/catalog")
    assert catalog.status_code == 200
    assert len(catalog.json()["plugins"]) == 8
    assert all("config_schema" in item and "bindings" in item for item in catalog.json()["plugins"])
    initial = client.get(f"{BASE}/instances").json()
    assert initial["revision"] == provider.read().revision
    assert len(initial["instances"]) == len(initial["defaults"]) == 7
    created = _create(client, provider)
    assert created.status_code == 201
    data = created.json()
    instance_id = data["instance"]["id"]
    assert data["revision"] == initial["revision"] + 1
    assert data["instance"]["config"] == {"retries": 2}
    assert client.get(f"{BASE}/instances/{instance_id}").json() == data
    changed = client.put(
        f"{BASE}/instances/{instance_id}", headers={"If-Match": str(data["revision"])},
        json={"name": "新名称", "config": {"retries": 4}},
    )
    assert changed.status_code == 200
    assert changed.json()["instance"]["config"] == {"retries": 4}
    defaults = client.put(
        f"{BASE}/defaults", headers={"If-Match": str(changed.json()["revision"])},
        json={"defaults": {"download": instance_id}},
    )
    assert defaults.status_code == 200
    assert defaults.json()["defaults"] == {**initial["defaults"], "download": instance_id}
    assert client.get(f"{BASE}/defaults").json() == defaults.json()
    reverted = client.put(
        f"{BASE}/defaults", headers={"If-Match": str(defaults.json()["revision"])},
        json={"defaults": {"download": "default_download"}},
    )
    deleted = client.delete(
        f"{BASE}/instances/{instance_id}", headers={"If-Match": str(reverted.json()["revision"])},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"revision": reverted.json()["revision"] + 1, "ok": True}
    assert client.get(f"{BASE}/instances/{instance_id}").status_code == 404


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/instances", _payload()),
    ("PUT", "/instances/default_download", {"name": "new"}),
    ("DELETE", "/instances/default_download", None),
    ("PUT", "/defaults", {"defaults": {}}),
])
def test_every_write_requires_numeric_if_match(plugin_client, method, path, body):
    client, provider, _ = plugin_client
    before = provider.read()
    for header in (None, "not-a-number", "-1", "1.0", '"1"'):
        headers = {} if header is None else {"If-Match": header}
        response = client.request(method, BASE + path, headers=headers, json=body)
        assert response.status_code == 422
    assert provider.read() == before


def test_all_routes_require_api_token(plugin_client):
    client, provider, _ = plugin_client
    provider.mutate(lambda document: document["business"].update({"api_token": "unit-test-token"}))
    requests = (
        ("GET", "/catalog", None), ("GET", "/instances", None),
        ("GET", "/instances/default_download", None), ("GET", "/defaults", None),
        ("POST", "/instances", _payload()),
        ("PUT", "/instances/default_download", {"name": "new"}),
        ("DELETE", "/instances/default_download", None),
        ("PUT", "/defaults", {"defaults": {}}),
    )
    for method, path, body in requests:
        response = client.request(method, BASE + path, json=body, headers={"If-Match": str(provider.read().revision)})
        assert response.status_code == 401
    response = client.get(f"{BASE}/catalog", headers={"Authorization": "Bearer unit-test-token"})
    assert response.status_code == 200
    created = client.post(f"{BASE}/instances", json=_payload(), headers={
        "If-Match": str(provider.read().revision), "X-API-Token": "unit-test-token",
    })
    assert created.status_code == 201


def test_stale_global_revision_returns_409_without_changing_instances(plugin_client):
    client, provider, _ = plugin_client
    created = _create(client, provider).json()
    instance_id = created["instance"]["id"]
    provider.mutate(lambda document: document["business"].update({"debug": True}))
    before = provider.read()
    for method, path, body in (
        ("POST", "/instances", _payload()),
        ("PUT", f"/instances/{instance_id}", {"name": "stale"}),
        ("DELETE", f"/instances/{instance_id}", None),
        ("PUT", "/defaults", {"defaults": {"download": instance_id}}),
    ):
        response = client.request(method, BASE + path, json=body, headers={"If-Match": str(created["revision"])})
        assert response.status_code == 409
    assert provider.read() == before


def test_default_instance_cannot_be_deleted_or_disabled(plugin_client):
    client, provider, _ = plugin_client
    before = provider.read()
    for method, body in (("PUT", {"enabled": False}), ("DELETE", None)):
        response = client.request(
            method, f"{BASE}/instances/default_download", json=body,
            headers={"If-Match": str(before.revision)},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "PLUGIN_INSTANCE_CONFLICT"
        assert "默认" in response.json()["detail"]["message"]
    assert provider.read() == before


@pytest.mark.parametrize("status", ["PENDING", "TRANSCRIBING", "BURNING"])
def test_active_v2_task_blocks_disable_delete_but_keeps_frozen_plan_on_updates(plugin_client, status):
    client, provider, task_store = plugin_client
    item = _create(client, provider).json()["instance"]
    instance_id = item["id"]
    task_id, frozen_plan = _task(task_store, instance_id, status=status)
    headers = {"If-Match": str(provider.read().revision)}
    assert client.delete(f"{BASE}/instances/{instance_id}", headers=headers).status_code == 409
    assert client.put(f"{BASE}/instances/{instance_id}", headers=headers, json={"enabled": False}).status_code == 409
    updated = client.put(
        f"{BASE}/instances/{instance_id}", headers=headers, json={"config": {"retries": 8}},
    )
    assert updated.status_code == 200
    assert task_store.get(task_id).plugin_plan_json == frozen_plan


@pytest.mark.parametrize("status,version", [("SUCCESS", 2), ("FAILED", 2), ("CANCELLED", 2), ("PENDING", 1)])
def test_terminal_and_legacy_tasks_do_not_block_instance_removal(plugin_client, status, version):
    client, provider, task_store = plugin_client
    instance_id = _create(client, provider).json()["instance"]["id"]
    task_id, plan = _task(task_store, instance_id, status=status, schema_version=version)
    response = client.delete(f"{BASE}/instances/{instance_id}", headers={"If-Match": str(provider.read().revision)})
    assert response.status_code == 200
    assert task_store.get(task_id).plugin_plan_json == plan


@pytest.mark.parametrize("body", [
    {"config": {"quality": "best"}}, {"config": {"retries": True}},
    {"config": {"api_key": "do-not-leak"}},
    {"config": {"proxy": "https://user:do-not-leak@example.com"}},
    {"config": {"proxy": "https://example.com?token=do-not-leak"}},
    {"config": {"proxy": "https://example.com#x"}},
    {"plugin_id": "unknown"}, {"implementation_version": "99"},
    {"engine_ref": {"id": "missing"}},
])
def test_instance_validation_returns_422_and_keeps_revision(plugin_client, body):
    client, provider, _ = plugin_client
    before = provider.read()
    response = _create(client, provider, **body)
    assert response.status_code == 422
    assert "do-not-leak" not in response.text
    assert provider.read() == before
    if body == {"config": {"quality": "best"}}:
        assert response.json()["detail"]["code"] == "invalid_input"


def test_invalid_defaults_and_missing_instances(plugin_client):
    client, provider, _ = plugin_client
    before = provider.read()
    for body in (
        {}, {"defaults": None}, {"defaults": {"download": None}},
        {"defaults": {"download": "missing"}}, {"defaults": {"download": "default_render"}},
        {"defaults": {"unknown": "default_download"}}, {"defaults": {}, "other": True},
    ):
        assert client.put(f"{BASE}/defaults", headers={"If-Match": str(before.revision)}, json=body).status_code == 422
    for method in ("GET", "PUT", "DELETE"):
        response = client.request(
            method, f"{BASE}/instances/missing", json={"name": "new"} if method == "PUT" else None,
            headers={"If-Match": str(before.revision)},
        )
        assert response.status_code == 404
    assert provider.read() == before


def test_redis_unavailable_uses_existing_app_503_handler(plugin_client):
    client, _, _ = plugin_client
    class DownProvider:
        def read(self):
            raise ConfigUnavailable("do-not-leak")
        def mutate(self, *_args, **_kwargs):
            raise ConfigUnavailable("do-not-leak")
    set_config_provider(DownProvider())
    for method, path, body in (
        ("GET", "/catalog", None), ("GET", "/instances", None),
        ("POST", "/instances", _payload()), ("PUT", "/defaults", {"defaults": {}}),
    ):
        response = client.request(method, BASE + path, json=body, headers={"If-Match": "1"})
        assert response.status_code == 503
        assert "do-not-leak" not in response.text
