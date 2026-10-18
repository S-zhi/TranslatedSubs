"""I1 pipeline orchestration through the injected plugin manager."""

from pathlib import Path

import pytest

from src.plugin_system import (
    AudioExtractResult, DownloadResult, PluginError, SubtitleRenderResult, TranscribeResult,
    TranslateResult,
)
from src.service import orchestrator
from src.service.orchestrator import (
    PipelineHandler, PipelineParams, PipelineResources, build_default_pipeline_chain,
    build_pipeline_chain, run_pipeline,
)


def _params(**overrides):
    values = dict(
        task_id="t1", url="https://example.test/video", source_lang="en",
        target_lang="zh-CN", mode="mono", burn="hard", model="local:tiny",
    )
    values.update(overrides)
    return PipelineParams(**values)


def _plan(params=None):
    params = params or _params()
    slots = {}

    def add(purpose, plugin_id, config):
        slots[purpose] = {
            "plugin_id": plugin_id, "implementation_version": "1", "config": config,
        }

    if params.source_type == "url":
        add("download", "builtin.ytdlp", {"quality": "480p"})
    if params.need_subtitle:
        add("extract.asr", "builtin.ffmpeg_audio", {"sample_rate": 16000, "channels": 1})
        add("transcribe", "builtin.whisper", {"backend": "local_whisper", "model": "tiny"})
        add("translate", "builtin.translation_engine", {})
        add("render.subtitle", "builtin.ffmpeg_render", {"mode": params.burn})
    return {"schema_version": 1, "slots": slots}


class FakeManager:
    def __init__(self, *, percent=100, fail_at=None):
        self.percent = percent
        self.fail_at = fail_at
        self.calls = []
        self.requests = []

    def run(self, plugin_id, version, request, config, context):
        purpose = request.purpose
        self.calls.append(purpose)
        self.requests.append((plugin_id, version, request, dict(config)))
        if purpose == self.fail_at:
            raise RuntimeError("secret-bearing underlying error")
        context.report_progress(self.percent)
        if purpose == "download":
            output = request.target_dir / "source.mp4"
            output.write_bytes(b"source-video")
            return DownloadResult(output, title="Example title")
        if purpose == "extract.asr":
            request.output_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
            return AudioExtractResult(request.output_path, request.sample_rate, request.channels)
        if purpose == "transcribe":
            request.output_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
            return TranscribeResult(request.output_srt, 1)
        if purpose == "translate":
            request.output_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n", encoding="utf-8")
            return TranslateResult(request.output_srt, 1, request.mode == "bilingual")
        if purpose == "render.subtitle":
            request.output_video.write_bytes(b"\x00\x00\x00\x18ftypisom")
            return SubtitleRenderResult(request.output_video)
        raise AssertionError(purpose)


def _run(monkeypatch, tmp_path, params=None, *, manager=None, events=None):
    params = params or _params()
    manager = manager or FakeManager()
    events = events if events is not None else []
    monkeypatch.setattr(orchestrator, "task_dir", lambda task_id: tmp_path)
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda task_id: tmp_path)
    event = run_pipeline(
        params, events.append, plugin_manager=manager, plugin_plan=_plan(params),
        credential_resolver=lambda ref: "secret-value",
    )
    return event, events, manager


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (KeyError("field"), "invalid_input"),
        (ValueError("bad value"), "invalid_input"),
        (AttributeError("missing"), "internal_error"),
        (TypeError("wrong type"), "internal_error"),
        (OSError("disk failure"), "io_error"),
        (RuntimeError("unexpected"), "internal_error"),
    ],
)
def test_error_code_for_exception_maps_builtin_errors(exception, expected):
    assert orchestrator._error_code_for_exception(exception) == expected


def test_pipeline_routes_every_enabled_stage_through_manager(monkeypatch, tmp_path):
    event, events, manager = _run(monkeypatch, tmp_path)
    assert manager.calls == ["download", "extract.asr", "transcribe", "translate", "render.subtitle"]
    assert event.status == "SUCCESS"
    assert event.title == "Example title"
    assert event.outputs["video"].endswith("output_hard.mp4")
    assert event.outputs["subtitle"].endswith("translated.srt")
    assert [item.progress for item in events] == sorted(item.progress for item in events)
    assert events[-1].status == "SUCCESS" and events[-1].progress == 100
    assert (tmp_path / "translate.plugin-meta.json").is_file()


