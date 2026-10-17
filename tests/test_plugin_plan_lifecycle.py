"""Lifecycle tests for task-plan persistence and queue ordering."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

# Importing the public facade first follows the repository's existing module
# initialization order and avoids the handler.deps -> app -> route cycle.
import src.handler.tasks  # noqa: F401
from fastapi import HTTPException

from src.config.config import settings as runtime_settings
from src.service import model_runtime
from src.handler.schemas import to_out
from src.service.plugin_plan import ensure_plan, parse_plugin_plan
from src.service.task.task_lifecycle import create_upload_task, create_url_task, retry_task
from src.store import TaskStore, TranslationEngineStore
from test_plugin_preflight import creation_plugins  # noqa: F401

pytestmark = pytest.mark.usefixtures("creation_plugins")


def _settings(**overrides):
    values = {
        "download_format": "bv*+ba/b",
        "download_proxy": None,
        "download_socket_timeout": 30,
        "download_retries": 5,
        "download_concurrent_fragments": 4,
        "merge_output_format": "mp4",
        "cookies_file": None,
        "max_upload_mb": 10,
        "max_video_minutes": 20,
        "ffprobe_bin": "ffprobe",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _body(**overrides):
    values = {
        "url": "https://video.example/one",
        "sourceLang": "en",
        "targetLang": "zh-CN",
        "mode": "mono",
        "burn": "hard",
        "model": "local:tiny",
        "engine": "unused",
        "needSubtitle": False,
        "quality": "720p",
        "ttsEnabled": False,
        "ttsVoice": "auto",
        "originalVoiceMode": "keep",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _noop(*_args, **_kwargs):
    return None


def test_url_task_persists_plan_before_return_and_duplicate_keeps_winner(tmp_path):
    selected_plugins = {
        "download": {"plugin_id": "custom.downloader", "implementation_version": "5"}
    }
    store = TaskStore(tmp_path / "tasks.db")
    record, created = create_url_task(
        body=_body(),
        store=store,
        engines=None,
        ensure_supported_translation_languages=_noop,
        ensure_local_model_ready=lambda *_: (_ for _ in ()).throw(AssertionError("download-only must skip model checks")),
        ensure_translation_engine=_noop,
        settings=_settings(),
        plugin_defaults=selected_plugins,
    )
    assert created
    assert record.plugin_plan_json
    assert parse_plugin_plan(record.plugin_plan_json)["slots"]["download"]["plugin_id"] == "custom.downloader"

    selected_plugins["download"] = {
        "plugin_id": "replacement.downloader", "implementation_version": "6"
    }
    duplicate, created_again = create_url_task(
        body=_body(),
        store=store,
        engines=None,
        ensure_supported_translation_languages=_noop,
        ensure_local_model_ready=_noop,
        ensure_translation_engine=_noop,
        settings=_settings(),
        plugin_defaults=selected_plugins,
    )
    assert not created_again
    assert duplicate.id == record.id
    assert duplicate.plugin_plan_json == record.plugin_plan_json


def test_upload_task_persists_plan_before_enqueue(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    enqueued = []
    file = SimpleNamespace(filename="clip.mp4", file=io.BytesIO(b"video-bytes"))
    record = create_upload_task(
        file=file,
        source_lang="en",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="local:tiny",
        engine="unused",
        need_subtitle=False,
        tts_enabled=False,
        tts_voice="auto",
        original_voice_mode="keep",
        store=store,
        engines=None,
        settings=_settings(),
        task_dir=lambda task_id: tmp_path / "data" / task_id,
        probe_duration=lambda *_: 30.0,
        enqueue_pipeline=lambda task_id: enqueued.append((task_id, store.get(task_id).plugin_plan_json)),
        release_lock=_noop,
        ensure_supported_translation_languages=_noop,
        ensure_local_model_ready=lambda *_: (_ for _ in ()).throw(AssertionError("download-only must skip model checks")),
        ensure_translation_engine=_noop,
        upload_error=lambda *_args, **_kwargs: RuntimeError("unused"),
        http_exception_type=RuntimeError,
        source_video_stem="source",
        upload_video_extensions={".mp4"},
    )
    assert record.source_type == "upload"
    assert record.plugin_plan_json
    assert "download" not in parse_plugin_plan(record.plugin_plan_json)["slots"]
    assert "plugin_plan_json" not in to_out(record).model_dump()
    assert enqueued == [(record.id, record.plugin_plan_json)]


def test_retry_leaves_legacy_plan_for_runner_and_preserves_first_snapshot(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks.db")
    record = store.create(
        url="https://video.example/legacy",
        source_lang="en",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="local:tiny",
        engine="unused",
        need_subtitle=False,
    )
    store.update(record.id, status="FAILED")
    monkeypatch.setenv(
        "SUBTRANS_PIPELINE_PLUGINS",
        json.dumps({"download": {"plugin_id": "legacy.migrated", "implementation_version": "2"}}),
    )
    enqueued = []
    returned = retry_task(
        store=store,
        task_id=record.id,
        enqueue_pipeline=enqueued.append,
        to_out=lambda value: value,
    )
    assert returned.plugin_plan_json is None
    # Runner captures this flag before its first CAS migration.
    legacy_cache = returned.plugin_plan_json is None
    assert legacy_cache is True
    migrated = ensure_plan(returned, store, engines=None, settings=_settings())
    frozen_json = migrated.plugin_plan_json
    assert parse_plugin_plan(frozen_json)["slots"]["download"]["plugin_id"] == "legacy.migrated"
    assert enqueued == [record.id]

    store.update(record.id, status="FAILED")
    monkeypatch.setenv(
        "SUBTRANS_PIPELINE_PLUGINS",
        json.dumps({"download": {"plugin_id": "must.not.replace", "implementation_version": "9"}}),
    )
    retried = retry_task(
        store=store,
        task_id=record.id,
        enqueue_pipeline=lambda _task_id: None,
        to_out=lambda value: value,
    )
    assert retried.plugin_plan_json == frozen_json


class _EngineStore:
    def __init__(self, *, engine_id="cloud", api_type="openai_compatible", enabled=1):
        self.record = SimpleNamespace(
            id=engine_id,
            api_type=api_type,
            enabled=enabled,
            base_url="https://engine.example/v1" if api_type != "local_ct2" else "",
            model="engine-model",
            api_key="test-key" if api_type != "local_ct2" else None,
        )

    def get(self, engine_id):
        return self.record if engine_id == self.record.id else None


def _plugin_map(**stages):
    return json.dumps({
        stage: {"plugin_id": plugin_id, "implementation_version": "1"}
        for stage, plugin_id in stages.items()
    })


def test_custom_transcriber_does_not_require_local_whisper_model(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    calls = []
    record, created = create_url_task(
        body=_body(needSubtitle=True, engine="cloud", model="local:tiny"),
        store=store,
        engines=_EngineStore(),
        ensure_supported_translation_languages=lambda *args: calls.append("language"),
        ensure_local_model_ready=lambda *args: (_ for _ in ()).throw(
            AssertionError("custom transcriber must validate its own dependencies")
        ),
        ensure_translation_engine=lambda *args: calls.append("translation-ready"),
        settings=_settings(),
        plugin_defaults=json.loads(_plugin_map(transcribe="custom.asr")),
    )

    assert created
    assert calls == ["language", "translation-ready"]
    assert parse_plugin_plan(record.plugin_plan_json)["slots"]["transcribe"]["plugin_id"] == "custom.asr"


def test_url_creation_uses_one_config_revision_for_runtime_and_plugin_plan(
    tmp_path, memory_config_provider
):
    engine_store = TranslationEngineStore(provider=memory_config_provider)
    engine = engine_store.create(
        name="Cloud", api_type="openai_compatible",
        base_url="https://old.example/v1", model="old-model", api_key="test-key",
    )
    engine_store.update(engine.id, availability="AVAILABLE")
    memory_config_provider.mutate(
        lambda document: document["business"].update({"download_retries": 13})
    )
    pinned_revision = memory_config_provider.read().revision
    store = TaskStore(tmp_path / "tasks.db")

    def resolve_then_change_config(**kwargs):
        assert kwargs["config_snapshot"].revision == pinned_revision
        runtime = model_runtime.resolve_runtime(**kwargs)

        def change(document):
            document["business"]["download_retries"] = 99
            current = document["engines"]["byId"][engine.id]
            current["base_url"] = "https://new.example/v1"
            current["model"] = "new-model"

        memory_config_provider.mutate(change)
        return runtime

    record, created = create_url_task(
        body=_body(engine=engine.id, needSubtitle=True),
        store=store,
        engines=engine_store,
        ensure_supported_translation_languages=_noop,
        ensure_local_model_ready=_noop,
        ensure_translation_engine=lambda *_: (_ for _ in ()).throw(
            AssertionError("runtime resolution already validated the pinned engine")
        ),
        resolve_runtime=resolve_then_change_config,
        settings=runtime_settings,
    )

    assert created
    runtime_snapshot = json.loads(record.runtime_config_json)
    assert runtime_snapshot["configRevision"] == pinned_revision
    assert runtime_snapshot["engine"]["model"] == "old-model"
    plan = parse_plugin_plan(record.plugin_plan_json)
    assert plan["slots"]["download"]["config"]["retries"] == 13
    translate_config = plan["slots"]["translate"]["config"]
    assert translate_config["model"] == "old-model"
    assert translate_config["base_url"] == "https://old.example/v1"


def test_builtin_whisper_readiness_error_keeps_url_409_and_creates_nothing(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")

    def not_ready(model):
        assert model == "local:tiny"
        raise HTTPException(
            status_code=409,
            detail={"code": "MODEL_NOT_READY", "message": "model is not ready"},
        )

    with pytest.raises(HTTPException) as raised:
        create_url_task(
            body=_body(needSubtitle=True, engine="cloud", model="local:tiny"),
            store=store,
            engines=_EngineStore(),
            ensure_supported_translation_languages=_noop,
            ensure_local_model_ready=not_ready,
            ensure_translation_engine=_noop,
            settings=_settings(),
        )
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "MODEL_NOT_READY"
    assert store.list() == []


def test_builtin_whisper_readiness_error_keeps_upload_error_and_does_not_enqueue(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    enqueued = []

    def not_ready(_model):
        assert _model == "local:tiny"
        raise HTTPException(
            status_code=409,
            detail={"code": "MODEL_NOT_READY", "message": "model is not ready"},
        )

    with pytest.raises(HTTPException) as raised:
        create_upload_task(
            file=SimpleNamespace(filename="clip.mp4", file=io.BytesIO(b"video")),
            source_lang="en", target_lang="zh-CN", mode="mono", burn="soft",
            model="local:tiny", engine="cloud", need_subtitle=True,
            tts_enabled=False, tts_voice="auto", original_voice_mode="keep",
            store=store, engines=_EngineStore(), settings=_settings(max_upload_mb=10),
            task_dir=lambda task_id: tmp_path / "data" / task_id,
            probe_duration=lambda *_: 10.0,
            enqueue_pipeline=lambda task_id: enqueued.append(task_id),
            release_lock=_noop,
            ensure_supported_translation_languages=_noop,
            ensure_local_model_ready=not_ready,
            ensure_translation_engine=_noop,
            upload_error=lambda *_args, **_kwargs: RuntimeError("unexpected"),
            http_exception_type=HTTPException,
            source_video_stem="source", upload_video_extensions={".mp4"},
        )
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "MODEL_NOT_READY"
    assert store.list() == []
    assert enqueued == []


def test_builtin_http_whisper_skips_local_model_readiness_on_upload(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    enqueued = []
    record = create_upload_task(
        file=SimpleNamespace(filename="http-asr.mp4", file=io.BytesIO(b"video")),
        source_lang="en", target_lang="zh-CN", mode="mono", burn="soft",
        model="tiny", engine="cloud", need_subtitle=True,
        tts_enabled=False, tts_voice="auto", original_voice_mode="keep",
        store=store, engines=_EngineStore(),
        settings=_settings(
            max_upload_mb=10, transcriber_backend="http",
            transcriber_url="https://asr.example.test/v1",
        ),
        task_dir=lambda task_id: tmp_path / "data" / task_id,
        probe_duration=lambda *_: 10.0,
        enqueue_pipeline=enqueued.append,
        release_lock=_noop,
        ensure_supported_translation_languages=_noop,
        ensure_local_model_ready=lambda *_: (_ for _ in ()).throw(
            AssertionError("HTTP transcriber must not require local Whisper model")
        ),
        ensure_translation_engine=_noop,
        upload_error=lambda *_args, **_kwargs: RuntimeError("unexpected"),
        http_exception_type=HTTPException,
        source_video_stem="source", upload_video_extensions={".mp4"},
    )
    plan = parse_plugin_plan(record.plugin_plan_json)
    assert plan["slots"]["transcribe"]["plugin_id"] == "builtin.whisper"
    assert plan["slots"]["transcribe"]["config"]["backend"] == "http"
    assert enqueued == [record.id]


def test_custom_transcriber_skips_local_model_readiness_on_upload(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    enqueued = []
    record = create_upload_task(
        file=SimpleNamespace(filename="custom-asr.mp4", file=io.BytesIO(b"video")),
        source_lang="en", target_lang="zh-CN", mode="mono", burn="soft",
        model="local:tiny", engine="cloud", need_subtitle=True,
        tts_enabled=False, tts_voice="auto", original_voice_mode="keep",
        store=store, engines=_EngineStore(), settings=_settings(max_upload_mb=10),
        task_dir=lambda task_id: tmp_path / "data" / task_id,
        probe_duration=lambda *_: 10.0,
        enqueue_pipeline=enqueued.append,
        release_lock=_noop,
        ensure_supported_translation_languages=_noop,
        ensure_local_model_ready=lambda *_: (_ for _ in ()).throw(
            AssertionError("custom transcriber must own model readiness")
        ),
        ensure_translation_engine=_noop,
        upload_error=lambda *_args, **_kwargs: RuntimeError("unexpected"),
        http_exception_type=HTTPException,
        source_video_stem="source", upload_video_extensions={".mp4"},
        plugin_defaults=json.loads(_plugin_map(transcribe="custom.asr")),
    )
    assert parse_plugin_plan(record.plugin_plan_json)["slots"]["transcribe"]["plugin_id"] == "custom.asr"
    assert enqueued == [record.id]


def test_custom_translator_skips_builtin_local_language_readiness(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    record, created = create_url_task(
        body=_body(
            needSubtitle=True, engine="local-engine", sourceLang="ja", targetLang="en"
        ),
        store=store,
        engines=_EngineStore(engine_id="local-engine", api_type="local_ct2"),
        ensure_supported_translation_languages=lambda *_: (_ for _ in ()).throw(
            AssertionError("custom translator owns language support")
        ),
        ensure_local_model_ready=lambda *_: (_ for _ in ()).throw(
            AssertionError("custom transcriber owns dependency readiness")
        ),
        ensure_translation_engine=lambda *_: (_ for _ in ()).throw(
            AssertionError("custom translator owns local engine readiness")
        ),
        settings=_settings(),
        plugin_defaults=json.loads(_plugin_map(transcribe="custom.asr", translate="custom.translate")),
    )
    assert created
    plan = parse_plugin_plan(record.plugin_plan_json)
    assert plan["slots"]["translate"]["plugin_id"] == "custom.translate"


@pytest.mark.parametrize("engine_store", [None, _EngineStore(enabled=0)])
def test_custom_translator_does_not_require_existing_enabled_engine_record(
    tmp_path, engine_store
):
    store = TaskStore(tmp_path / "tasks.db")
    record, created = create_url_task(
        body=_body(needSubtitle=True, engine="cloud"),
        store=store,
        engines=engine_store,
        ensure_supported_translation_languages=_noop,
        ensure_local_model_ready=_noop,
        ensure_translation_engine=lambda *_: pytest.fail("custom translator used builtin engine"),
        settings=_settings(),
        plugin_defaults=json.loads(_plugin_map(translate="custom.translate")),
    )
    assert created
    assert parse_plugin_plan(record.plugin_plan_json)["slots"]["translate"]["plugin_id"] == "custom.translate"


def test_builtin_local_ct2_language_error_keeps_422_and_creates_nothing(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    with pytest.raises(HTTPException) as raised:
        create_url_task(
            body=_body(needSubtitle=True, engine="local-engine", sourceLang="ja", targetLang="en"),
            store=store,
            engines=_EngineStore(engine_id="local-engine", api_type="local_ct2"),
            ensure_supported_translation_languages=_noop,
            ensure_local_model_ready=_noop,
            ensure_translation_engine=_noop,
            settings=_settings(),
        )
    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "UNSUPPORTED_TRANSLATION_LANGUAGE"
    assert store.list() == []


def test_creation_freezes_plugin_defaults_before_real_runtime_resolution(tmp_path, monkeypatch):
    from test_plugin_preflight import _plugins

    _plugins(monkeypatch, transcribe="custom.asr")
    store = TaskStore(tmp_path / "frozen-plugins.db")
    engines = TranslationEngineStore()
    engine = engines.create(name="Cloud", api_type="openai_compatible", model="fixed",
                            base_url="https://translate.example/v1", api_key="test-key")
    engines.update(engine.id, availability="AVAILABLE")

    def resolve_then_replace_defaults(**kwargs):
        runtime = model_runtime.resolve_runtime(**kwargs)
        _plugins(monkeypatch, transcribe="unregistered.new-asr")
        return runtime

    record, created = create_url_task(
        body=_body(needSubtitle=True, engine=engine.id), store=store, engines=engines,
        ensure_supported_translation_languages=_noop,
        ensure_local_model_ready=lambda *_: pytest.fail("custom ASR used builtin model"),
        ensure_translation_engine=_noop, resolve_runtime=resolve_then_replace_defaults,
        settings=runtime_settings,
    )
    assert created
    assert parse_plugin_plan(record.plugin_plan_json)["slots"]["transcribe"]["plugin_id"] == "custom.asr"


@pytest.mark.parametrize("step", ["reburn:claim", "subtitle_edit:claim"])
def test_cancel_refuses_active_subtitle_operation_before_pipeline_cleanup(tmp_path, step):
    from src.service.task.task_lifecycle import LifecycleConflictError, cancel_task

    store = TaskStore(tmp_path / "operations.db")
    record = store.create(url="https://video.example/operation", source_lang="en",
                          target_lang="zh-CN", mode="mono", burn="soft",
                          model="local:tiny", engine="unused")
    store.update(record.id, status="BURNING", current_step=step)
    with pytest.raises(LifecycleConflictError, match="字幕操作进行中"):
        cancel_task(store=store, task_id=record.id,
                    cancel_pipeline=lambda *_: pytest.fail("operation cancelled pipeline"),
                    to_out=lambda value: value)
    assert store.get(record.id).status == "BURNING"


def test_retry_losing_conditional_write_does_not_enqueue(tmp_path, monkeypatch):
    from src.service.task.task_lifecycle import LifecycleConflictError

    store = TaskStore(tmp_path / "retry-cas.db")
    record = store.create(url="https://video.example/retry", source_lang="en",
                          target_lang="zh-CN", mode="mono", burn="soft",
                          model="local:tiny", engine="unused")
    store.update(record.id, status="FAILED")
    conditional_update = store.update_if_status

    def claim_before_update(task_id, expected_statuses, **fields):
        store.update(task_id, status="BURNING", current_step="reburn:other-worker")
        return conditional_update(task_id, expected_statuses, **fields)

    monkeypatch.setattr(store, "update_if_status", claim_before_update)
    with pytest.raises(LifecycleConflictError, match="任务状态已变化"):
        retry_task(store=store, task_id=record.id,
                   enqueue_pipeline=lambda *_: pytest.fail("CAS loser enqueued task"),
                   to_out=lambda value: value)
    assert store.get(record.id).current_step == "reburn:other-worker"


def test_delete_losing_conditional_write_keeps_existing_files(tmp_path, monkeypatch):
    from src.service.task.task_lifecycle import LifecycleConflictError, delete_task

    store = TaskStore(tmp_path / "delete-cas.db")
    record = store.create(url="https://video.example/delete", source_lang="en",
                          target_lang="zh-CN", mode="mono", burn="soft",
                          model="local:tiny", engine="unused")
    store.update(record.id, status="SUCCESS")
    directory = tmp_path / record.id
    directory.mkdir()
    output = directory / "output_soft.mp4"
    output.write_bytes(b"previous-video")
    conditional_delete = store.delete_if_status

    def claim_before_delete(task_id, expected_statuses):
        store.update(task_id, status="BURNING", current_step="reburn:other-worker")
        return conditional_delete(task_id, expected_statuses)

    monkeypatch.setattr(store, "delete_if_status", claim_before_delete)
    with pytest.raises(LifecycleConflictError, match="任务状态已变化"):
        delete_task(store=store, task_id=record.id, task_dir=lambda _id: directory,
                    cleanup_partial_artifacts=lambda *_: pytest.fail("CAS loser cleaned files"),
                    release_lock=lambda *_: pytest.fail("CAS loser released editor lock"))
    assert output.read_bytes() == b"previous-video"
    assert store.get(record.id).current_step == "reburn:other-worker"
