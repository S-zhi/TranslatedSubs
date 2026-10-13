from pathlib import Path

import pytest

from src.core import transcriber
from src.plugin_system import (
    FakeContext,
    PluginCancelled,
    PluginError,
    TranscribeRequest,
    TranscribeResult,
)
from src.plugins.builtin import transcriber_whisper


def _config(backend="local_whisper", model="small", **overrides):
    config = {
        "backend": backend,
        "model": model,
        "service_url": None,
        "credential_ref": None,
        "timeout_seconds": 73,
        "device": "cpu",
        "compute_type": "int8",
        "download_root": None,
        "beam_size": 4,
    }
    if backend in {"http", "custom", "custom_http"}:
        config["service_url"] = "https://speech.example/transcribe"
    config.update(overrides)
    return config


def _request(task_dir: Path, source: Path, backend="local_whisper", model="local:small"):
    return TranscribeRequest(
        stage="transcribe",
        operation="transcribe",
        purpose="transcribe",
        audio_path=source,
        output_srt=task_dir / "original.srt",
        source_lang="en",
        backend=backend,
        model=model,
    )


def test_factory_manifest_and_local_service_use_frozen_model_settings(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    audio = task_dir / "audio.wav"
    audio.write_bytes(b"audio")
    captured = {}

    def fake_transcribe(audio_path, task_id, **kwargs):
        captured.update(kwargs)
        output = task_dir / "original.srt"
        output.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
        kwargs["on_progress"](type("Progress", (), {"percent": 44.0})())
        return transcriber.TranscribeResult(output, "en", 0.99, 1, 1.0)

    monkeypatch.setattr(transcriber_whisper.transcriber, "transcribe", fake_transcribe)
    context = FakeContext(task_id="task", task_dir=task_dir, purpose="transcribe")
    session = transcriber_whisper.factory.create_session(_config(), context)
    result = session.run(_request(task_dir, audio))

    assert transcriber_whisper.factory.manifest.plugin_id == "builtin.whisper"
    assert transcriber_whisper.factory.manifest.implementation_version == "1"
    assert result == TranscribeResult(task_dir / "original.srt", 1)
    service = captured["service"]
    assert isinstance(service, transcriber.LocalWhisperTranscriber)
    assert service.model_name == "small"
    assert service.device == "cpu" and service.compute_type == "int8"
    assert service.download_root is None and service.beam_size == 4
    assert service.require_model_ready is True
    assert captured["model_name"] == "small"
    assert context.progress == [44.0]
    session.close()


def test_http_service_uses_snapshot_url_and_current_optional_credential(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    audio = task_dir / "audio.wav"
    audio.write_bytes(b"audio")
    ref = "settings:transcriber_api_key"
    captured = {}

    def fake_transcribe(audio_path, task_id, **kwargs):
        captured.update(kwargs)
        output = task_dir / "original.srt"
        output.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
        return transcriber.TranscribeResult(output, "en", 0.9, 1, 1.0)

    monkeypatch.setattr(transcriber_whisper.transcriber, "transcribe", fake_transcribe)
    context = FakeContext(
        task_id="task", task_dir=task_dir, purpose="transcribe",
        credentials={ref: "current-stt-key"},
    )
    config = _config(
        backend="http",
        model="whisper-v2",
        credential_ref=ref,
        timeout_seconds=19,
    )
    session = transcriber_whisper.factory.create_session(config, context)
    result = session.run(_request(task_dir, audio, backend="custom_http", model="whisper-v2"))

    service = captured["service"]
    assert isinstance(service, transcriber.HttpTranscriber)
    assert service.url == "https://speech.example/transcribe"
    assert service.api_key == "current-stt-key"
    assert service.timeout == 19
    assert result.segments == 1
    context.assert_credentials_not_exposed(result)
    session.close()


def test_http_service_allows_missing_optional_key_and_missing_ref_is_explicit(
    tmp_path, monkeypatch
):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    audio = task_dir / "audio.wav"
    audio.write_bytes(b"audio")
    captured = {}

    def fake_transcribe(audio_path, task_id, **kwargs):
        captured.update(kwargs)
        output = task_dir / "original.srt"
        output.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
        return transcriber.TranscribeResult(output, "en", None, 1, 1.0)

    monkeypatch.setattr(transcriber_whisper.transcriber, "transcribe", fake_transcribe)
    session = transcriber_whisper.factory.create_session(
        _config(backend="http", model="server-model"),
        FakeContext(task_id="task", task_dir=task_dir, purpose="transcribe"),
    )
    session.run(_request(task_dir, audio, backend="http", model="server-model"))
    assert captured["service"].api_key is None

    missing = transcriber_whisper.factory.create_session(
        _config(backend="http", model="server-model", credential_ref="settings:transcriber_api_key"),
        FakeContext(task_id="task", task_dir=task_dir, purpose="transcribe"),
    )
    with pytest.raises(PluginError) as exc_info:
        missing.run(_request(task_dir, audio, backend="http", model="server-model"))
    assert exc_info.value.code == "credential_missing"


def test_transcribe_cancellation_maps_to_plugin_cancelled_and_is_not_wrapped(
    tmp_path, monkeypatch
):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    audio = task_dir / "audio.wav"
    audio.write_bytes(b"audio")

    def cancel(*args, **kwargs):
        kwargs["cancel_check"]()
        raise AssertionError("cancel check should have interrupted")

    monkeypatch.setattr(transcriber_whisper.transcriber, "transcribe", cancel)
    session = transcriber_whisper.factory.create_session(
        _config(),
        FakeContext(task_id="task", task_dir=task_dir, purpose="transcribe", cancelled=True),
    )
    with pytest.raises(PluginCancelled):
        session.run(_request(task_dir, audio))


def test_transcribe_errors_keep_code_and_cause_with_safe_message(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    audio = task_dir / "audio.wav"
    audio.write_bytes(b"audio")
    cause = transcriber.TranscribeError("speech service returned HTTP 429", code="rate_limited")

    def fail(*args, **kwargs):
        raise cause

    monkeypatch.setattr(transcriber_whisper.transcriber, "transcribe", fail)
    session = transcriber_whisper.factory.create_session(
        _config(), FakeContext(task_id="task", task_dir=task_dir, purpose="transcribe")
    )
    with pytest.raises(PluginError) as exc_info:
        session.run(_request(task_dir, audio))
    assert exc_info.value.code == "rate_limited"
    assert exc_info.value.cause is cause
    assert "HTTP 429" in exc_info.value.message


def test_transcription_dependency_cause_is_actionable_but_existing_core_code_wins(
    tmp_path, monkeypatch
):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    audio = task_dir / "audio.wav"
    audio.write_bytes(b"audio")
    missing_binary = FileNotFoundError("private/path/secret-binary")
    wrapped = RuntimeError("transcriber invocation failed")
    wrapped.__cause__ = missing_binary

    def fail(*args, **kwargs):
        raise wrapped

    monkeypatch.setattr(transcriber_whisper.transcriber, "transcribe", fail)
    session = transcriber_whisper.factory.create_session(
        _config(), FakeContext(task_id="task", task_dir=task_dir, purpose="transcribe")
    )
    with pytest.raises(PluginError) as exc_info:
        session.run(_request(task_dir, audio))
    assert exc_info.value.code == "dependency_missing"
    assert "install" in exc_info.value.message.lower()
    assert "private/path" not in exc_info.value.message
    assert exc_info.value.cause is wrapped

    core_error = transcriber.TranscribeError("model is not installed", code="model_not_installed")
    core_error.__cause__ = ImportError("implementation detail")

    def fail_with_core_code(*args, **kwargs):
        raise core_error

    monkeypatch.setattr(transcriber_whisper.transcriber, "transcribe", fail_with_core_code)
    with pytest.raises(PluginError) as preserved:
        session.run(_request(task_dir, audio))
    assert preserved.value.code == "model_not_installed"
    assert preserved.value.cause is core_error
