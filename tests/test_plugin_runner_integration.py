"""Persistent plugin-plan handoff across creation, runner, retry and MCP REST."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from src.handler import tasks as task_compat
from src.mcp_server.business_client import BusinessApiClient, BusinessApiConfig
from src.config.config_repository import get_config_provider
from src.service import runner
from src.service.orchestrator import PipelineEvent
from src.service.plugin_plan import parse_plugin_plan
from src.service.task import task_lifecycle
from src.store import TaskStore, TranslationEngineStore


@pytest.fixture
def stores(tmp_path, monkeypatch):
    task_store = TaskStore(tmp_path / "tasks.sqlite3")
    engine_store = TranslationEngineStore(tmp_path / "tasks.sqlite3")
    engine = engine_store.create(
        name="Integration engine",
        api_type="openai_compatible",
        base_url="https://old.example/v1",
        model="old-model",
        api_key="key-v1",
    )
    monkeypatch.setattr(runner, "_store", task_store)
    monkeypatch.setattr(runner, "_engine_store", engine_store)
    return task_store, engine_store, engine


def _body(engine_id: str, *, need_subtitle: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        url="https://video.example/watch?v=plugin-plan-test",
        sourceLang="en",
        targetLang="zh-CN",
        mode="mono",
        burn="soft",
        model="local:tiny",
        engine=engine_id,
        needSubtitle=need_subtitle,
        quality="720p",
        ttsEnabled=False,
        ttsVoice="auto",
        originalVoiceMode="keep",
    )


def _create_url_task(task_store, engine_store, engine_id, *, need_subtitle=True, settings=None):
    return task_lifecycle.create_url_task(
        body=_body(engine_id, need_subtitle=need_subtitle),
        store=task_store,
        engines=engine_store,
        ensure_supported_translation_languages=lambda *args: None,
        ensure_local_model_ready=lambda *args: None,
        ensure_translation_engine=lambda *args: None,
        settings=settings,
    )


def _run_with_capture(monkeypatch, task_id, callback):
    def fake_pipeline(params, on_event, **kwargs):
        callback(params, kwargs)
        on_event(PipelineEvent("SUCCESS", 100, None, outputs={}))

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    runner._run(task_id)


def test_created_plan_snapshots_engine_and_default_plugin_identity(stores, monkeypatch):
    task_store, engine_store, engine = stores
    monkeypatch.setenv(
        "SUBTRANS_PIPELINE_PLUGINS",
        json.dumps({
            "download": {"plugin_id": "backend.ytdlp-v2", "implementation_version": "2"},
            "translate": {"plugin_id": "backend.translator-v4", "implementation_version": "4"},
        }),
    )
    record, created = _create_url_task(task_store, engine_store, engine.id)
    assert created
    saved_plan = record.plugin_plan_json
    saved = parse_plugin_plan(saved_plan)
    assert saved["slots"]["download"]["plugin_id"] == "backend.ytdlp-v2"
    assert saved["slots"]["translate"]["plugin_id"] == "backend.translator-v4"
    assert saved["slots"]["translate"]["config"]["base_url"] == "https://old.example/v1"
    assert saved["slots"]["translate"]["config"]["model"] == "old-model"

    monkeypatch.setenv(
        "SUBTRANS_PIPELINE_PLUGINS",
        json.dumps({
            "download": {"plugin_id": "backend.ytdlp-v3", "implementation_version": "3"},
            "translate": {"plugin_id": "backend.translator-v5", "implementation_version": "5"},
        }),
    )
    engine_store.update(engine.id, base_url="https://new.example/v2", model="new-model", api_key="key-v2")
    captured = {}

    def inspect(params, kwargs):
        captured.update(kwargs)
        captured["plan"] = kwargs["plugin_plan"]
        captured["key"] = kwargs["credential_resolver"](
            f"translation_engine:{engine.id}:api_key"
        )

    _run_with_capture(monkeypatch, record.id, inspect)
    persisted = task_store.get(record.id)
    assert persisted.plugin_plan_json == saved_plan
    assert captured["plan"]["slots"]["download"]["plugin_id"] == "backend.ytdlp-v2"
    assert captured["plan"]["slots"]["translate"]["plugin_id"] == "backend.translator-v4"
    assert captured["plan"]["slots"]["translate"]["config"]["base_url"] == "https://old.example/v1"
    assert captured["plan"]["slots"]["translate"]["config"]["model"] == "old-model"
    assert captured["key"] == "key-v2"


@pytest.mark.parametrize("delete_key", [False, True])
def test_credential_resolver_uses_fixed_engine_id_and_current_key(stores, monkeypatch, delete_key):
    task_store, engine_store, engine = stores
    record, _ = _create_url_task(task_store, engine_store, engine.id)
    plan = parse_plugin_plan(record.plugin_plan_json)
    assert plan["slots"]["translate"]["config"]["engine_id"] == engine.id
    if delete_key:
        assert engine_store.delete(engine.id)
    else:
        engine_store.update(engine.id, api_key="key-rotated")
    result = {}

    def inspect(params, kwargs):
        resolver = kwargs["credential_resolver"]
        if delete_key:
            with pytest.raises(Exception):
                resolver(f"translation_engine:{engine.id}:api_key")
            result["missing"] = True
        else:
            result["key"] = resolver(f"translation_engine:{engine.id}:api_key")

    _run_with_capture(monkeypatch, record.id, inspect)
    assert result == ({"missing": True} if delete_key else {"key": "key-rotated"})


def test_v2_snapshot_materializes_missing_plan_from_frozen_routes(stores, monkeypatch):
    task_store, _engine_store, _engine = stores
    provider = get_config_provider()

    def save_llm(config):
        config["models"]["llm"] = {
            "enabled": True,
            "provider": "deepseek",
            "model": "model-A",
            "baseUrl": "https://model-a.example/v1",
            "apiKey": "key-at-create",
            "_runtimeManaged": True,
            "models": [{
                "id": "card-a", "name": "Model A", "provider": "deepseek",
                "model": "model-A", "baseUrl": "https://model-a.example/v1",
                "enabled": True, "isCurrent": True, "apiKey": "key-at-create",
            }],
        }

    provider.mutate(save_llm)
    created_config = provider.read()
    runtime_snapshot = {
        "schemaVersion": 2,
        "configRevision": created_config.revision,
        "business": runner.settings.capture_business_snapshot(config_snapshot=created_config),
        "engine": {
            "source": "unified_llm", "id": "deepseek", "cardId": "card-a",
            "provider": "deepseek", "model": "model-A",
            "baseUrl": "https://model-a.example/v1",
            "params": {"temperature": 0.3, "maxTokens": 4096, "timeout": 120, "concurrency": 1},
        },
        "whisper": {
            "source": "request", "cardId": None, "provider": "local", "model": "local:tiny",
        },
        "tts": {
            "enabled": False, "source": "request", "cardId": None, "provider": "",
            "model": "", "voice": "auto", "speed": 1.0, "format": "wav",
        },
    }
    record = task_store.create(
        url="https://video.example/watch?v=v2-null-plan",
        source_lang="en", target_lang="zh-CN", mode="mono", burn="soft",
        model="local:tiny", engine="deepseek", plugin_plan_json=None,
        runtime_config_json=json.dumps(runtime_snapshot),
    )

    def rotate_llm(config):
        card = config["models"]["llm"]["models"][0]
        card.update(model="model-B", baseUrl="https://model-b.example/v2", apiKey="key-current")
        config["models"]["llm"].update(
            model="model-B", baseUrl="https://model-b.example/v2", apiKey="key-current"
        )

    provider.mutate(rotate_llm)
    captured = {}

    def inspect(_params, kwargs):
        translate = kwargs["plugin_plan"]["slots"]["translate"]["config"]
        captured["model"] = translate["model"]
        captured["base_url"] = translate["base_url"]
        captured["key"] = kwargs["credential_resolver"](
            "translation_engine:deepseek:api_key"
        )

    _run_with_capture(monkeypatch, record.id, inspect)
    persisted = task_store.get(record.id)
    assert persisted.plugin_plan_json is not None
    assert captured == {
        "model": "model-A",
        "base_url": "https://model-a.example/v1",
        "key": "key-current",
    }


def test_null_snapshot_with_existing_plan_keeps_engine_key_source_on_retry(stores, monkeypatch):
    task_store, _engine_store, engine = stores
    record, _ = _create_url_task(task_store, _engine_store, engine.id)
    saved_plan = record.plugin_plan_json
    provider = get_config_provider()

    def save_routes(config):
        config["engines"]["byId"][engine.id] = {
            "id": engine.id, "name": engine.name, "api_type": engine.api_type,
            "base_url": engine.base_url, "model": engine.model,
            "api_key": "legacyengineKey", "enabled": 1,
            "availability": "AVAILABLE", "last_checked_at": None,
            "last_error": None, "api_key_rotated_at": None,
            "created_at": 1, "updated_at": 1,
        }
        config["models"]["llm"] = {
            "enabled": True, "provider": "deepseek", "model": "unified-model",
            "baseUrl": "https://unified.example/v1", "apiKey": "unified-card-key",
            "_runtimeManaged": True,
            "models": [{
                "id": "unified-card", "name": "Unified", "provider": "deepseek",
                "model": "unified-model", "baseUrl": "https://unified.example/v1",
                "enabled": True, "isCurrent": True, "apiKey": "unified-card-key",
            }],
        }

    provider.mutate(save_routes)
    task_store.update(record.id, runtime_config_json=None)
    observed_keys = []

    def inspect(_params, kwargs):
        observed_keys.append(kwargs["credential_resolver"](
            f"translation_engine:{engine.id}:api_key"
        ))

    _run_with_capture(monkeypatch, record.id, inspect)
    persisted_snapshot = json.loads(task_store.get(record.id).runtime_config_json)
    assert persisted_snapshot["engine"]["source"] == "translation_store"
    assert persisted_snapshot["engine"]["id"] == engine.id
    assert task_store.get(record.id).plugin_plan_json == saved_plan

    task_store.update(record.id, status="FAILED", error="retry test")
    queued = []
    task_lifecycle.retry_task(
        store=task_store, task_id=record.id,
        enqueue_pipeline=lambda task_id: queued.append(task_id),
        to_out=lambda item: item,
    )
    assert queued == [record.id]
    _run_with_capture(monkeypatch, record.id, inspect)
    assert observed_keys == ["legacyengineKey", "legacyengineKey"]
    assert task_store.get(record.id).plugin_plan_json == saved_plan


def test_only_download_does_not_resolve_engine_or_create_translation_slot(stores, monkeypatch):
    task_store, engine_store, engine = stores
    record, _ = _create_url_task(
        task_store, engine_store, engine.id, need_subtitle=False
    )

    class NoEngineReads:
        def get(self, engine_id):
            raise AssertionError("download-only execution must not access translation engine store")

    monkeypatch.setattr(runner, "_engine_store", NoEngineReads())
    seen = {}

    def inspect(params, kwargs):
        plan = kwargs["plugin_plan"]
        assert "translate" not in plan["slots"]
        assert "transcribe" not in plan["slots"]
        assert "render.subtitle" not in plan["slots"]
        seen["plan"] = plan

    _run_with_capture(monkeypatch, record.id, inspect)
    assert "download" in seen["plan"]["slots"]


def test_legacy_null_plan_migrates_once_and_retry_preserves_snapshot(stores, monkeypatch):
    task_store, engine_store, engine = stores
    record = task_store.create(
        url="https://video.example/watch?v=legacy-plan",
        source_lang="en", target_lang="zh-CN", mode="mono", burn="soft",
        model="local:tiny", engine=engine.id, plugin_plan_json=None,
    )
    redis_engine = engine.__dict__.copy()
    redis_engine["availability"] = "AVAILABLE"
    get_config_provider().mutate(
        lambda document: document["engines"]["byId"].update({engine.id: redis_engine})
    )
    monkeypatch.setenv(
        "SUBTRANS_PIPELINE_PLUGINS",
        json.dumps({"translate": {"plugin_id": "legacy.migration-v1", "implementation_version": "1"}}),
    )
    legacy_flags = []

    def capture(params, kwargs):
        legacy_flags.append(kwargs["legacy_cache"])

    _run_with_capture(monkeypatch, record.id, capture)
    migrated = task_store.get(record.id).plugin_plan_json
    assert migrated is not None
    assert parse_plugin_plan(migrated)["slots"]["translate"]["plugin_id"] == "legacy.migration-v1"

    task_store.update(record.id, status="FAILED", error="retry test")
    queued = []
    task_lifecycle.retry_task(
        store=task_store, task_id=record.id,
        enqueue_pipeline=lambda task_id: queued.append(task_id),
        to_out=lambda item: item,
    )
    assert queued == [record.id]
    assert task_store.get(record.id).plugin_plan_json == migrated
    monkeypatch.setenv(
        "SUBTRANS_PIPELINE_PLUGINS",
        json.dumps({"translate": {"plugin_id": "new.default-v2", "implementation_version": "2"}}),
    )
    _run_with_capture(monkeypatch, record.id, capture)
    assert legacy_flags == [True, False]
    assert task_store.get(record.id).plugin_plan_json == migrated


def test_late_success_after_cancel_signal_cannot_replace_cancelled_status(stores, monkeypatch):
    task_store, _engine_store, engine = stores
    record = task_store.create(
        url="https://video.example/watch?v=cancel-race",
        source_lang="en", target_lang="zh-CN", mode="mono", burn="soft",
        model="local:tiny", engine=engine.id, need_subtitle=False,
    )
    event_ready = threading.Event()
    release_success = threading.Event()

    def fake_pipeline(params, on_event, **_kwargs):
        def delayed_success():
            event_ready.set()
            assert release_success.wait(timeout=5)
            on_event(PipelineEvent("SUCCESS", 100, None, outputs={}))

        event_thread = threading.Thread(target=delayed_success)
        event_thread.start()
        assert event_ready.wait(timeout=5)
        runner.set_cancelled_signal(record.id)
        task_store.update(record.id, status="CANCELLED", error="user cancelled")
        release_success.set()
        event_thread.join(timeout=5)
        assert not event_thread.is_alive()

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    runner._run(record.id)

    assert task_store.get(record.id).status == "CANCELLED"


def test_mcp_business_client_posts_to_same_rest_create_route_and_hides_plan(
    stores, monkeypatch
):
    task_store, engine_store, engine = stores
    engine_store.update(engine.id, availability="AVAILABLE")
    app = FastAPI()
    app.include_router(task_lifecycle.router)
    app.dependency_overrides[task_lifecycle.get_store] = lambda: task_store
    app.dependency_overrides[task_lifecycle.get_translation_engine_store] = lambda: engine_store
    app.dependency_overrides[task_lifecycle.require_api_token] = lambda: None
    monkeypatch.setattr(task_compat, "enqueue_pipeline", lambda task_id: None)
    monkeypatch.setattr(task_compat, "_ensure_supported_translation_languages", lambda *args: None)
    monkeypatch.setattr(task_compat, "_ensure_local_model_ready", lambda *args: None)
    monkeypatch.setattr(task_compat, "_ensure_translation_engine", lambda *args: None)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        api = BusinessApiClient(
            BusinessApiConfig(base_url="http://business.test"), transport=transport
        )
        response = await api.create_task({
            "url": "https://video.example/watch?v=mcp-plugin-plan",
            "sourceLang": "en", "targetLang": "zh-CN", "mode": "mono",
            "burn": "soft", "model": "local:tiny", "engine": engine.id,
            "needSubtitle": True,
        })
        return response

    response = asyncio.run(scenario())
    record = task_store.get(response["id"])
    assert record is not None and record.task_origin == "mcp"
    assert record.plugin_plan_json
    assert "plugin_plan_json" not in response and "pluginPlanJson" not in response
