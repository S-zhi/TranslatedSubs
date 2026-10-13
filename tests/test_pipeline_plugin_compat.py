"""V1 host compatibility tests using the real PluginManager and deterministic plugins."""

from __future__ import annotations

import dataclasses
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import settings
from src.plugin_system import (
    AudioExtractResult,
    DownloadResult,
    DubMuxResult,
    PluginCancelled,
    PluginError,
    PluginManifest,
    SubtitleRenderResult,
    TTSResult,
    TranscribeResult,
    TranslateResult,
    VocalSeparateResult,
)
from src.plugin_system.manager import PluginManager
from src.plugin_system.registry import PluginRegistry
from src.service import orchestrator
from src.service.plugin_plan import parse_plugin_plan, resolve_plugin_plan


STAGE_SPECS = {
    "download": ("download", frozenset({"download"})),
    "audio_extract": ("audio_extract", frozenset({"audio_extract"})),
    "vocal_separate": ("vocal_separate", frozenset({"vocal_separate"})),
    "transcribe": ("transcribe", frozenset({"transcribe"})),
    "translate": ("translate", frozenset({"translate"})),
    "tts": ("tts", frozenset({"tts"})),
    "render": ("render", frozenset({"subtitle", "dub_mux"})),
}
SLOT_STAGE = {
    "download": "download",
    "extract.asr": "audio_extract",
    "extract.dubbing": "audio_extract",
    "separate.asr": "vocal_separate",
    "separate.dubbing": "vocal_separate",
    "transcribe": "transcribe",
    "translate": "translate",
    "tts": "tts",
    "render.subtitle": "render",
    "render.dub_mux": "render",
}


