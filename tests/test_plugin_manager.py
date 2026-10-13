import pytest

from src.plugin_system.context import FakeContext
from src.plugin_system.contracts import (
    DownloadRequest,
    DownloadResult,
    AudioExtractRequest,
    AudioExtractResult,
    ProbeRequest,
    ProbeFormat,
    ProbeResult,
    TranslateRequest,
    TranslateResult,
)
from src.plugin_system.errors import PluginCancelled, PluginError
from src.plugin_system.manifest import PluginManifest
from src.plugin_system.manager import PluginManager
from src.plugin_system.registry import PluginRegistry
from tests.plugin_fakes import FakeFactory


def _setup(stage="translate", *, result=None, error=None, close_error=None, capabilities=frozenset()):
    registry = PluginRegistry()
    factory = FakeFactory(stage, result=result, error=error)
    manifest = PluginManifest(
        plugin_id=f"builtin.{stage}",
        stage=stage,
        implementation_version="1",
        operations=frozenset({stage}) if stage != "render" else frozenset({"subtitle", "dub_mux"}),
        capabilities=capabilities,
    )
    registry.register(manifest, factory)
    if close_error is not None:
        original_create = factory.create_session

        def create(config, context):
            session = original_create(config, context)
            original_close = session.close

            def close():
                original_close()
                raise close_error

            session.close = close
            return session

        factory.create_session = create
    return PluginManager(registry), factory


def _translate_request(task_dir):
    return TranslateRequest(
        stage="translate", operation="translate", purpose="translate",
        original_srt=task_dir / "original.srt", output_srt=task_dir / "translated.srt",
        source_lang="en", target_lang="zh-CN", mode="mono",
    )


def _write_srt(path):
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")


def test_run_uses_isolated_session_validates_artifact_and_drops_late_progress(tmp_path):
    output = tmp_path / "translated.srt"
    _write_srt(output)
    manager, factory = _setup(result=TranslateResult(output, segments=1, bilingual=False))
    context = FakeContext(task_dir=tmp_path, purpose="translate")

    result = manager.run("builtin.translate", "1", _translate_request(tmp_path), {}, context)
    assert result.translated_srt == output
    assert len(factory.created) == 1
    assert factory.created[0].close_count == 1
    assert len(factory.created[0].validated) == 1
    assert len(factory.created[0].ran) == 1
    factory.created[0].context.report_progress(75)
    assert context.progress == []

    manager.run("builtin.translate", "1", _translate_request(tmp_path), {}, context)
    assert len(factory.created) == 2
    assert factory.created[0] is not factory.created[1]
    assert all(session.close_count == 1 for session in factory.created)


@pytest.mark.parametrize("result_kind", ["wrong_type", "wrong_path", "missing", "empty", "invalid_srt"])
def test_run_rejects_invalid_results_and_always_closes(tmp_path, result_kind):
    output = tmp_path / "translated.srt"
    if result_kind == "wrong_type":
        result = DownloadResult(tmp_path / "source.mp4")
    elif result_kind == "wrong_path":
        _write_srt(output)
        result = TranslateResult(tmp_path / "original.srt", segments=1, bilingual=False)
    elif result_kind == "missing":
        result = TranslateResult(output, segments=1, bilingual=False)
    elif result_kind == "empty":
        output.write_bytes(b"")
        result = TranslateResult(output, segments=1, bilingual=False)
    else:
        output.write_text("not an srt", encoding="utf-8")
        result = TranslateResult(output, segments=1, bilingual=False)
    manager, factory = _setup(result=result)

    with pytest.raises(PluginError) as raised:
        manager.run("builtin.translate", "1", _translate_request(tmp_path), {}, FakeContext(task_dir=tmp_path))
    assert raised.value.code == "invalid_result"
    assert factory.created[0].close_count == 1


def test_run_rejects_request_context_and_version_mismatch_before_create(tmp_path):
    manager, factory = _setup(result=TranslateResult(tmp_path / "translated.srt", 0, False))
    request = _translate_request(tmp_path)

    with pytest.raises(PluginError) as context_error:
        manager.run("builtin.translate", "1", request, {}, FakeContext(task_dir=tmp_path, purpose="tts"))
    assert context_error.value.code == "invalid_input"
    with pytest.raises(PluginError) as version_error:
        manager.run("builtin.translate", "2", request, {}, FakeContext(task_dir=tmp_path))
    assert version_error.value.code == "version_mismatch"
    assert factory.created == []


def test_cancellation_precedes_lookup_and_unknown_plugin_uses_requested_stage(tmp_path):
    manager, _ = _setup()
    with pytest.raises(PluginCancelled):
        manager.run(
            "missing", "1", _translate_request(tmp_path), {},
            FakeContext(task_dir=tmp_path, cancelled=True),
        )
    with pytest.raises(PluginError) as raised:
        manager.run(
            "missing", "1", _translate_request(tmp_path), {},
            FakeContext(task_dir=tmp_path),
        )
    assert raised.value.code == "plugin_unavailable"
    assert raised.value.stage == "translate"


