"""Integration checks for the unified model settings runtime contract."""

from __future__ import annotations

import json
import dataclasses
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.handler.app import app
from src.handler import deps
from src.handler import model_settings as model_settings_routes
from src.service import runner
from src.service import model_runtime
from src.service.model_runtime import resolve_runtime
from src.service.plugin_plan import parse_plugin_plan
from src.service.orchestrator import PipelineEvent
from src.handler import tasks as tasks_routes
from src.store import ModelSettingsStore, TaskStore, TranslationEngineStore
from src.config.config_repository import get_config_provider
from test_plugin_preflight import creation_plugins  # noqa: F401

pytestmark = pytest.mark.usefixtures("creation_plugins")


class _ReadyTranslationManager:
    def is_ready(self):
        return True

    def dependency_status(self):
        return {"ready": True, "missing": []}


def _body(**overrides):
    values = {
        "url": "https://example.test/video",
        "sourceLang": "en",
        "targetLang": "en",
        "mode": "mono",
        "burn": "soft",
        "model": "local:tiny",
        "engine": "deepseek",
        "needSubtitle": True,
        "quality": "480p",
        "ttsEnabled": True,
        "ttsVoice": "auto",
        "originalVoiceMode": "keep",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _save_cards(model_store: ModelSettingsStore) -> None:
    model_store.update({
        "llm": {
            "provider": "openai_compatible",
            "model": "configured-v1",
            "baseUrl": "https://llm.example/v1",
            "apiKey": "sk-unified-secret",
            "models": [{
                "id": "llm-deepseek",
                "provider": "openai_compatible",
                "model": "configured-v1",
                "baseUrl": "https://llm.example/v1",
                "apiKey": "sk-unified-secret",
                "isCurrent": True,
            }],
        },
        "whisper": {
            "provider": "local",
            "model": "small",
            "models": [{
                "id": "whisper-default",
                "provider": "local",
                "model": "small",
                "isCurrent": True,
                "enabled": True,
            }],
        },
        "tts": {
            "enabled": True,
            "provider": "local",
            "model": "kokoro-v1",
            "voice": "af_maple",
            "speed": 1.25,
            "format": "wav",
            "models": [{
                "id": "tts-default",
                "provider": "local",
                "model": "kokoro-v1",
                "voice": "af_maple",
                "speed": 1.25,
                "format": "wav",
                "isCurrent": True,
                "enabled": True,
            }],
        },
    })


def test_saved_settings_are_snapshotted_and_used_by_runner(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    task_store = TaskStore(db_path)
    engine_store = TranslationEngineStore(db_path)
    engine_store.ensure_default_deepseek(api_key="sk-legacy")
    model_store = ModelSettingsStore(db_path)
    _save_cards(model_store)
    monkeypatch.setattr(model_runtime.importlib.util, "find_spec", lambda _name: object())

    runtime = resolve_runtime(
        engine="deepseek",
        need_subtitle=True,
        request_model="local:tiny",
        source_lang="en",
        target_lang="en",
        tts_enabled=True,
        tts_voice="auto",
        engines=engine_store,
        model_store=model_store,
        compat_settings=SimpleNamespace(db_path=db_path, deepseek_api_key=None),
        translation_model_manager=_ReadyTranslationManager(),
        ensure_local_model_ready=lambda _model: None,
    )
    snapshot_json = json.dumps(runtime.snapshot, ensure_ascii=False)
    assert "sk-unified-secret" not in snapshot_json
    assert runtime.snapshot["engine"]["model"] == "configured-v1"
    assert runtime.snapshot["whisper"]["model"] == "local:small"
    assert runtime.snapshot["tts"]["voice"] == "af_maple"
    assert runtime.snapshot["schemaVersion"] == 2
    assert runtime.snapshot["configRevision"] > 0
    assert "business" in runtime.snapshot

    rec, created = tasks_routes._task_lifecycle.create_url_task(
        body=_body(),
        store=task_store,
        engines=engine_store,
        ensure_supported_translation_languages=lambda *_args: None,
        ensure_local_model_ready=lambda _model: None,
        ensure_translation_engine=lambda *_args: None,
        resolve_runtime=lambda **kwargs: runtime,
    )
    assert created is True
    assert json.loads(rec.runtime_config_json)["engine"]["model"] == "configured-v1"

    # Changing the selected card while the task is queued must not change its
    # model or endpoint. Secret rotation remains allowed at execution time.
    model_store.update({
        "llm": {
            "models": [{
                "id": "llm-deepseek",
                "provider": "openai_compatible",
                "model": "changed-v2",
                "baseUrl": "https://changed.example/v1",
                "isCurrent": True,
            }],
        },
    })

    seen = {}

    def fake_pipeline(params, on_event, *, plugin_manager, plugin_plan, credential_resolver, legacy_cache):
        seen["model"] = params.model
        seen["tts_voice"] = params.tts_voice
        translated = plugin_plan["slots"]["translate"]["config"]
        seen["engine_model"] = translated["model"]
        seen["engine_base_url"] = translated["base_url"]
        seen["api_key"] = credential_resolver(translated["credential_ref"])
        seen["tts_config"] = plugin_plan["slots"]["tts"]["config"]
        on_event(PipelineEvent("SUCCESS", 100, None, outputs={}))

    monkeypatch.setattr(runner, "_store", task_store)
    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    runner._run(rec.id)

    assert seen == {
        "model": "local:small",
        "tts_voice": "af_maple",
        "engine_model": "configured-v1",
        "engine_base_url": "https://llm.example/v1",
        "api_key": "sk-unified-secret",
        "tts_config": {
            "voice": "af_maple", "target_lang": "en", "bilingual": False,
            "speed": 1.25, "model_id": "kokoro-v1", "output_format": "wav",
        },
    }
    assert "sk-unified-secret" not in rec.plugin_plan_json
    assert parse_plugin_plan(rec.plugin_plan_json)["slots"]["transcribe"]["config"]["model"] == "small"
    assert task_store.get(rec.id).status == "SUCCESS"


def test_deepseek_does_not_fall_back_to_settings_secret():
    with pytest.raises(HTTPException) as exc_info:
        resolve_runtime(
            engine="deepseek", need_subtitle=True, request_model="local:tiny",
            source_lang="en", target_lang="zh-CN", tts_enabled=False,
            tts_voice="auto", engines=TranslationEngineStore(),
            model_store=ModelSettingsStore(),
            compat_settings=SimpleNamespace(deepseek_api_key="sk-env-secret"),
            ensure_local_model_ready=lambda _model: None,
        )
    assert exc_info.value.status_code == 422
    assert "Redis" in str(exc_info.value.detail)


def test_runtime_rejects_disabled_unified_card(tmp_path):
    store = ModelSettingsStore()
    store.update({"llm": {
        "enabled": False,
        "models": [{"id": "llm-off", "provider": "openai_compatible",
                    "model": "m", "apiKey": "sk-secret", "isCurrent": True}],
    }})
    with pytest.raises(HTTPException) as exc_info:
        resolve_runtime(
            engine="deepseek", need_subtitle=True, request_model="local:tiny",
            source_lang="en", target_lang="zh-CN", tts_enabled=False,
            tts_voice="auto", engines=TranslationEngineStore(), model_store=store,
            ensure_local_model_ready=lambda _model: None,
        )
    assert exc_info.value.detail["code"] == "MODEL_DISABLED"


def test_runner_fails_closed_when_snapshotted_model_card_is_disabled(tmp_path, monkeypatch):
    provider = get_config_provider()
    store = ModelSettingsStore()
    engines = TranslationEngineStore()
    engines.ensure_default_deepseek()
    engines.update("deepseek", api_key="sk-engine", availability="AVAILABLE")
    _save_cards(store)
    monkeypatch.setattr(model_runtime.importlib.util, "find_spec", lambda _name: object())
    runtime = resolve_runtime(
        engine="deepseek", need_subtitle=True, request_model="local:tiny",
        source_lang="en", target_lang="zh-CN", tts_enabled=False,
        tts_voice="auto", engines=engines, model_store=store,
        ensure_local_model_ready=lambda _model: None,
    )
    rec = TaskStore(tmp_path / "tasks.db").create(
        url="https://example.test/card", source_lang="en", target_lang="zh-CN",
        mode="mono", burn="soft", model="local:tiny", engine="deepseek",
        runtime_config_json=json.dumps(runtime.snapshot),
    )
    store.update({"llm": {"enabled": False}})
    task_store = TaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(runner, "_store", task_store)
    monkeypatch.setattr(runner, "run_pipeline", lambda *_args, **_kwargs: pytest.fail("must fail closed"))
    runner._run(rec.id)
    assert task_store.get(rec.id).status == "FAILED"
    assert task_store.get(rec.id).error_code == "engine_credentials_missing"


def test_legacy_task_persists_routing_and_business_snapshot_for_retry(tmp_path, monkeypatch):
    config_provider = get_config_provider()
    config_provider.mutate(lambda document: document["business"].update({"download_quality": "360p"}))
    engines = TranslationEngineStore()
    engines.ensure_default_deepseek()
    engines.update("deepseek", api_key="sk-legacy", availability="AVAILABLE")
    task_store = TaskStore(tmp_path / "tasks.db")
    rec = task_store.create(
        url="https://example.test/legacy", source_lang="en", target_lang="zh-CN",
        mode="mono", burn="soft", model="local:tiny", engine="deepseek",
    )
    monkeypatch.setattr(runner, "_store", task_store)
    observed = []
    def fake_pipeline(_params, on_event, *, api_key=None, engine_config=None):
        from src.config import settings
        observed.append(settings.download_quality)
        on_event(PipelineEvent("SUCCESS", 100, None, outputs={}))
    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    runner._run(rec.id)
    persisted = json.loads(task_store.get(rec.id).runtime_config_json)
    assert persisted["business"]["download_quality"] == "360p"
    assert "engine" in persisted and "whisper" in persisted and "tts" in persisted
    config_provider.mutate(lambda document: document["business"].update({"download_quality": "720p"}))
    runner._run(rec.id)
    assert observed == ["360p", "360p"]


def test_malformed_snapshot_fails_without_dynamic_fallback(tmp_path, monkeypatch):
    task_store = TaskStore(tmp_path / "tasks.db")
    rec = task_store.create(
        url="https://example.test/bad-snapshot", source_lang="en", target_lang="zh-CN",
        mode="mono", burn="soft", model="local:tiny", engine="deepseek",
        runtime_config_json="[",
    )
    monkeypatch.setattr(runner, "_store", task_store)
    monkeypatch.setattr(runner, "run_pipeline", lambda *_args, **_kwargs: pytest.fail("must not run"))
    runner._run(rec.id)
    assert task_store.get(rec.id).status == "FAILED"
    assert task_store.get(rec.id).error_code == "runtime_snapshot_invalid"


@pytest.mark.parametrize("damage", ["engine-type", "tts-speed"])
def test_structurally_invalid_v2_snapshot_fails_and_cleans_up(tmp_path, monkeypatch, damage):
    task_store = TaskStore(tmp_path / f"invalid-{damage}.db")
    snapshot = {
        "schemaVersion": 2, "configRevision": 1, "business": {},
        "engine": {"source": "disabled", "id": "deepseek", "provider": "",
                   "model": "", "baseUrl": "", "params": {}},
        "whisper": {"source": "disabled", "cardId": None, "provider": "", "model": ""},
        "tts": {"enabled": False, "source": "request", "cardId": None,
                "provider": "", "model": "", "voice": "auto", "speed": 1.0,
                "format": "wav"},
    }
    if damage == "engine-type":
        snapshot["engine"] = []
    else:
        snapshot["tts"]["speed"] = "bad"
    rec = task_store.create(
        url="https://example.test/invalid", source_lang="en", target_lang="zh-CN",
        mode="mono", burn="soft", model="local:tiny", engine="deepseek",
        runtime_config_json=json.dumps(snapshot),
    )
    monkeypatch.setattr(runner, "_store", task_store)
    monkeypatch.setattr(runner, "run_pipeline", lambda *_args, **_kwargs: pytest.fail("invalid snapshot must not run"))
    cleanup_calls = []
    unregister_calls = []
    monkeypatch.setattr(runner, "_cleanup_partial_artifacts", cleanup_calls.append)
    monkeypatch.setattr(runner, "unregister_cancellation_signal", unregister_calls.append)

    runner._run(rec.id)

    saved = task_store.get(rec.id)
    assert saved.status == "FAILED"
    assert saved.error_code == "runtime_snapshot_invalid"
    assert damage not in (saved.error or "")
    assert cleanup_calls == [rec.id]
    assert unregister_calls == [rec.id]


def test_partial_v1_routing_is_completed_once_and_preserved_on_retry(tmp_path, monkeypatch):
    provider = get_config_provider()
    engines = TranslationEngineStore()
    engines.ensure_default_deepseek()
    engines.update("deepseek", api_key="sk-live", availability="AVAILABLE")
    task_store = TaskStore(tmp_path / "v1-partial.db")
    v1 = {
        "schemaVersion": 1,
        "engine": {
            "source": "legacy", "id": "deepseek", "provider": "deepseek",
            "model": "fixed-v1-model", "baseUrl": "https://old.example/v1",
            "params": {},
        },
        "whisper": {"model": "local:tiny"},
    }
    rec = task_store.create(
        url="https://example.test/v1", source_lang="en", target_lang="zh-CN",
        mode="mono", burn="soft", model="local:small", engine="deepseek",
        runtime_config_json=json.dumps(v1),
    )
    monkeypatch.setattr(runner, "_store", task_store)
    observed = []

    def fake_pipeline(params, on_event, *, api_key=None, engine_config=None):
        observed.append((params.model, engine_config.model))
        on_event(PipelineEvent("SUCCESS", 100, None, outputs={}))

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    runner._run(rec.id)
    first = json.loads(task_store.get(rec.id).runtime_config_json)
    assert first["schemaVersion"] == 2
    assert first["engine"]["source"] == "translation_store"
    assert first["engine"]["model"] == "fixed-v1-model"
    assert first["whisper"]["model"] == "local:tiny"
    assert first["tts"]["enabled"] is False
    assert "api_key" not in first["engine"]

    provider.mutate(lambda document: document["engines"]["byId"]["deepseek"].update(
        model="changed-model", api_key="sk-rotated",
    ))
    runner._run(rec.id)
    assert observed == [("local:tiny", "fixed-v1-model"), ("local:tiny", "fixed-v1-model")]


def test_queued_engine_uses_rotated_key_without_revalidation(tmp_path, monkeypatch):
    engines = TranslationEngineStore()
    engine = engines.create(
        name="Rotatable", api_type="openai_compatible", base_url="https://llm.example/v1",
        model="fixed-model", api_key="first-key",
    )
    engines.update(engine.id, availability="AVAILABLE")
    runtime = resolve_runtime(
        engine=engine.id, need_subtitle=True, request_model="local:tiny",
        source_lang="en", target_lang="zh-CN", tts_enabled=False, tts_voice="auto",
        engines=engines, ensure_local_model_ready=lambda _model: None,
    )
    task_store = TaskStore(tmp_path / "rotated-key.db")
    rec = task_store.create(
        url="https://example.test/key-rotation", source_lang="en", target_lang="zh-CN",
        mode="mono", burn="soft", model="local:tiny", engine=engine.id,
        runtime_config_json=json.dumps(runtime.snapshot),
    )
    engines.update(engine.id, api_key="second-key")
    assert engines.get(engine.id).availability == "UNKNOWN"
    monkeypatch.setattr(runner, "_store", task_store)
    observed = []

    def fake_pipeline(_params, on_event, *, api_key=None, engine_config=None):
        observed.append(engine_config.api_key)
        on_event(PipelineEvent("SUCCESS", 100, None, outputs={}))

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    runner._run(rec.id)
    assert observed == ["second-key"]


def test_download_only_snapshot_does_not_require_translation_engine():
    from src.service.runner import _snapshot_engine_config

    engine_config = _snapshot_engine_config({
        "source": "disabled", "id": "deepseek", "provider": "",
    })
    assert engine_config is not None
    assert engine_config.api_type == "disabled"


def test_local_engine_remains_authoritative_over_unified_llm(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    task_store = TaskStore(db_path)
    engine_store = TranslationEngineStore(db_path)
    engine_store.ensure_local_ct2()
    model_store = ModelSettingsStore(db_path)
    _save_cards(model_store)

    runtime = resolve_runtime(
        engine="local-opus-en-zh",
        need_subtitle=True,
        request_model="local:tiny",
        source_lang="en",
        target_lang="zh-CN",
        tts_enabled=False,
        tts_voice="auto",
        engines=engine_store,
        model_store=model_store,
        compat_settings=SimpleNamespace(db_path=db_path, deepseek_api_key=None),
        translation_model_manager=_ReadyTranslationManager(),
        ensure_local_model_ready=lambda _model: None,
    )
    assert runtime.snapshot["engine"]["source"] == "translation_store"
    assert runtime.snapshot["engine"]["provider"] == "local"

    rec = task_store.create(
        url="https://example.test/local",
        source_lang="en",
        target_lang="zh-CN",
        mode="mono",
        burn="soft",
        model="local:tiny",
        engine="local-opus-en-zh",
        runtime_config_json=json.dumps(runtime.snapshot),
    )
    seen = {}

    def fake_pipeline(params, on_event, *, api_key=None, engine_config=None):
        seen["api_type"] = engine_config.api_type
        seen["api_key"] = engine_config.api_key
        on_event(PipelineEvent("SUCCESS", 100, None, outputs={}))

    monkeypatch.setattr(runner, "_store", task_store)
    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    runner._run(rec.id)

    assert seen == {"api_type": "local_ct2", "api_key": None}


def test_http_save_then_create_persists_runtime_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    task_store = TaskStore(db_path)
    engine_store = TranslationEngineStore(db_path)
    engine_store.ensure_default_deepseek()
    base_settings = dataclasses.replace(
        tasks_routes.settings,
        db_path=db_path,
        data_dir=tmp_path,
        deepseek_api_key=None,
    )
    monkeypatch.setattr(tasks_routes, "settings", base_settings)
    monkeypatch.setattr(model_settings_routes, "settings", base_settings)
    monkeypatch.setattr(deps, "settings", base_settings)
    monkeypatch.setattr(model_runtime.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(tasks_routes, "_ensure_local_model_ready", lambda _model: None)
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", lambda _task_id: None)
    app.dependency_overrides[deps.get_store] = lambda: task_store
    app.dependency_overrides[deps.get_translation_engine_store] = lambda: engine_store
    try:
        with TestClient(app) as client:
            saved = client.put("/api/settings/models", json={
                "llm": {
                    "provider": "openai_compatible",
                    "model": "http-configured",
                    "baseUrl": "https://http.example/v1",
                    "apiKey": "sk-http-secret",
                    "models": [{
                        "id": "llm-deepseek",
                        "provider": "openai_compatible",
                        "model": "http-configured",
                        "baseUrl": "https://http.example/v1",
                        "apiKey": "sk-http-secret",
                        "isCurrent": True,
                    }],
                },
                "tts": {
                    "enabled": True,
                    "provider": "local",
                    "model": "kokoro-v1",
                    "voice": "zf_001",
                    "format": "wav",
                    "models": [{
                        "id": "tts-default",
                        "provider": "local",
                        "model": "kokoro-v1",
                        "voice": "zf_001",
                        "format": "wav",
                        "isCurrent": True,
                        "enabled": True,
                    }],
                },
            })
            assert saved.status_code == 200
            assert saved.json()["llm"]["hasApiKey"] is True
            assert "sk-http-secret" not in saved.text

            created = client.post("/api/tasks", json={
                "url": "https://example.test/http-save",
                "sourceLang": "en",
                "targetLang": "zh-CN",
                "model": "local:tiny",
                "engine": "deepseek",
                "ttsEnabled": True,
                # The saved TTS card overrides this request value.
                "ttsVoice": "af_maple",
            })
            assert created.status_code == 201
            record = task_store.get(created.json()["id"])
            snapshot = json.loads(record.runtime_config_json)
            assert snapshot["engine"]["model"] == "http-configured"
            assert snapshot["tts"]["voice"] == "zf_001"
            assert "sk-http-secret" not in record.runtime_config_json
    finally:
        app.dependency_overrides.clear()


def test_http_whisper_snapshot_is_frozen_when_current_card_changes(tmp_path, monkeypatch):
    db_path = tmp_path / "whisper-state.db"
    task_store = TaskStore(db_path)
    engine_store = TranslationEngineStore(db_path)
    engine_store.ensure_default_deepseek()
    base_settings = dataclasses.replace(
        tasks_routes.settings, db_path=db_path, data_dir=tmp_path,
        deepseek_api_key="sk-test",
    )
    monkeypatch.setattr(tasks_routes, "settings", base_settings)
    monkeypatch.setattr(model_settings_routes, "settings", base_settings)
    monkeypatch.setattr(deps, "settings", base_settings)
    monkeypatch.setattr(tasks_routes, "_ensure_local_model_ready", lambda _model: None)
    monkeypatch.setattr(tasks_routes, "enqueue_pipeline", lambda _task_id: None)
    monkeypatch.setattr(model_settings_routes.model_manager, "is_ready", lambda _name: True)
    monkeypatch.setattr(model_settings_routes.model_manager, "list_models", lambda: [
        {"name": "tiny", "status": "READY"}, {"name": "small", "status": "READY"},
    ])
    app.dependency_overrides[deps.get_store] = lambda: task_store
    app.dependency_overrides[deps.get_translation_engine_store] = lambda: engine_store
    try:
        with TestClient(app) as client:
            saved = client.put("/api/settings/models", json={
                "llm": {"provider": "openai_compatible", "model": "test-model",
                        "baseUrl": "https://example.test/v1", "apiKey": "sk-test",
                        "models": [{"id": "llm-card", "provider": "openai_compatible",
                                    "model": "test-model", "baseUrl": "https://example.test/v1",
                                    "isCurrent": True}]},
                "whisper": {
                    "enabled": True, "provider": "local", "model": "tiny",
                    "models": [{"id": "tiny-card", "provider": "local", "model": "tiny",
                                "enabled": True, "isCurrent": True}],
                },
            })
            assert saved.status_code == 200
            created = client.post("/api/tasks", json=_body(ttsEnabled=False, model="local:request-model").__dict__)
            assert created.status_code == 201
            task_id = created.json()["id"]
            record = task_store.get(task_id)
            assert json.loads(record.runtime_config_json)["whisper"]["model"] == "local:tiny"
            assert "sk-test" not in record.runtime_config_json

            switched = client.put("/api/settings/models", json={"whisper": {
                "enabled": True, "provider": "local", "model": "local:small",
                "models": [
                    {"id": "tiny-card", "provider": "local", "model": "local:tiny", "enabled": True},
                    {"id": "small-card", "provider": "local", "model": "local:small", "enabled": True, "isCurrent": True},
                ],
            }})
            assert switched.status_code == 200

        observed = {}

        def fake_pipeline(params, on_event, *, api_key=None, engine_config=None):
            observed["model"] = params.model
            on_event(PipelineEvent("SUCCESS", 100, None, outputs={}))

        monkeypatch.setattr(runner, "_store", task_store)
        monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
        runner._run(task_id)
        assert observed["model"] == "local:tiny"
    finally:
        app.dependency_overrides.clear()