def _wav(path: Path, *, rate: int = 16000, channels: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\0\0" * channels * (rate // 10))


def _mp4(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2")


def _srt(path: Path, text="source") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n", encoding="utf-8"
    )


class ScenarioFactory:
    def __init__(self, plugin_id: str, stage: str, *, fault=None, version="1") -> None:
        self.plugin_id = plugin_id
        self.stage, operations = STAGE_SPECS[stage]
        self.manifest = PluginManifest(
            plugin_id=plugin_id, stage=self.stage, implementation_version=version,
            operations=operations,
        )
        self.fault = fault
        self.created = 0
        self.requests = []

    def create_session(self, config, context):
        self.created += 1
        return ScenarioSession(self, context)


class ScenarioSession:
    def __init__(self, factory: ScenarioFactory, context) -> None:
        self.factory = factory
        self.context = context

    def validate(self, request) -> None:
        assert request.stage == self.factory.stage
        assert request.purpose == self.context.purpose

    def run(self, request):
        self.context.check_cancelled()
        self.factory.requests.append(request)
        fault = self.factory.fault
        if fault and fault[0] == request.purpose:
            if fault[1] == "cancel":
                raise PluginCancelled("fixture cancellation")
            raise PluginError("execution_error", request.stage, self.factory.plugin_id, "fixture failure")

        if request.operation == "download":
            path = request.target_dir / "source.mp4"
            _mp4(path)
            return DownloadResult(path, title="fixture", duration_seconds=1.0)
        if request.operation == "audio_extract":
            # Keep output bytes identical across frozen config changes so the
            # cache test covers producer provenance, not only input hashes.
            _wav(request.output_path)
            return AudioExtractResult(
                request.output_path, request.sample_rate, request.channels, 0.1
            )
        if request.operation == "vocal_separate":
            if request.vocal_path is not None:
                _wav(request.vocal_path)
            if request.background_path is not None:
                _wav(request.background_path)
            return VocalSeparateResult(request.vocal_path, request.background_path)
        if request.operation == "transcribe":
            _srt(request.output_srt)
            return TranscribeResult(request.output_srt, segments=1)
        if request.operation == "translate":
            _srt(request.output_srt, "translated")
            return TranslateResult(
                request.output_srt, segments=1, bilingual=request.mode == "bilingual"
            )
        if request.operation == "tts":
            _wav(request.output_audio, rate=24000)
            return TTSResult(request.output_audio, duration_seconds=0.1)
        if request.operation == "subtitle":
            _mp4(request.output_video)
            return SubtitleRenderResult(request.output_video)
        if request.operation == "dub_mux":
            _wav(request.mixed_audio, rate=48000, channels=2)
            _mp4(request.output_video)
            return DubMuxResult(request.mixed_audio, request.output_video)
        raise AssertionError(f"unexpected operation: {request.operation}")

    def close(self) -> None:
        return None


def _settings(*, vocal=True, sample_rate=16000):
    return SimpleNamespace(
        download_format="bestvideo+bestaudio/best", download_proxy=None,
        download_socket_timeout=30, download_retries=5,
        download_concurrent_fragments=4, merge_output_format="mp4", cookies_file=None,
        audio_sample_rate=sample_rate, audio_channels=1,
        vocal_separation_enabled=vocal, vocal_separation_backend="demucs",
        vocal_separation_model="htdemucs", vocal_separation_threads=1,
        vocal_separation_command="python -m demucs.separate", vocal_separation_timeout=30,
        transcriber_backend="local_whisper", transcriber_url=None,
        transcriber_api_key=None, transcriber_timeout=90,
        local_whisper_beam_size=5, translate_batch_size=8,
        local_translate_batch_size=4,
    )


def _plugin_defaults(plugin_ids=None, versions=None):
    plugin_ids = plugin_ids or {}
    versions = versions or {}
    return {
        stage: {
            "plugin_id": plugin_ids.get(stage, f"fixture.{stage}"),
            "implementation_version": versions.get(stage, "1"),
        }
        for stage in STAGE_SPECS
    }


def _plan(params, *, plugin_ids=None, versions=None, runtime_settings=None):
    engine = SimpleNamespace(
        id=params.engine, api_type="openai_compatible",
        base_url="https://translate.example.test/v1", model="fixture-model", api_key=None,
    )
    raw = resolve_plugin_plan(
        source_type=params.source_type,
        source_lang=params.source_lang,
        target_lang=params.target_lang,
        mode=params.mode,
        burn=params.burn,
        model=params.model,
        engine=params.engine,
        need_subtitle=params.need_subtitle,
        quality=params.quality,
        tts_enabled=params.tts_enabled,
        tts_voice=params.tts_voice,
        original_voice_mode=params.original_voice_mode,
        settings=runtime_settings or _settings(),
        engines=SimpleNamespace(get=lambda engine_id: engine),
        plugin_defaults=_plugin_defaults(plugin_ids, versions),
    )
    return parse_plugin_plan(raw)


def _manager(plugin_ids=None, faults=None, versions=None):
    plugin_ids = plugin_ids or {}
    faults = faults or {}
    versions = versions or {}
    registry = PluginRegistry()
    factories = {}
    for stage, (manifest_stage, _) in STAGE_SPECS.items():
        plugin_id = plugin_ids.get(stage, f"fixture.{stage}")
        factory = ScenarioFactory(
            plugin_id, stage, fault=faults.get(stage), version=versions.get(stage, "1")
        )
        registry.register(factory.manifest, factory)
        factories[plugin_id] = factory
    return PluginManager(registry), factories


def _params(task_id, **overrides):
    values = dict(
        task_id=task_id, url="https://example.test/video", source_lang="en",
        target_lang="zh-CN", mode="mono", burn="soft", model="local:tiny",
        engine="fixture-engine", source_type="upload", need_subtitle=True,
        tts_enabled=False, tts_voice="auto", original_voice_mode="keep",
    )
    values.update(overrides)
    return orchestrator.PipelineParams(**values)


def _invoke(
    monkeypatch, tmp_path, params, *, plugin_ids=None, faults=None, plan=None,
    capture_cancellation=False, capture_errors=False, legacy_cache=False,
):
    from src.config import storage

    data_root = tmp_path / "data"
    data_root.mkdir(exist_ok=True)
    task_dir = data_root / params.task_id
    task_dir.mkdir(exist_ok=True)
    if params.source_type == "upload":
        _mp4(task_dir / "source.mp4")
    runtime_settings = dataclasses.replace(settings, _data_dir=data_root)
    monkeypatch.setattr(storage, "settings", runtime_settings)
    manager, factories = _manager(plugin_ids, faults)
    plan = plan or _plan(params, plugin_ids=plugin_ids)
    events = []
    try:
        result = orchestrator.run_pipeline(
            params, events.append,
            plugin_manager=manager,
            plugin_plan=plan,
            credential_resolver=lambda ref: "fixture-secret",
            legacy_cache=legacy_cache,
        )
    except orchestrator.PipelineCancelledError as exc:
        if not capture_cancellation:
            raise
        result = exc
    except Exception as exc:
        if not capture_errors:
            raise
        result = exc
    return result, events, factories, manager, plan, data_root / params.task_id


def _upload_source(task_dir: Path) -> None:
    _mp4(task_dir / "source.mp4")


@pytest.mark.parametrize("burn", ["hard", "soft"])
@pytest.mark.parametrize("mode", ["mono", "bilingual"])
def test_frozen_plan_runs_mono_bilingual_and_hard_soft(monkeypatch, tmp_path, burn, mode):
    params = _params(f"mode-{burn}-{mode}", burn=burn, mode=mode)
    result, events, factories, _, plan, task_dir = _invoke(monkeypatch, tmp_path, params)
    assert result.status == "SUCCESS"
    requests = factories[plan["slots"]["translate"]["plugin_id"]].requests
    assert requests[0].mode == mode
    subtitle_request = factories[plan["slots"]["render.subtitle"]["plugin_id"]].requests[0]
    assert subtitle_request.mode == burn
    assert Path(result.outputs["video"]).is_file()
    assert events[-1].status == "SUCCESS"


def test_only_download_never_creates_other_sessions_and_upload_skips_download(monkeypatch, tmp_path):
    only_download = _params("only-download", source_type="url", need_subtitle=False)
    _, events, factories, _, plan, _ = _invoke(monkeypatch, tmp_path, only_download)
    assert events[-1].outputs.keys() == {"video"}
    assert factories[plan["slots"]["download"]["plugin_id"]].created == 1
    assert all(factory.created == 0 for key, factory in factories.items() if key != "fixture.download")

    upload = _params("upload-only", tts_enabled=False)
    result, _, factories, _, plan, _ = _invoke(monkeypatch, tmp_path, upload)
    assert result.status == "SUCCESS"
    assert "download" not in plan["slots"]
    assert factories["fixture.download"].created == 0


def test_separation_purpose_outputs_are_isolated_and_keep_skips_dubbing_separation(
    monkeypatch, tmp_path
):
    # Execution follows the queued plan even if the live setting changes later.
    monkeypatch.setattr(
        orchestrator,
        "settings",
        dataclasses.replace(orchestrator.settings, vocal_separation_enabled=False),
    )
    params = _params(
        "purpose-isolation", tts_enabled=True, original_voice_mode="lower",
    )
    enabled_plan = _plan(params, runtime_settings=_settings(vocal=True))
    result, _, factories, _, plan, task_dir = _invoke(
        monkeypatch, tmp_path, params, plan=enabled_plan,
    )
    assert result.status == "SUCCESS"
    separate_factory = factories[plan["slots"]["separate.asr"]["plugin_id"]]
    assert [request.purpose for request in separate_factory.requests] == [
        "separate.asr", "separate.dubbing",
    ]
    asr_request, dubbing_request = separate_factory.requests
    assert asr_request.vocal_path.name == "vocal.wav"
    assert asr_request.background_path is None
    assert dubbing_request.background_path.name == "background.wav"
    assert dubbing_request.vocal_path.name == "vocal_stem.wav"
    assert (task_dir / "vocal.wav") != (task_dir / "vocal_stem.wav")

    keep = _params("purpose-keep", tts_enabled=True, original_voice_mode="keep")
    keep_plan_arg = _plan(keep, runtime_settings=_settings(vocal=True))
    keep_result, _, keep_factories, _, keep_plan, _ = _invoke(
        monkeypatch, tmp_path, keep, plan=keep_plan_arg,
    )
    assert keep_result.status == "SUCCESS"
    assert "separate.dubbing" not in keep_plan["slots"]
    assert [request.purpose for request in keep_factories["fixture.vocal_separate"].requests] == [
        "separate.asr",
    ]


def test_disabled_plan_skips_asr_separation_even_if_live_setting_is_enabled(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        orchestrator,
        "settings",
        dataclasses.replace(orchestrator.settings, vocal_separation_enabled=True),
    )
    params = _params("purpose-disabled-plan")
    disabled_plan = _plan(params, runtime_settings=_settings(vocal=False))
    assert "separate.asr" not in disabled_plan["slots"]

    result, _, factories, _, _, _ = _invoke(
        monkeypatch, tmp_path, params, plan=disabled_plan,
    )
    assert result.status == "SUCCESS"
    assert factories["fixture.vocal_separate"].requests == []


def test_legacy_cache_does_not_adopt_metadata_less_artifact_for_custom_plugin(
    monkeypatch, tmp_path
):
    params = _params("legacy-custom-plugin", tts_enabled=False)
    task_dir = tmp_path / "data" / params.task_id
    task_dir.mkdir(parents=True)
    _upload_source(task_dir)
    _wav(task_dir / "audio.wav")
    assert not (task_dir / "extract.asr.plugin-meta.json").exists()

    plan = _plan(params, runtime_settings=_settings(vocal=False))
    result, _, factories, _, _, _ = _invoke(
        monkeypatch, tmp_path, params, plan=plan, legacy_cache=True,
    )
    assert result.status == "SUCCESS"
    assert factories["fixture.audio_extract"].created == 1


@pytest.mark.parametrize("purpose", [
    "extract.dubbing", "separate.dubbing", "tts", "render.dub_mux",
])
@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_dubbing_failures_preserve_published_subtitle_video_and_never_publish_fake_dubbed_video(
    monkeypatch, tmp_path, purpose, failure
):
    params = _params(
        f"dub-{purpose}-{failure}", tts_enabled=True, original_voice_mode="lower",
    )
    fault_stage = {
        "extract.dubbing": "audio_extract",
        "separate.dubbing": "vocal_separate",
        "tts": "tts",
        "render.dub_mux": "render",
    }[purpose]
    result, events, factories, _, plan, task_dir = _invoke(
        monkeypatch, tmp_path, params, faults={fault_stage: (purpose, failure)},
        capture_cancellation=(failure == "cancel"),
    )
    assert (task_dir / ("output_hard.mp4" if params.burn == "hard" else "output_soft.mp4")).is_file()
    assert (task_dir / "translated.srt").is_file()
    assert all("dubbedVideo" not in (event.outputs or {}) for event in events)
    if failure == "error":
        assert result.status == "SUCCESS"
        assert result.outputs.get("video")
        assert "dubbedVideo" not in result.outputs
        assert any(event.tts_status == "FAILED" for event in events)
    else:
        assert isinstance(result, orchestrator.PipelineCancelledError)
        assert not any(event.status == "SUCCESS" for event in events)
    expected_render_sessions = 2 if purpose == "render.dub_mux" else 1
    assert factories[plan["slots"]["render.subtitle"]["plugin_id"]].created == expected_render_sessions


def test_retry_reuses_completed_stage_and_upstream_identity_change_invalidates_downstream(
    monkeypatch, tmp_path
):
    params = _params("cache-retry", source_type="upload", tts_enabled=False)
    task_dir = tmp_path / "data" / params.task_id
    task_dir.mkdir(parents=True)
    _upload_source(task_dir)
    plan = _plan(params, runtime_settings=_settings(vocal=False))
    first, _, factories, manager, _, _ = _invoke(monkeypatch, tmp_path, params, plan=plan)
    counts = {key: factory.created for key, factory in factories.items()}
    second_events = []
    second = orchestrator.run_pipeline(
        params, second_events.append, plugin_manager=manager, plugin_plan=plan,
        credential_resolver=lambda ref: "fixture-secret", legacy_cache=False,
    )
    assert first.status == second.status == "SUCCESS"
    assert {key: factory.created for key, factory in factories.items()} == counts

    changed_ids = {"audio_extract": "fixture.audio_extract.next"}
    new_plan = _plan(params, plugin_ids=changed_ids, runtime_settings=_settings(vocal=False))
    changed_factory = ScenarioFactory("fixture.audio_extract.next", "audio_extract")
    manager._registry.register(changed_factory.manifest, changed_factory)
    factories[changed_factory.plugin_id] = changed_factory
    before_translate = factories["fixture.translate"].created
    orchestrator.run_pipeline(
        params, lambda event: None, plugin_manager=manager, plugin_plan=new_plan,
        credential_resolver=lambda ref: "fixture-secret", legacy_cache=False,
    )
    assert factories["fixture.translate"].created > before_translate


def test_retry_after_translation_failure_reuses_completed_upstream_stages(monkeypatch, tmp_path):
    params = _params("stage-retry", source_type="upload", tts_enabled=False)
    plan = _plan(params, runtime_settings=_settings(vocal=False))
    first, _, factories, manager, _, _ = _invoke(
        monkeypatch, tmp_path, params,
        faults={"translate": ("translate", "error")}, plan=plan,
        capture_errors=True,
    )
    assert isinstance(first, PluginError)
    assert factories["fixture.audio_extract"].created == 1
    assert factories["fixture.transcribe"].created == 1
    factories["fixture.translate"].fault = None
    events = []
    retried = orchestrator.run_pipeline(
        params, events.append, plugin_manager=manager, plugin_plan=plan,
        credential_resolver=lambda ref: "fixture-secret", legacy_cache=False,
    )
    assert retried.status == "SUCCESS"
    assert factories["fixture.audio_extract"].created == 1
    assert factories["fixture.transcribe"].created == 1
    assert factories["fixture.translate"].created == 2


@pytest.mark.parametrize("change", ["plugin_id", "implementation_version", "config"])
def test_upstream_change_invalidates_downstream_even_when_output_bytes_are_identical(
    monkeypatch, tmp_path, change
):
    params = _params(f"provenance-{change}", source_type="upload", tts_enabled=False)
    baseline_settings = _settings(vocal=False)
    first_plan = _plan(params, runtime_settings=baseline_settings)
    first, _, factories, manager, _, task_dir = _invoke(
        monkeypatch, tmp_path, params, plan=first_plan,
    )
    assert first.status == "SUCCESS"
    original_audio = (task_dir / "audio.wav").read_bytes()
    before_translation = factories["fixture.translate"].created

    if change == "plugin_id":
        new_factory = ScenarioFactory("fixture.audio_extract.next", "audio_extract")
        manager._registry.register(new_factory.manifest, new_factory)
        factories[new_factory.plugin_id] = new_factory
        next_plan = _plan(
            params, plugin_ids={"audio_extract": new_factory.plugin_id},
            runtime_settings=baseline_settings,
        )
        next_manager = manager
        next_factories = factories
    elif change == "implementation_version":
        next_manager, next_factories = _manager(versions={"audio_extract": "2"})
        next_plan = _plan(
            params, versions={"audio_extract": "2"}, runtime_settings=baseline_settings,
        )
    else:
        next_manager = manager
        next_factories = factories
        next_plan = _plan(
            params, runtime_settings=_settings(vocal=False, sample_rate=22050),
        )

    rerun = orchestrator.run_pipeline(
        params, lambda event: None, plugin_manager=next_manager, plugin_plan=next_plan,
        credential_resolver=lambda ref: "fixture-secret", legacy_cache=False,
    )
    assert rerun.status == "SUCCESS"
    assert (task_dir / "audio.wav").read_bytes() == original_audio
    if change == "implementation_version":
        assert next_factories["fixture.translate"].created == 1
    else:
        assert next_factories["fixture.translate"].created > before_translation


@pytest.mark.parametrize("failure", ["missing_plugin", "version_mismatch"])
def test_missing_plugin_or_version_mismatch_fails_without_default_fallback(
    monkeypatch, tmp_path, failure
):
    params = _params("version-mismatch", source_type="upload")
    task_dir = tmp_path / "data" / params.task_id
    task_dir.mkdir(parents=True)
    _upload_source(task_dir)
    plugin_ids = {
        "transcribe": "fixture.missing-transcriber"
        if failure == "missing_plugin" else "fixture.transcribe"
    }
    plan = _plan(params, plugin_ids=plugin_ids, runtime_settings=_settings(vocal=False))
    slot = dict(plan["slots"]["transcribe"])
    if failure == "version_mismatch":
        slot["implementation_version"] = "2"
    broken = {**plan, "slots": {**plan["slots"], "transcribe": slot}}
    with pytest.raises(PluginError) as raised:
        _invoke(monkeypatch, tmp_path, params, plan=broken)
    expected_code = "plugin_unavailable" if failure == "missing_plugin" else "version_mismatch"
    assert raised.value.code == expected_code
