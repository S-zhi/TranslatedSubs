from pathlib import Path
from types import SimpleNamespace

import pytest

from src.plugin_system import (
    FakeContext,
    PluginCancelled,
    PluginError,
    TranslateRequest,
    TranslateResult,
)
from src.plugins.builtin import translator_engine
from src.store.translation_engine_store import LOCAL_TRANSLATION_MODEL


def _config(api_type="openai_compatible", engine_id="engine_test"):
    return {
        "engine_id": engine_id,
        "api_type": api_type,
        "base_url": "https://translator.example/v1",
        "model": (
            LOCAL_TRANSLATION_MODEL if api_type == "local_ct2"
            else "model-fixed-at-queue-time"
        ),
        "credential_ref": (
            None if api_type == "local_ct2"
            else f"translation_engine:{engine_id}:api_key"
        ),
        "batch_size": 7,
        "local_batch_size": 13,
        "timeout_seconds": 42,
    }


def _request(task_dir: Path, source: Path) -> TranslateRequest:
    return TranslateRequest(
        stage="translate",
        operation="translate",
        purpose="translate",
        original_srt=source,
        output_srt=task_dir / "translated.srt",
        source_lang="en",
        target_lang="zh-CN",
        mode="bilingual",
    )


def test_factory_is_lightweight_and_declares_v1_manifest(tmp_path):
    context = FakeContext(task_dir=tmp_path)
    session = translator_engine.factory.create_session(_config(), context)

    assert translator_engine.factory.stage == "translate"
    assert translator_engine.factory.manifest.plugin_id == "builtin.translation_engine"
    assert translator_engine.factory.manifest.protocol_version == 1
    assert translator_engine.factory.manifest.operations == frozenset({"translate"})
    assert session._secret is None


def test_remote_engine_uses_frozen_snapshot_current_credential_and_explicit_batches(
    tmp_path, monkeypatch
):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    source = task_dir / "original.srt"
    source.write_text("input", encoding="utf-8")
    credential_ref = "translation_engine:engine_test:api_key"
    context = FakeContext(
        task_id="task",
        task_dir=task_dir,
        credentials={credential_ref: "current-secret"},
    )
    captured = {}

    def fake_translate(*args, **kwargs):
        captured.update(kwargs)
        output = task_dir / "translated.srt"
        output.write_text("translated", encoding="utf-8")
        kwargs["on_progress"](SimpleNamespace(percent=67.5))
        return translator_engine.translator.TranslateResult(output, 1, True)

    monkeypatch.setattr(translator_engine.translator, "translate_srt", fake_translate)
    session = translator_engine.factory.create_session(_config(), context)
    request = _request(task_dir, source)
    session.validate(request)
    result = session.run(request)

    assert result == TranslateResult(task_dir / "translated.srt", 1, True)
    assert captured["api_key"] == "current-secret"
    assert captured["engine_config"].api_type == "openai_compatible"
    assert captured["engine_config"].base_url == "https://translator.example/v1"
    assert captured["engine_config"].model == "model-fixed-at-queue-time"
    assert captured["engine_config"].timeout == 42
    assert captured["batch_size"] == 7
    assert captured["local_batch_size"] == 13
    assert context.progress == [67.5]
    context.assert_credentials_not_exposed(result)
    session.close()


def test_local_engine_needs_no_credential_and_missing_remote_credential_is_explicit(
    tmp_path, monkeypatch
):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    source = task_dir / "original.srt"
    source.write_text("input", encoding="utf-8")
    called = {}

    def fake_translate(*args, **kwargs):
        called.update(kwargs)
        output = task_dir / "translated.srt"
        output.write_text("translated", encoding="utf-8")
        return translator_engine.translator.TranslateResult(output, 1, True)

    monkeypatch.setattr(translator_engine.translator, "translate_srt", fake_translate)
    context = FakeContext(task_id="task", task_dir=task_dir)
    session = translator_engine.factory.create_session(_config("local_ct2"), context)
    result = session.run(_request(task_dir, source))
    assert result.segments == 1
    assert called["api_key"] is None
    assert called["engine_config"].api_type == "local_ct2"
    session.close()

    missing = translator_engine.factory.create_session(_config(), context)
    with pytest.raises(PluginError) as exc_info:
        missing.run(_request(task_dir, source))
    assert exc_info.value.code == "credential_missing"
    assert "secret" not in exc_info.value.message


def test_cancelled_context_is_not_wrapped_or_translated(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    source = task_dir / "original.srt"
    source.write_text("input", encoding="utf-8")
    context = FakeContext(
        task_id="task",
        task_dir=task_dir,
        credentials={"translation_engine:engine_test:api_key": "secret"},
        cancelled=True,
    )
    session = translator_engine.factory.create_session(_config(), context)
    with pytest.raises(PluginCancelled):
        session.run(_request(task_dir, source))


def test_config_rejects_wrong_credential_reference_and_unknown_keys(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    source = task_dir / "original.srt"
    source.write_text("input", encoding="utf-8")
    bad_config = _config()
    bad_config["credential_ref"] = "translation_engine:other:api_key"
    session = translator_engine.factory.create_session(
        bad_config,
        FakeContext(task_id="task", task_dir=task_dir),
    )
    with pytest.raises(PluginError) as exc_info:
        session.validate(_request(task_dir, source))
    assert exc_info.value.code == "invalid_input"

    bad_config = _config()
    bad_config["api_key"] = "must-not-be-in-config"
    session = translator_engine.factory.create_session(
        bad_config,
        FakeContext(task_id="task", task_dir=task_dir),
    )
    with pytest.raises(PluginError, match="unsupported fields"):
        session.validate(_request(task_dir, source))

    bad_local_config = _config("local_ct2")
    bad_local_config["model"] = "another-local-model"
    local_session = translator_engine.factory.create_session(
        bad_local_config,
        FakeContext(task_id="task", task_dir=task_dir),
    )
    with pytest.raises(PluginError, match="model is not supported"):
        local_session.validate(_request(task_dir, source))


def test_adapter_redacts_secret_from_public_error_but_keeps_cause(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    source = task_dir / "original.srt"
    source.write_text("input", encoding="utf-8")
    secret = "credential-value"
    context = FakeContext(
        task_id="task",
        task_dir=task_dir,
        credentials={"translation_engine:engine_test:api_key": secret},
    )
    failure = RuntimeError(f"provider echoed {secret}")

    def fail_translation(*args, **kwargs):
        raise failure

    monkeypatch.setattr(translator_engine.translator, "translate_srt", fail_translation)
    session = translator_engine.factory.create_session(_config(), context)
    with pytest.raises(PluginError) as exc_info:
        session.run(_request(task_dir, source))

    assert secret not in exc_info.value.message
    assert exc_info.value.cause is failure
    assert exc_info.value.__cause__ is failure