def test_pre_cancelled_pipeline_wins_over_invalid_plugin_plan():
    from src.service.orchestrator import (
        PipelineCancelledError, set_cancelled_signal, unregister_cancellation_signal,
    )

    params = _params()
    set_cancelled_signal(params.task_id)
    try:
        with pytest.raises(PipelineCancelledError):
            run_pipeline(
                params, lambda event: None, plugin_manager=FakeManager(), plugin_plan={},
                credential_resolver=lambda ref: "secret",
            )
    finally:
        unregister_cancellation_signal(params.task_id)


def test_pipeline_maps_progress_bands(monkeypatch, tmp_path):
    _, events, manager = _run(monkeypatch, tmp_path, manager=FakeManager(percent=50))
    by_status = {}
    for event in events:
        by_status.setdefault(event.status, []).append(event.progress)
    assert 10 in by_status["DOWNLOADING"]
    assert 50 in by_status["TRANSCRIBING"]
    assert 92 in by_status["BURNING"]


def test_pipeline_sanitizes_unknown_plugin_failure(monkeypatch, tmp_path):
    events = []
    manager = FakeManager(fail_at="transcribe")
    with pytest.raises(RuntimeError):
        _run(monkeypatch, tmp_path, manager=manager, events=events)
    assert manager.calls == ["download", "extract.asr", "transcribe"]
    assert events[-1].status == "FAILED"
    assert events[-1].error_code == "internal_error"
    assert "secret-bearing" not in events[-1].error


def test_pipeline_preserves_plugin_error_code_and_stops_downstream(monkeypatch, tmp_path):
    class CodeFailure(FakeManager):
        def run(self, plugin_id, version, request, config, context):
            self.calls.append(request.purpose)
            if request.purpose == "transcribe":
                raise PluginError(
                    "model_unavailable", "transcribe", plugin_id,
                    "transcription model unavailable",
                )
            self.calls.pop()
            return super().run(plugin_id, version, request, config, context)

    manager = CodeFailure()
    events = []
    with pytest.raises(PluginError) as exc_info:
        _run(monkeypatch, tmp_path, manager=manager, events=events)
    assert exc_info.value.code == "model_unavailable"
    assert manager.calls == ["download", "extract.asr", "transcribe"]
    assert events[-1].error_code == "model_unavailable"
    assert events[-1].error == "transcription model unavailable"


def test_plugin_cancel_during_invocation_cleans_new_output_and_stops_chain(monkeypatch, tmp_path):
    from src.service.orchestrator import (
        PipelineCancelledError, set_cancelled_signal, unregister_cancellation_signal,
    )

    class CancellingManager(FakeManager):
        def run(self, plugin_id, version, request, config, context):
            if request.purpose == "extract.asr":
                self.calls.append(request.purpose)
                request.output_path.write_bytes(b"new partial audio")
                set_cancelled_signal("t1")
                context.report_progress(25)
            return super().run(plugin_id, version, request, config, context)

    manager = CancellingManager()
    params = _params()
    monkeypatch.setattr(orchestrator, "task_dir", lambda task_id: tmp_path)
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda task_id: tmp_path)
    try:
        with pytest.raises(PipelineCancelledError):
            run_pipeline(
                params, lambda event: None, plugin_manager=manager,
                plugin_plan=_plan(params), credential_resolver=lambda ref: "secret",
            )
        assert manager.calls == ["download", "extract.asr"]
        assert not (tmp_path / "audio.wav").exists()
    finally:
        unregister_cancellation_signal(params.task_id)


def test_cancel_during_cache_metadata_publication_discards_invocation(monkeypatch, tmp_path):
    from src.service.orchestrator import (
        PipelineCancelledError, set_cancelled_signal, unregister_cancellation_signal,
    )

    params = _params()
    manager = FakeManager()
    monkeypatch.setattr(orchestrator, "task_dir", lambda task_id: tmp_path)
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda task_id: tmp_path)
    write_metadata = orchestrator.AssetResolver.write_plugin_cache_metadata

    def cancel_after_metadata(task_id, metadata):
        write_metadata(task_id, metadata)
        if metadata["purpose"] == "extract.asr":
            set_cancelled_signal(params.task_id)

    monkeypatch.setattr(
        orchestrator.AssetResolver, "write_plugin_cache_metadata", cancel_after_metadata
    )
    try:
        with pytest.raises(PipelineCancelledError):
            run_pipeline(
                params, lambda event: None, plugin_manager=manager, plugin_plan=_plan(params),
                credential_resolver=lambda ref: "secret",
            )
        assert manager.calls == ["download", "extract.asr"]
        assert (tmp_path / "source.mp4").is_file()
        assert (tmp_path / "download.plugin-meta.json").is_file()
        assert not (tmp_path / "audio.wav").exists()
        assert not (tmp_path / "extract.asr.plugin-meta.json").exists()
    finally:
        unregister_cancellation_signal(params.task_id)


