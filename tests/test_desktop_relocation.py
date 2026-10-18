"""Relocated bundle execution with real SQLite/snapshots, no media or downloads."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from src.config import settings
from src.config.config_repository import set_config_provider
from src.config.sqlite_config import SQLiteConfigProvider
from src.desktop.lifecycle import recover_desktop_tasks
from src.handler import tasks as tasks_routes
from src.service import runner
from src.service.model_runtime import resolve_runtime
from src.service.orchestrator import PipelineEvent
from src.store import TaskStore, TranslationEngineStore


def _bundle(tmp_path, name):
    directory = tmp_path / name / "TranslateSub.app" / "Contents" / "Resources" / "bin"
    directory.mkdir(parents=True)
    paths = {}
    for tool in ("ffmpeg", "ffprobe"):
        path = directory / tool
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        paths[tool + "_bin"] = str(path)
    return paths


def _launch(monkeypatch, paths, db):
    monkeypatch.setenv("SUBTRANS_DESKTOP", "1")
    for tool in ("ffmpeg", "ffprobe"):
        monkeypatch.setenv("SUBTRANS_DESKTOP_" + tool.upper(), paths[tool + "_bin"])
    provider = SQLiteConfigProvider(db)
    provider.initialize()
    set_config_provider(provider)
    return provider


@pytest.mark.parametrize("existing_plan", [False, True], ids=["recovery-plan", "saved-plan"])
@pytest.mark.parametrize("desktop", [True, False], ids=["desktop-relocation", "server-frozen"])
def test_relocation_scopes_preserve_routing_and_stored_snapshots(tmp_path, monkeypatch, existing_plan, desktop):
    old = _bundle(tmp_path, "A")
    current = _bundle(tmp_path, "B")
    provider = _launch(monkeypatch, old, tmp_path / "config.db")
    engines = TranslationEngineStore()
    engines.ensure_local_ct2()
    runtime = resolve_runtime(
        engine="local-opus-en-zh", need_subtitle=True, request_model="local:base.en",
        source_lang="en", target_lang="zh-CN", tts_enabled=False, tts_voice="auto",
        engines=engines, ensure_local_model_ready=lambda _: None,
        translation_model_manager=SimpleNamespace(is_ready=lambda: True),
    )
    frozen = copy.deepcopy(runtime.snapshot)
    encoded = json.dumps(frozen)
    store = TaskStore(tmp_path / "tasks.db")
    record = store.create(
        url="https://example.test/video", source_lang="en", target_lang="zh-CN",
        mode="bilingual", burn="hard", model="local:base.en", engine="local-opus-en-zh",
        runtime_config_json=encoded,
    )
    if existing_plan:
        record = runner.ensure_plan(record, store, engines, settings, runtime_snapshot=frozen)
    saved_plan = record.plugin_plan_json
    store.update(record.id, status="TRANSCRIBING")
    assert recover_desktop_tasks(store) == [record.id]
    queued = []
    tasks_routes._task_lifecycle.retry_task(
        store=store, task_id=record.id, enqueue_pipeline=queued.append, to_out=lambda row: row,
    )
    assert queued == [record.id]

    # A real startup refresh of the same SQLite document after moving the app.
    provider = _launch(monkeypatch, current, provider.db_path)
    for path in old.values():
        from pathlib import Path
        Path(path).unlink()
    provider.mutate(lambda doc: doc["business"].update({
        "download_quality": "360p", "local_whisper_beam_size": 2,
    }))
    provider.mutate(lambda doc: doc["models"]["whisper"].update(model="local:tiny"))
    before_config = copy.deepcopy(provider.read().values)
    # Execution cannot bypass startup validation by rereading arbitrary env.
    for tool in ("FFMPEG", "FFPROBE"):
        monkeypatch.setenv("SUBTRANS_DESKTOP_" + tool, "/unvalidated/" + tool.lower())
        monkeypatch.setenv(tool + "_BIN", "/external/" + tool.lower())
    if not desktop:
        monkeypatch.delenv("SUBTRANS_DESKTOP")

    observed = []
    original_ensure = runner.ensure_plan

    def observe_settings():
        return {key: getattr(settings, key) for key in frozen["business"]}

    def ensure(record, store, engines, scoped, *, runtime_snapshot):
        observed.append(observe_settings())
        assert runtime_snapshot == frozen
        return original_ensure(record, store, engines, scoped, runtime_snapshot=runtime_snapshot)

    def pipeline(params, on_event, *, plugin_manager, plugin_plan, credential_resolver, legacy_cache):
        observed.append(observe_settings())
        assert params.model == frozen["whisper"]["model"]
        assert plugin_plan["slots"]["translate"]["config"]["model"] == frozen["engine"]["model"]
        assert plugin_plan["slots"]["transcribe"]["config"]["model"] == "base.en"
        on_event(PipelineEvent("SUCCESS", 100, None, outputs={}))

    monkeypatch.setattr(runner, "_store", store)
    monkeypatch.setattr(runner, "ensure_plan", ensure)
    monkeypatch.setattr(runner, "run_pipeline", pipeline)
    runner._run(record.id)
    result = store.get(record.id)
    assert result.status == "SUCCESS", result.error
    expected = {**frozen["business"], **(current if desktop else {})}
    # Settings represents target_languages as an immutable tuple.
    expected["target_languages"] = tuple(expected["target_languages"])
    assert observed == [expected, expected]
    assert result.runtime_config_json == encoded
    if existing_plan:
        assert result.plugin_plan_json == saved_plan
    assert provider.read().values == before_config
    assert settings.download_quality == "360p"  # scope reset
    assert settings.ffmpeg_bin == current["ffmpeg_bin"]


@pytest.mark.parametrize("key", ["ffmpeg_bin", "ffprobe_bin"])
@pytest.mark.parametrize("invalid", ["relative", "missing", "directory", "not_executable"])
def test_invalid_current_tools_cannot_fall_back_to_environment_or_history(tmp_path, monkeypatch, key, invalid):
    from pathlib import Path

    valid = _bundle(tmp_path, "valid")
    provider = _launch(monkeypatch, valid, tmp_path / "config.db")
    invalid_path = tmp_path / invalid
    if invalid == "directory":
        invalid_path.mkdir()
    elif invalid == "not_executable":
        invalid_path.write_text("not an executable")
        invalid_path.chmod(0o600)
    value = "ffmpeg" if invalid == "relative" else str(invalid_path)
    provider.mutate(lambda doc: doc["business"].update({key: value}))
    snapshot = {"business": {**valid, "download_quality": "720p"}}
    original = copy.deepcopy(snapshot)
    # Even executable historical/env paths cannot rescue an invalid boot config.
    assert Path(valid[key]).is_file()
    with pytest.raises(ValueError, match="Desktop bootstrap executable unavailable: " + key):
        runner._execution_business_settings(snapshot, provider.read())
    assert snapshot == original