@pytest.mark.parametrize("bad_request", [
    lambda task_dir: TranslateRequest(
        stage="translate", operation="translate", purpose="translate",
        original_srt=task_dir / "original.srt", output_srt=task_dir / "wrong-name.srt",
        source_lang="en", target_lang="zh-CN", mode="mono",
    ),
    lambda task_dir: TranslateRequest(
        stage="translate", operation="translate", purpose=[],
        original_srt=task_dir / "original.srt", output_srt=task_dir / "translated.srt",
        source_lang="en", target_lang="zh-CN", mode="mono",
    ),
    lambda task_dir: TranslateRequest(
        stage="translate", operation="translate", purpose="translate",
        original_srt=task_dir / "original.srt", output_srt=task_dir / "translated.srt",
        source_lang="en", target_lang="zh-CN", mode=[],
    ),
])
def test_rejects_invalid_request_fields_before_factory_create(tmp_path, bad_request):
    manager, factory = _setup(result=TranslateResult(tmp_path / "translated.srt", 0, False))
    with pytest.raises(PluginError) as raised:
        manager.run("builtin.translate", "1", bad_request(tmp_path), {}, FakeContext(task_dir=tmp_path))
    assert raised.value.code == "invalid_input"
    assert factory.created == []


def test_probe_rejects_non_string_url_as_invalid_input(tmp_path):
    manager, _ = _setup(stage="download", result=ProbeResult(ok=True), capabilities=frozenset({"probe"}))
    request = ProbeRequest(url="https://example.test/video")
    object.__setattr__(request, "url", object())
    with pytest.raises(PluginError) as raised:
        manager.run_probe(
            "builtin.download", "1", request, {},
            FakeContext(task_dir=tmp_path, purpose="download"),
        )
    assert raised.value.code == "invalid_input"


@pytest.mark.parametrize("mutate", [
    lambda result: object.__setattr__(result, "formats_count", True),
    lambda result: object.__setattr__(result, "available_qualities", ["720p"]),
    lambda result: object.__setattr__(result, "formats", (object(),)),
    lambda result: object.__setattr__(result, "duration", float("nan")),
])
def test_probe_rejects_invalid_result_fields(tmp_path, mutate):
    result = ProbeResult(ok=True)
    mutate(result)
    manager, factory = _setup(
        stage="download", result=result, capabilities=frozenset({"probe"})
    )

    with pytest.raises(PluginError) as raised:
        manager.run_probe(
            "builtin.download", "1", ProbeRequest(url="https://example.test/video"), {},
            FakeContext(task_dir=tmp_path, purpose="download"),
        )

    assert raised.value.code == "invalid_result"
    assert factory.created[0].close_count == 1


def test_probe_rejects_invalid_nested_format_values(tmp_path):
    format_entry = ProbeFormat(
        formatId="1", ext="mp4", resolution="720p", height=720, width=1280,
        fps=30.0, vcodec="h264", acodec="aac", filesize=100, tbr=1000.0,
        note=None, type="video",
    )
    object.__setattr__(format_entry, "height", True)
    result = ProbeResult(ok=True, formats=(format_entry,))
    manager, factory = _setup(
        stage="download", result=result, capabilities=frozenset({"probe"})
    )

    with pytest.raises(PluginError) as raised:
        manager.run_probe(
            "builtin.download", "1", ProbeRequest(url="https://example.test/video"), {},
            FakeContext(task_dir=tmp_path, purpose="download"),
        )

    assert raised.value.code == "invalid_result"
    assert factory.created[0].close_count == 1


def test_audio_result_must_have_minimum_wav_signature(tmp_path):
    output = tmp_path / "audio.wav"
    output.write_bytes(b"not really wav")
    request = AudioExtractRequest(
        stage="audio_extract", operation="audio_extract", purpose="extract.asr",
        source_path=tmp_path / "source.mp4", output_path=output,
        sample_rate=16000, channels=1,
    )
    manager, factory = _setup(
        stage="audio_extract",
        result=AudioExtractResult(output, sample_rate=16000, channels=1),
    )
    with pytest.raises(PluginError) as raised:
        manager.run(
            "builtin.audio_extract", "1", request, {},
            FakeContext(task_dir=tmp_path, purpose="extract.asr"),
        )
    assert raised.value.code == "invalid_result"
    assert factory.created[0].close_count == 1


def test_paths_must_be_inside_task_and_reject_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside.srt"
    _write_srt(outside)
    link = tmp_path / "translated.srt"
    link.symlink_to(outside)
    manager, factory = _setup(result=TranslateResult(link, 1, False))

    with pytest.raises(PluginError) as raised:
        manager.run("builtin.translate", "1", _translate_request(tmp_path), {}, FakeContext(task_dir=tmp_path))
    assert raised.value.code == "invalid_input"
    assert factory.created == []