def test_upload_without_source_fails_before_any_plugin_call(monkeypatch, tmp_path):
    from src.service.asset_resolver import ResourceError, ResourceState

    monkeypatch.setattr(orchestrator, "task_dir", lambda task_id: tmp_path)
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda task_id: tmp_path)
    monkeypatch.setattr(
        orchestrator.AssetResolver, "require_source",
        lambda task_id: (_ for _ in ()).throw(
            ResourceError("missing upload", ResourceState.DELETED)
        ),
    )
    params = _params(source_type="upload", need_subtitle=False)
    events = []
    manager = FakeManager()
    with pytest.raises(orchestrator.PipelineError):
        run_pipeline(
            params, events.append, plugin_manager=manager, plugin_plan=_plan(params),
            credential_resolver=lambda ref: "secret",
        )
    assert manager.calls == []
    assert events[-1].status == "FAILED"


def test_download_plugin_failure_stops_pipeline_before_next_stage(monkeypatch, tmp_path):
    class MissingOutputManager(FakeManager):
        def run(self, plugin_id, version, request, config, context):
            self.calls.append(request.purpose)
            raise PluginError(
                "download_failed", "download", plugin_id, "download output unavailable"
            )

    params = _params()
    monkeypatch.setattr(orchestrator, "task_dir", lambda task_id: tmp_path)
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda task_id: tmp_path)
    manager = MissingOutputManager()
    events = []
    with pytest.raises(PluginError):
        run_pipeline(
            params, events.append, plugin_manager=manager, plugin_plan=_plan(params),
            credential_resolver=lambda ref: "secret",
        )
    assert manager.calls == ["download"]
    assert events[-1].status == "FAILED"


@pytest.mark.parametrize("failure", ["run", "close", "metadata_before", "metadata_after"])
def test_download_retry_failure_cleans_new_artifacts_and_preserves_completed_stages(
    monkeypatch, tmp_path, failure,
):
    from src.plugin_system import PluginManifest
    from src.plugin_system.manager import PluginManager
    from src.plugin_system.registry import PluginRegistry
    from src.service.asset_resolver import ResourceState

    first, _, _ = _run(monkeypatch, tmp_path)
    assert first.status == "SUCCESS"
    published = {
        path: path.read_bytes() for path in tmp_path.iterdir()
        if path.name not in {"source.mp4", "download.plugin-meta.json"}
    }
    assert tmp_path / "output_hard.mp4" in published
    session_calls = []

    class DownloadSession:
        def validate(self, request):
            pass

        def run(self, request):
            session_calls.append("run")
            # Cache invalidation must precede the retry's new artifact writes.
            assert not (request.target_dir / "source.mp4").exists()
            assert not (request.target_dir / "download.plugin-meta.json").exists()
            output = request.target_dir / "source.webm"
            output.write_bytes(b"\x1a\x45\xdf\xa3replacement-webm")
            (request.target_dir / "source.webm.part").write_bytes(b"partial")
            (request.target_dir / "download.tmp").write_bytes(b"temporary")
            if failure == "run":
                raise RuntimeError("download failed after writing output")
            return DownloadResult(output)

        def close(self):
            session_calls.append("close")
            if failure == "close":
                raise RuntimeError("download client close failed")

    class DownloadFactory:
        stage = "download"
        manifest = PluginManifest(
            plugin_id="fixture.download", stage="download", implementation_version="1",
            operations=frozenset({"download"}),
        )

        def create_session(self, config, context):
            return DownloadSession()

    factory = DownloadFactory()
    registry = PluginRegistry()
    registry.register(factory.manifest, factory)
    params = _params(need_subtitle=False)
    plan = _plan(params)
    plan["slots"]["download"]["plugin_id"] = factory.manifest.plugin_id
    if failure.startswith("metadata"):
        write_metadata = orchestrator.AssetResolver.write_plugin_cache_metadata

        def fail_publication(task_id, metadata):
            if failure == "metadata_after":
                write_metadata(task_id, metadata)
            raise OSError("download metadata publication failed")

        monkeypatch.setattr(
            orchestrator.AssetResolver, "write_plugin_cache_metadata", fail_publication
        )
    events = []
    expected_error = OSError if failure.startswith("metadata") else PluginError
    with pytest.raises(expected_error):
        run_pipeline(
            params, events.append, plugin_manager=PluginManager(registry), plugin_plan=plan,
            credential_resolver=lambda ref: "secret",
        )

    assert session_calls == ["run", "close"]
    assert events[-1].status == "FAILED"
    assert events[-1].error_code == {
        "run": "execution_error", "close": "cleanup_error",
        "metadata_before": "io_error", "metadata_after": "io_error",
    }[failure]
    assert not list(tmp_path.glob("source.*"))
    assert not (tmp_path / "download.tmp").exists()
    assert not (tmp_path / "download.plugin-meta.json").exists()
    state, source, _ = orchestrator.AssetResolver.resolve_source(params.task_id)
    assert state == ResourceState.DELETED and source is None
    assert {path: path.read_bytes() for path in published} == published


