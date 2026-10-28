"""Creation preflight exercises real runtime and plugin-plan resolution."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.config.config_repository import get_config_provider
from src.handler import tasks as task_routes
from src.handler.schemas import TaskCreate
from src.plugin_system import PluginError
from src.plugin_system.manager import PluginManager
from src.plugin_system.manifest import PluginManifest
from src.plugin_system.registry import PluginRegistry
from src.plugin_system.manifest import REQUIRED_OPERATIONS
from src.plugins.builtin import factories
from src.plugins.builtin.configuration import BuiltinFactory
from src.service import model_runtime
from src.service.plugin_plan import parse_plugin_plan
from src.service.task import task_lifecycle
from src.store import ModelSettingsStore, TaskStore, TranslationEngineStore


class _Factory:
    def __init__(self, plugin_id, stage, version="1", *, failure=None):
        self.stage = stage
        self.manifest = PluginManifest(
            plugin_id=plugin_id, stage=stage, implementation_version=version,
            operations=REQUIRED_OPERATIONS[stage],
        )
        self.calls = []
        self.failure = failure

    def preflight(self, config, **kwargs):
        self.calls.append((dict(config), kwargs))
        if self.failure:
            raise self.failure

    def create_session(self, *_args):
        pytest.fail("creation preflight must not create a media session")


@pytest.fixture
def creation_plugins(monkeypatch):
    """Keep real manager checks; substitute unavailable external dependencies."""
    from src.core import ffmpeg_utils
    from src.service.model_manager import model_manager
    from src.service import translation_model_manager
    import src.plugins

    monkeypatch.setattr(BuiltinFactory, "_module", lambda *_args: None)
    monkeypatch.setattr(BuiltinFactory, "_binary", lambda *_args: None)
    monkeypatch.setattr(ffmpeg_utils, "has_subtitles_filter", lambda *_args: True)
    monkeypatch.setattr(model_manager, "is_ready", lambda *_args: True)
    monkeypatch.setattr(translation_model_manager, "get_translation_model_manager", lambda: SimpleNamespace(
        is_ready=lambda: True, dependency_status=lambda: {"ready": True, "missing": []},
    ))
    registry = PluginRegistry()
    for factory in factories():
        registry.register(factory.manifest, factory)
    custom = {}
    for plugin_id, stage, version in (
        ("custom.downloader", "download", "5"),
        ("replacement.downloader", "download", "6"),
        ("backend.ytdlp-v2", "download", "2"),
        ("backend.ytdlp-v3", "download", "3"),
        ("backend.translator-v4", "translate", "4"),
        ("backend.translator-v5", "translate", "5"),
        ("custom.asr", "transcribe", "1"),
        ("custom.translate", "translate", "1"),
        ("custom.tts", "tts", "1"),
    ):
        factory = _Factory(plugin_id, stage, version)
        registry.register(factory.manifest, factory)
        custom[plugin_id] = factory
    manager = PluginManager(registry)
    monkeypatch.setattr(src.plugins, "get_plugin_manager", lambda: manager)
    return manager, custom


def _plugins(monkeypatch, **stages):
    monkeypatch.setenv("SUBTRANS_PIPELINE_PLUGINS", json.dumps({
        stage: {"plugin_id": plugin_id, "implementation_version": "1"}
        for stage, plugin_id in stages.items()
    }))

    def select(document):
        section = document["plugins"]
        for stage, plugin_id in stages.items():
            section["byId"][section["defaults"][stage]]["plugin_id"] = plugin_id

    get_config_provider().mutate(select)


@pytest.fixture
def creation_client(tmp_path, monkeypatch, creation_plugins):
    store = TaskStore(tmp_path / "tasks.db")
    engines = TranslationEngineStore()
    engine = engines.create(name="Cloud", api_type="openai_compatible", model="fixed-model",
                            base_url="https://translate.example/v1", api_key="test-key")
    engines.update(engine.id, availability="AVAILABLE")
    app = FastAPI()
    app.include_router(task_lifecycle.router)
    app.dependency_overrides[task_lifecycle.get_store] = lambda: store
    app.dependency_overrides[task_lifecycle.get_translation_engine_store] = lambda: engines
    app.dependency_overrides[task_lifecycle.require_api_token] = lambda: None
    queued = []
    monkeypatch.setattr(task_routes, "enqueue_pipeline", queued.append)
    monkeypatch.setattr(task_routes, "task_dir", lambda task_id: tmp_path / "data" / task_id)
    monkeypatch.setattr(task_routes, "probe_duration", lambda *_args: 10.0)
    with TestClient(app) as client:
        yield client, store, engine, queued


def _request(client, source_type, engine, **overrides):
    body = {"sourceLang": "en", "targetLang": "zh-CN", "burn": "soft",
            "model": "local:tiny", "engine": engine, "needSubtitle": True}
    body.update(overrides)
    if source_type == "url":
        return client.post("/api/tasks", json={"url": "https://video.example/selected", **body})
    return client.post("/api/tasks/upload", data=body,
                       files={"file": ("clip.mp4", io.BytesIO(b"media"), "video/mp4")})


@pytest.mark.parametrize("source_type", ["url", "upload"])
def test_real_create_custom_slots_skip_disabled_builtin_cards(
    creation_client, creation_plugins, monkeypatch, source_type,
):
    client, store, _engine, queued = creation_client
    _manager, custom = creation_plugins
    _plugins(monkeypatch, transcribe="custom.asr", translate="custom.translate", tts="custom.tts")
    ModelSettingsStore().update({
        kind: {"enabled": False, "models": []} for kind in ("llm", "whisper", "tts")
    })
    monkeypatch.setattr(task_routes, "_ensure_local_model_ready", lambda *_args: pytest.fail("custom ASR used Whisper"))
    monkeypatch.setattr(task_routes, "get_translation_model_manager", lambda: pytest.fail("custom translator used local engine"))
    response = _request(client, source_type, "missing-engine", targetLang="ja",
                        ttsEnabled=True, ttsVoice="custom-ja-voice")
    assert response.status_code == 201, response.text
    record = store.get(response.json()["id"])
    snapshot = json.loads(record.runtime_config_json)
    plan = parse_plugin_plan(record.plugin_plan_json)
    assert snapshot["engine"]["source"] == "disabled"
    assert snapshot["whisper"]["source"] == "disabled"
    assert plan["slots"]["tts"]["config"]["voice"] == "custom-ja-voice"
    for plugin_id in ("custom.asr", "custom.translate", "custom.tts"):
        assert len(custom[plugin_id].calls) == 1
    assert queued == [record.id]


def test_url_creation_persists_pinned_tts_voice(creation_client):
    client, store, engine, queued = creation_client

    def configure_pinned_voice(values):
        values["models"]["tts"] = {
            "enabled": True,
            "models": [{
                "id": "pinned-tts",
                "provider": "kokoro",
                "model": "kokoro-v1",
                "voice": "zf_001",
                "enabled": True,
                "isCurrent": True,
            }],
        }
        values["plugins"]["byId"]["default_tts"].update({
            "plugin_id": "custom.tts",
            "model_ref": {"kind": "tts", "card_id": "pinned-tts"},
        })

    get_config_provider().mutate(configure_pinned_voice)
    response = _request(
        client, "url", engine.id, ttsEnabled=True, ttsVoice="auto",
    )
    assert response.status_code == 201, response.text

    record = store.get(response.json()["id"])
    snapshot = json.loads(record.runtime_config_json)
    plan = parse_plugin_plan(record.plugin_plan_json)
    assert record.tts_voice == "zf_001"
    assert response.json()["ttsVoice"] == "zf_001"
    assert snapshot["tts"]["voice"] == "zf_001"
    assert plan["slots"]["tts"]["config"]["voice"] == "zf_001"
    assert queued == [record.id]


@pytest.mark.parametrize("source_type", ["url", "upload"])
def test_real_create_http_asr_ignores_disabled_whisper_card(
    creation_client, monkeypatch, memory_config_provider, source_type,
):
    client, store, engine, _queued = creation_client
    memory_config_provider.mutate(lambda document: document["business"].update({
        "transcriber_backend": "http", "transcriber_url": "https://asr.example/v1",
    }))
    ModelSettingsStore().update({"whisper": {"enabled": False, "models": []}})
    monkeypatch.setattr(task_routes, "_ensure_local_model_ready", lambda *_args: pytest.fail("HTTP ASR used local model"))
    response = _request(client, source_type, engine.id, model="remote-asr-model")
    assert response.status_code == 201, response.text
    record = store.get(response.json()["id"])
    assert json.loads(record.runtime_config_json)["whisper"]["provider"] == "http"
    assert parse_plugin_plan(record.plugin_plan_json)["slots"]["transcribe"]["config"]["backend"] == "http"


@pytest.mark.parametrize("source_type", ["url", "upload"])
def test_selected_factory_error_prevents_task_and_upload_artifacts(
    creation_client, creation_plugins, monkeypatch, tmp_path, source_type,
):
    client, store, engine, queued = creation_client
    _manager, custom = creation_plugins
    _plugins(monkeypatch, transcribe="custom.asr")
    custom["custom.asr"].failure = PluginError(
        "dependency_missing", "transcribe", "custom.asr", "ASR SDK missing",
    )
    response = _request(client, source_type, engine.id)
    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "dependency_missing", "message": "ASR SDK missing"}
    assert store.list() == []
    assert queued == []
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("source_type", ["url", "upload"])
def test_real_download_only_skips_subtitle_plugins(creation_client, monkeypatch, source_type):
    client, store, _engine, _queued = creation_client
    _plugins(monkeypatch, transcribe="unknown.asr", translate="unknown.translate", tts="unknown.tts")
    ModelSettingsStore().update({
        kind: {"enabled": False, "models": []} for kind in ("llm", "whisper", "tts")
    })
    response = _request(client, source_type, "missing-engine", needSubtitle=False)
    assert response.status_code == 201, response.text
    slots = parse_plugin_plan(store.get(response.json()["id"]).plugin_plan_json)["slots"]
    assert set(slots) == ({"download"} if source_type == "url" else set())


def test_task_schema_keeps_only_generic_tts_constraint():
    assert TaskCreate(url="https://video.example", ttsEnabled=True, targetLang="ja", ttsVoice="ja-custom")
    with pytest.raises(ValueError, match="配音需要启用字幕翻译"):
        TaskCreate(url="https://video.example", ttsEnabled=True, needSubtitle=False)


@pytest.mark.parametrize("source_type", ["url", "upload"])
def test_real_builtin_whisper_keeps_409_error_and_no_task(creation_client, monkeypatch, source_type):
    client, store, engine, queued = creation_client

    def not_ready(_model):
        raise HTTPException(status_code=409, detail={"code": "MODEL_NOT_READY", "message": "not ready"})

    monkeypatch.setattr(task_routes, "_ensure_local_model_ready", not_ready)
    response = _request(client, source_type, engine.id)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MODEL_NOT_READY"
    assert store.list() == [] and queued == []