def test_rejects_artifact_symlink_created_during_plugin_run(tmp_path):
    outside = tmp_path.parent / "outside-artifact.srt"
    _write_srt(outside)
    output = tmp_path / "translated.srt"
    manager, factory = _setup(result=TranslateResult(output, 1, False))
    original_create = factory.create_session

    def create(config, context):
        session = original_create(config, context)
        original_run = session.run

        def run(request):
            output.symlink_to(outside)
            return original_run(request)

        session.run = run
        return session

    factory.create_session = create
    with pytest.raises(PluginError) as raised:
        manager.run("builtin.translate", "1", _translate_request(tmp_path), {}, FakeContext(task_dir=tmp_path))
    assert raised.value.code == "invalid_result"
    assert factory.created[0].close_count == 1


def test_original_error_and_cause_survive_close_failure(tmp_path):
    failure = RuntimeError("API_KEY=secret")
    manager, factory = _setup(error=failure, close_error=RuntimeError("close failed"))

    with pytest.raises(PluginError) as raised:
        manager.run("builtin.translate", "1", _translate_request(tmp_path), {}, FakeContext(task_dir=tmp_path))
    assert raised.value.code == "execution_error"
    assert raised.value.__cause__ is failure
    assert isinstance(raised.value.cleanup_error, RuntimeError)
    assert "secret" not in raised.value.message
    assert factory.created[0].close_count == 1


def test_manager_gives_session_an_immutable_copy_of_config(tmp_path):
    output = tmp_path / "translated.srt"
    _write_srt(output)
    manager, factory = _setup(result=TranslateResult(output, 1, False))
    original_config = {"engine": "local", "batch_size": 8}
    observed = {}
    original_create = factory.create_session

    def create(config, context):
        observed["config"] = config
        return original_create(config, context)

    factory.create_session = create
    manager.run(
        "builtin.translate", "1", _translate_request(tmp_path), original_config,
        FakeContext(task_dir=tmp_path),
    )
    original_config["engine"] = "changed"
    assert observed["config"]["engine"] == "local"
    with pytest.raises(TypeError):
        observed["config"]["engine"] = "mutated"


def test_close_failure_after_success_becomes_cleanup_error(tmp_path):
    output = tmp_path / "translated.srt"
    _write_srt(output)
    manager, factory = _setup(
        result=TranslateResult(output, 1, False), close_error=RuntimeError("cleanup")
    )

    with pytest.raises(PluginError) as raised:
        manager.run("builtin.translate", "1", _translate_request(tmp_path), {}, FakeContext(task_dir=tmp_path))
    assert raised.value.code == "cleanup_error"
    assert factory.created[0].close_count == 1


def test_cancellation_wins_and_create_failure_does_not_close(tmp_path):
    manager, factory = _setup(error=RuntimeError("ordinary failure"))
    context = FakeContext(task_dir=tmp_path)
    session = factory.create_session

    def create_then_cancel(config, wrapped_context):
        created = session(config, wrapped_context)
        context.cancelled = True
        return created

    factory.create_session = create_then_cancel
    with pytest.raises(PluginCancelled):
        manager.run("builtin.translate", "1", _translate_request(tmp_path), {}, context)
    assert factory.created[0].close_count == 1

    manager_cancel, factory_cancel = _setup(error=RuntimeError("execution failed"))
    cancel_context = FakeContext(task_dir=tmp_path)
    original_create = factory_cancel.create_session

    def create_and_cancel_on_close(config, wrapped_context):
        created = original_create(config, wrapped_context)
        original_close = created.close

        def close():
            original_close()
            cancel_context.cancelled = True

        created.close = close
        return created

    factory_cancel.create_session = create_and_cancel_on_close
    with pytest.raises(PluginCancelled):
        manager_cancel.run(
            "builtin.translate", "1", _translate_request(tmp_path), {}, cancel_context
        )
    assert factory_cancel.created[0].close_count == 1

    manager2, factory2 = _setup()

    def fail_create(config, wrapped_context):
        raise RuntimeError("factory failed")

    factory2.create_session = fail_create
    with pytest.raises(PluginError) as raised:
        manager2.run("builtin.translate", "1", _translate_request(tmp_path), {}, FakeContext(task_dir=tmp_path))
    assert raised.value.code == "execution_error"
    assert factory2.created == []


def test_probe_uses_download_capability_and_does_not_require_artifact(tmp_path):
    manager, factory = _setup(stage="download", result=ProbeResult(ok=True), capabilities=frozenset({"probe"}))
    result = manager.run_probe(
        "builtin.download", "1", ProbeRequest("https://example.test/video"), {},
        FakeContext(task_dir=tmp_path, purpose="download"),
    )
    assert result.ok is True
    assert factory.created[0].close_count == 1