def test_resume_reuses_valid_cached_stages_and_recomputes_invalidated_suffix(monkeypatch, tmp_path):
    params = _params()
    monkeypatch.setattr(orchestrator, "task_dir", lambda task_id: tmp_path)
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda task_id: tmp_path)
    first_manager = FakeManager()
    first = run_pipeline(
        params, lambda event: None, plugin_manager=first_manager, plugin_plan=_plan(params),
        credential_resolver=lambda ref: "secret",
    )
    assert first.status == "SUCCESS"

    # Simulate interruption after translation inputs were ready but before the
    # translated subtitle and rendered video were durably published.
    for name in ("translated.srt", "output_hard.mp4", "translate.plugin-meta.json", "render.subtitle.plugin-meta.json"):
        (tmp_path / name).unlink(missing_ok=True)
    second_manager = FakeManager()
    resumed = run_pipeline(
        params, lambda event: None, plugin_manager=second_manager, plugin_plan=_plan(params),
        credential_resolver=lambda ref: "secret",
    )
    assert resumed.status == "SUCCESS"
    assert second_manager.calls == ["translate", "render.subtitle"]


def test_upload_and_download_only_do_not_initialize_unneeded_slots(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"uploaded")
    params = _params(source_type="upload", need_subtitle=False, title="Upload")
    event, _, manager = _run(monkeypatch, tmp_path, params)
    assert manager.calls == []
    assert event.outputs == {"video": str(source)}

    params = _params(need_subtitle=False)
    event, _, manager = _run(monkeypatch, tmp_path, params)
    assert manager.calls == ["download"]
    assert event.status == "SUCCESS"


def test_custom_handler_chain_shares_resources_and_completes():
    calls = []
    resource_ids = []

    class Handler(PipelineHandler):
        def __init__(self, name, complete=False):
            super().__init__()
            self.name = name
            self.complete_result = complete

        def process(self, context):
            calls.append(self.name)
            resource_ids.append(id(context.resources))
            if self.name == "first":
                context.resources.video_path = Path("/source.mp4")
            if self.complete_result:
                context.complete(outputs={"video": str(context.resources.video_path)})

    chain = build_pipeline_chain(Handler("first"), Handler("second", complete=True))
    event = run_pipeline(
        _params(), lambda item: None, plugin_manager=FakeManager(), plugin_plan=_plan(),
        credential_resolver=lambda ref: "secret", handler_chain=chain,
    )
    assert calls == ["first", "second"]
    assert resource_ids[0] == resource_ids[1]
    assert event.outputs == {"video": "/source.mp4"}


def test_default_pipeline_keeps_the_six_handler_order():
    handler = build_default_pipeline_chain()
    names = []
    while handler is not None:
        names.append(type(handler).__name__)
        handler = handler._next_handler
    assert names == [
        "DownloadHandler", "AudioExtractionHandler", "VocalSeparationHandler",
        "TranscriptionHandler", "TranslationHandler", "SubtitleBurningHandler",
    ]


def test_pipeline_resources_are_isolated():
    first = PipelineResources(video_path=Path("/first.mp4"))
    second = PipelineResources()
    assert first.video_path == Path("/first.mp4")
    assert second.video_path is None
