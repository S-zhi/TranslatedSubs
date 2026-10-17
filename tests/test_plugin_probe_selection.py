"""Probe the same download instance/config that task creation will freeze."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config.config_repository import get_config_provider
from src.handler import deps, tasks
from src.plugin_system import ProbeResult
from src.plugins import get_plugin_manager
from src.service.plugin_plan import parse_plugin_plan
from src.service.task import task_lifecycle
from src.store import ProbeStore, TaskStore
from src.store.plugin_instance_store import PluginInstanceStore
from test_plugin_preflight import creation_plugins  # noqa: F401


@pytest.fixture
def probe_client(tmp_path, monkeypatch, creation_plugins):
    probes = ProbeStore(tmp_path / "probes.db")
    store = TaskStore(tmp_path / "tasks.db")
    app = FastAPI()
    app.include_router(task_lifecycle.router)
    app.dependency_overrides[deps.get_probe_store] = lambda: probes
    app.dependency_overrides[deps.get_store] = lambda: store
    app.dependency_overrides[deps.require_api_token] = lambda: None
    monkeypatch.setattr(tasks, "enqueue_pipeline", lambda *_: None)
    with TestClient(app) as client:
        yield client, store, probes


def _instance(**overrides):
    provider = get_config_provider()
    result, _ = PluginInstanceStore(provider=provider).create({
        "id": "selected-download", "name": "Selected downloader", "stage": "download",
        "plugin_id": "builtin.ytdlp", "implementation_version": "1",
        "config": {"proxy": "http://proxy.example:8080", "socket_timeout": 7},
        **overrides,
    }, expected_revision=provider.read().revision)
    return result


def test_explicit_probe_matches_created_plan_even_if_default_probe_fails(probe_client, monkeypatch):
    client, store, probes = probe_client
    instance = _instance()
    observed = []
    batch_updates = []
    monkeypatch.setattr(tasks.probe_batch_manager, "record_manual_probe", lambda *args: batch_updates.append(args))

    def capture(_manager, plugin_id, version, request, config, context):
        observed.append((plugin_id, version, dict(config)))
        return ProbeResult(ok=bool(config.get("proxy")), title="Selected configuration")

    monkeypatch.setattr(type(get_plugin_manager()), "run_probe", capture)
    url = "https://video.example/selected"
    assert client.post("/api/tasks/probe", json={"url": url}).json()["ok"] is False
    payload = {"url": url, "pluginInstances": {"download": instance["id"]}, "quality": "720p"}
    response = client.post("/api/tasks/probe", json=payload)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    created = client.post("/api/tasks", json={**payload, "needSubtitle": False})
    assert created.status_code == 201
    slot = parse_plugin_plan(store.get(created.json()["id"]).plugin_plan_json)["slots"]["download"]
    assert observed[-1] == (slot["plugin_id"], slot["implementation_version"], slot["config"])
    assert slot["instance_id"] == instance["id"]
    assert len(probes.list(limit=10)) == 1
    assert probes.list(limit=10)[0].ok == 1
    assert len(batch_updates) == 1
    assert batch_updates[0][1].ok is False


@pytest.mark.parametrize("damage", ["missing", "disabled", "stage", "unregistered", "version"])
def test_invalid_probe_instance_is_rejected_before_probe_or_history(probe_client, monkeypatch, damage):
    client, _store, probes = probe_client
    instance = _instance()
    selected = instance["id"]
    if damage == "missing":
        selected = "missing"
    elif damage == "stage":
        selected = "default_transcribe"
    else:
        changes = {"disabled": {"enabled": False}, "unregistered": {"plugin_id": "absent.download"},
                   "version": {"implementation_version": "999"}}[damage]
        get_config_provider().mutate(lambda doc: doc["plugins"]["byId"][selected].update(changes))
    monkeypatch.setattr(type(get_plugin_manager()), "run_probe", lambda *_: pytest.fail("invalid selection reached plugin"))
    response = client.post("/api/tasks/probe", json={
        "url": "https://video.example/invalid", "pluginInstances": {"download": selected},
    })
    assert response.status_code == 422
    assert probes.list(limit=10) == []


def test_probe_ignores_unused_stage_selection_and_uses_default_download(probe_client, monkeypatch):
    client, _store, _probes = probe_client
    observed = []
    def capture(_manager, plugin_id, _version, _request, config, _context):
        observed.append((plugin_id, config["proxy"]))
        return ProbeResult(ok=True)
    monkeypatch.setattr(type(get_plugin_manager()), "run_probe", capture)
    response = client.post("/api/tasks/probe", json={
        "url": "https://video.example/default", "pluginInstances": {"translate": "deleted-unused"},
    })
    assert response.status_code == 200
    assert observed == [("builtin.ytdlp", None)]
