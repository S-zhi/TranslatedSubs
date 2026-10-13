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

from src.handler.schemas import to_out
from src.service.plugin_plan import ensure_plan, parse_plugin_plan
from src.service.task.task_lifecycle import create_upload_task, create_url_task, retry_task
from src.store import TaskStore


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


def test_url_task_persists_plan_before_return_and_duplicate_keeps_winner(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SUBTRANS_PIPELINE_PLUGINS",
        json.dumps({"download": {"plugin_id": "custom.downloader", "implementation_version": "5"}}),
    )
    store = TaskStore(tmp_path / "tasks.db")
    record, created = create_url_task(
        body=_body(),
        store=store,
        engines=None,
        ensure_supported_translation_languages=_noop,
        ensure_local_model_ready=lambda *_: (_ for _ in ()).throw(AssertionError("download-only must skip model checks")),
        ensure_translation_engine=_noop,
        settings=_settings(),
    )
    assert created
    assert record.plugin_plan_json
    assert parse_plugin_plan(record.plugin_plan_json)["slots"]["download"]["plugin_id"] == "custom.downloader"

    monkeypatch.setenv(
        "SUBTRANS_PIPELINE_PLUGINS",
        json.dumps({"download": {"plugin_id": "replacement.downloader", "implementation_version": "6"}}),
    )
    duplicate, created_again = create_url_task(
        body=_body(),
        store=store,
        engines=None,
        ensure_supported_translation_languages=_noop,
        ensure_local_model_ready=_noop,
        ensure_translation_engine=_noop,
        settings=_settings(),
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


def test_custom_transcriber_does_not_require_local_whisper_model(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBTRANS_PIPELINE_PLUGINS", _plugin_map(transcribe="custom.asr"))
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
    )

    assert created
    assert calls == ["language", "translation-ready"]
    assert parse_plugin_plan(record.plugin_plan_json)["slots"]["transcribe"]["plugin_id"] == "custom.asr"


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


def test_custom_transcriber_skips_local_model_readiness_on_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBTRANS_PIPELINE_PLUGINS", _plugin_map(transcribe="custom.asr"))
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
    )
    assert parse_plugin_plan(record.plugin_plan_json)["slots"]["transcribe"]["plugin_id"] == "custom.asr"
    assert enqueued == [record.id]


def test_custom_translator_skips_builtin_local_language_readiness(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SUBTRANS_PIPELINE_PLUGINS",
        _plugin_map(transcribe="custom.asr", translate="custom.translate"),
    )
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
    )
    assert created
    plan = parse_plugin_plan(record.plugin_plan_json)
    assert plan["slots"]["translate"]["plugin_id"] == "custom.translate"


@pytest.mark.parametrize("engine_store", [None, _EngineStore(enabled=0)])
def test_custom_translator_still_requires_existing_enabled_engine_record(
    tmp_path, monkeypatch, engine_store
):
    monkeypatch.setenv("SUBTRANS_PIPELINE_PLUGINS", _plugin_map(translate="custom.translate"))
    store = TaskStore(tmp_path / "tasks.db")
    with pytest.raises(HTTPException) as raised:
        create_url_task(
            body=_body(needSubtitle=True, engine="cloud"),
            store=store,
            engines=engine_store,
            ensure_supported_translation_languages=_noop,
            ensure_local_model_ready=_noop,
            ensure_translation_engine=_noop,
            settings=_settings(),
        )
    assert raised.value.status_code == 422
    assert "翻译引擎" in str(raised.value.detail)
    assert store.list() == []


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
