from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.plugin_system import (
    AudioExtractRequest,
    DownloadRequest,
    DownloadResult,
    FakeContext,
    PluginCancelled,
    PluginError,
    PluginManifest,
    ProbeFormat,
    ProbeRequest,
    ProbeResult,
    SubtitleRenderRequest,
    validate_registration,
)
from tests.plugin_fakes import FakeFactory


def test_manifest_requires_complete_operations_and_valid_versions():
    with pytest.raises(ValueError, match="missing required operation"):
        PluginManifest(
            plugin_id="builtin.render", stage="render", operations=frozenset({"subtitle"})
        )

    with pytest.raises(ValueError, match="unsupported operation"):
        PluginManifest(
            plugin_id="builtin.translate", stage="translate",
            operations=frozenset({"translate", "probe"}),
        )

    with pytest.raises(ValueError, match="unsupported plugin protocol"):
        PluginManifest(
            plugin_id="builtin.download", stage="download", protocol_version=2,
            operations=frozenset({"download"}),
        )

    manifest = PluginManifest(
        plugin_id="builtin.download", stage="download", operations=frozenset({"download"})
    )
    factory = FakeFactory("download")
    validate_registration(manifest, factory, set())
    with pytest.raises(ValueError, match="already registered"):
        validate_registration(manifest, factory, {"builtin.download"})
    with pytest.raises(ValueError, match="does not match"):
        validate_registration(manifest, FakeFactory("translate"), set())


def test_request_types_keep_stage_operation_and_output_path_contract_explicit():
    target = Path("/tmp/task-1/audio.wav")
    request = AudioExtractRequest(
        stage="audio_extract",
        operation="audio_extract",
        purpose="extract.asr",
        source_path=Path("/tmp/task-1/source.mp4"),
        output_path=target,
        sample_rate=16000,
        channels=1,
    )
    assert (request.stage, request.operation, request.purpose) == (
        "audio_extract", "audio_extract", "extract.asr"
    )
    assert request.output_path == target
    with pytest.raises(FrozenInstanceError):
        request.output_path = Path("/tmp/task-1/tts_source.wav")

    download = DownloadRequest(
        stage="download", operation="download", purpose="download", url="https://example.test/v",
        quality="720p", target_dir=Path("/tmp/task-1"),
    )
    assert download.source_stem == "source"
    render = SubtitleRenderRequest(
        stage="render", operation="subtitle", purpose="render.subtitle",
        source_video=Path("/tmp/task-1/source.mp4"),
        translated_srt=Path("/tmp/task-1/translated.srt"),
        output_video=Path("/tmp/task-1/output_hard.mp4"), mode="hard",
    )
    assert render.output_video.name == "output_hard.mp4"
    assert ProbeRequest("https://example.test/v").force_refresh is False
    format_metadata = ProbeFormat(
        formatId="137", ext="mp4", resolution="1080p", height=1080, width=1920,
        fps=30.0, vcodec="h264", acodec=None, filesize=100, tbr=900.0,
        note=None, type="video",
    )
    probe_result = ProbeResult(ok=True, formats=(format_metadata,))
    assert probe_result.formats[0].height == 1080


def test_context_tracks_callbacks_and_rejects_invalid_progress():
    proc = object()
    context = FakeContext(
        task_id="task-9", invocation_id="call-9", task_dir=Path("/tmp/task-9"),
        purpose="translate", credentials={"translation_engine:local:api_key": "secret-value"},
    )
    context.report_progress(0)
    context.report_progress(None)
    context.register_process(proc)
    context.unregister_process(proc)
    assert context.progress == [0, None]
    assert context.registered_processes == []
    assert context.unregistered_processes == [proc]
    assert context.resolve_credential("translation_engine:local:api_key") == "secret-value"
    context.assert_credentials_not_exposed({"message": "safe"})
    with pytest.raises(AssertionError, match="exposed"):
        context.assert_credentials_not_exposed({"message": "secret-value"})
    with pytest.raises(ValueError, match="between 0 and 100"):
        context.report_progress(101)
    with pytest.raises(ValueError, match="between 0 and 100"):
        context.report_progress(True)
    with pytest.raises(ValueError, match="between 0 and 100"):
        context.report_progress(float("nan"))
    with pytest.raises(AttributeError):
        context.task_id = "changed"
    assert FakeContext().invocation_id != FakeContext().invocation_id
    context.cancelled = True
    with pytest.raises(PluginCancelled):
        context.check_cancelled()


def test_plugin_error_redacts_common_credentials_and_omits_cause_from_dict():
    cause = RuntimeError("API_KEY=topsecret")
    error = PluginError(
        "execution_error", "translate", "builtin.translation_engine",
        "request failed: https://user:pass@example.test/?token=abc Bearer xyz", cause,
    )
    assert "user:pass" not in error.message
    assert "token=abc" not in error.message
    assert "Bearer xyz" not in error.message
    assert error.__cause__ is cause
    assert error.to_dict() == {
        "code": "execution_error",
        "stage": "translate",
        "plugin_id": "builtin.translation_engine",
        "message": error.message,
    }
    assert "cause" not in error.to_dict()


def test_minimal_plugin_example_covers_success_failure_and_cancellation(tmp_path):
    request = DownloadRequest(
        stage="download", operation="download", purpose="download",
        url="https://example.test/video", quality="480p", target_dir=tmp_path,
    )
    output_path = tmp_path / "source.mp4"
    success_factory = FakeFactory("download", DownloadResult(source_path=output_path, title="Demo"))
    success = success_factory.create_session({}, FakeContext(task_dir=tmp_path))
    success.validate(request)
    result = success.run(request)
    success.close()
    assert result.source_path == output_path
    assert success.close_count == 1

    failure = RuntimeError("decoder failed")
    failing_factory = FakeFactory("download", error=failure)
    failing = failing_factory.create_session({}, FakeContext(task_dir=tmp_path))
    with pytest.raises(RuntimeError, match="decoder failed"):
        failing.run(request)
    failing.close()
    assert failing.close_count == 1

    cancelled_context = FakeContext(task_dir=tmp_path, cancelled=True)
    cancelled = FakeFactory("download", DownloadResult(source_path=output_path)).create_session(
        {}, cancelled_context
    )
    with pytest.raises(PluginCancelled):
        cancelled.run(request)
    cancelled.close()
    assert cancelled.close_count == 1
