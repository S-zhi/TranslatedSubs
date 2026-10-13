"""Full-pipeline smoke with the production manager and built-in adapters."""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import wave
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import settings, storage
from src.core import transcriber as transcriber_core
from src.core import translator as translator_core
from src.plugin_system.manager import PluginManager
from src.plugin_system.registry import PluginRegistry
from src.plugins.builtin import factories as builtin_factories
from src.plugins.builtin import audio_ffmpeg, downloader_ytdlp, subtitle_ffmpeg, transcriber_whisper
from src.plugins.builtin import translator_engine, tts_kokoro, vocal_demucs
from src.service import orchestrator
from src.service.plugin_plan import parse_plugin_plan, resolve_plugin_plan


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="FFmpeg tools are required")


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _write_wav(path: Path, *, rate: int, channels: int, frequency: int = 440) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run([
        FFMPEG, "-y", "-f", "lavfi", "-i",
        f"sine=frequency={frequency}:duration=1:sample_rate={rate}",
        "-ac", str(channels), "-c:a", "pcm_s16le", str(path),
    ])


def _probe(path: Path) -> list[dict[str, str]]:
    completed = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(completed.stdout)["streams"]


class _CountingSession:
    def __init__(self, inner, counts, requests):
        self._inner = inner
        self._counts = counts
        self._requests = requests

    def validate(self, request):
        return self._inner.validate(request)

    def run(self, request):
        self._counts[request.purpose] += 1
        self._requests[request.purpose].append(request)
        return self._inner.run(request)

    def close(self):
        return self._inner.close()


class _CountingFactory:
    def __init__(self, inner, counts, requests):
        self._inner = inner
        self.manifest = inner.manifest
        self.stage = inner.stage
        self._counts = counts
        self._requests = requests

    def create_session(self, config, context):
        return _CountingSession(
            self._inner.create_session(config, context), self._counts, self._requests,
        )


@pytest.mark.parametrize("source_type", ["url", "upload"])
def test_run_pipeline_with_official_adapters_auto_voice_and_cache_reuse(
    tmp_path, monkeypatch, source_type
):
    task_id = f"official-adapters-smoke-{source_type}"
    task_dir = tmp_path / "data" / task_id
    task_dir.mkdir(parents=True)
    runtime_settings = dataclasses.replace(
        settings, _data_dir=tmp_path / "data", _ffmpeg_bin=FFMPEG,
        _ffprobe_bin=FFPROBE,
    )
    monkeypatch.setattr(storage, "settings", runtime_settings)
    monkeypatch.setattr(audio_ffmpeg.audio_extractor, "settings", runtime_settings)
    monkeypatch.setattr(audio_ffmpeg.audio_extractor, "ensure_task_dir", lambda _task: task_dir)
    monkeypatch.setattr(subtitle_ffmpeg, "settings", runtime_settings)
    monkeypatch.setattr(subtitle_ffmpeg.subtitle_burner, "settings", runtime_settings)
    monkeypatch.setattr(subtitle_ffmpeg.subtitle_burner, "ensure_task_dir", lambda _task: task_dir)
    monkeypatch.setattr(vocal_demucs.vocal_separator, "settings", runtime_settings)

    fixture_video = tmp_path / "fixture-source.mp4"
    _run([
        FFMPEG, "-y", "-f", "lavfi", "-i", "color=c=black:s=160x120:r=25:d=1",
        "-f", "lavfi", "-i", "sine=frequency=330:duration=1:sample_rate=48000",
        "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(fixture_video),
    ])
    if source_type == "upload":
        shutil.copy2(fixture_video, task_dir / "source.mp4")

    def fake_download(url, download_task_id, **kwargs):
        destination = task_dir / "source.mp4"
        shutil.copy2(fixture_video, destination)
        return downloader_ytdlp.downloader.DownloadResult(
            video_path=destination, title="local fixture", duration=1.0,
            ext="mp4", filesize=destination.stat().st_size, width=160, height=120,
            source_url=url,
        )

    monkeypatch.setattr(downloader_ytdlp.downloader, "download_video", fake_download)

    def fake_demucs_or_normalize(command, **kwargs):
        if "--two-stems=vocals" in command:
            output_root = Path(command[command.index("-o") + 1]) / "htdemucs" / Path(command[-1]).stem
            output_root.mkdir(parents=True)
            _write_wav(output_root / "vocals.wav", rate=48000, channels=2, frequency=880)
            _write_wav(output_root / "no_vocals.wav", rate=48000, channels=2, frequency=220)
            return 0, "", ""
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        return completed.returncode, completed.stdout, completed.stderr

    monkeypatch.setattr(vocal_demucs.vocal_separator, "_run_external", fake_demucs_or_normalize)

    def fake_transcribe(audio_path, output_task_id, **kwargs):
        output = task_dir / "original.srt"
        output.write_text("1\n00:00:00,000 --> 00:00:00,800\nhello\n", encoding="utf-8")
        return transcriber_core.TranscribeResult(
            srt_path=output, language="en", language_probability=1.0,
            segment_count=1, duration=1.0,
        )

    monkeypatch.setattr(transcriber_whisper.transcriber, "transcribe", fake_transcribe)
    monkeypatch.setattr(
        transcriber_whisper.transcriber, "LocalWhisperTranscriber",
        lambda **_kwargs: object(),
    )

    def fake_translate(srt_path, output_task_id, source_lang, target_lang, **kwargs):
        output = task_dir / "translated.srt"
        output.write_text("1\n00:00:00,000 --> 00:00:00,800\nhola\n", encoding="utf-8")
        return translator_core.TranslateResult(srt_path=output, count=1, bilingual=False)

    monkeypatch.setattr(translator_engine.translator, "translate_srt", fake_translate)
    generated_voice = []

    def fake_tts(translated_srt, original_srt, output_audio, **kwargs):
        generated_voice.append(kwargs["voice"])
        _write_wav(
            Path(output_audio), rate=tts_kokoro.tts_core.SAMPLE_RATE,
            channels=1, frequency=1200,
        )
        return Path(output_audio)

    monkeypatch.setattr(tts_kokoro.tts_core, "generate_timeline_audio", fake_tts)

    plan_settings = SimpleNamespace(
        audio_sample_rate=16000, audio_channels=1,
        vocal_separation_enabled=True, vocal_separation_backend="demucs",
        vocal_separation_model="htdemucs", vocal_separation_threads=1,
        vocal_separation_command="python -m demucs.separate", vocal_separation_timeout=30,
        transcriber_backend="local_whisper", transcriber_timeout=90,
        local_whisper_beam_size=5, translate_batch_size=12,
        local_translate_batch_size=6,
    )
    engine = SimpleNamespace(
        id="fixture-engine", api_type="openai_compatible",
        base_url="https://translate.example.test/v1", model="fixture-model", api_key=None,
    )
    params = orchestrator.PipelineParams(
        task_id=task_id, url="https://fixture.example.test/video",
        source_lang="en", target_lang="zh-CN",
        mode="mono", burn="soft", model="local:tiny", engine="fixture-engine",
        source_type=source_type, need_subtitle=True, tts_enabled=True,
        tts_voice="auto", original_voice_mode="lower",
    )
    frozen_plan = parse_plugin_plan(resolve_plugin_plan(
        source_type=params.source_type, source_lang=params.source_lang,
        target_lang=params.target_lang, mode=params.mode, burn=params.burn,
        model=params.model, engine=params.engine, need_subtitle=params.need_subtitle,
        quality=params.quality, tts_enabled=params.tts_enabled, tts_voice=params.tts_voice,
        original_voice_mode=params.original_voice_mode, settings=plan_settings,
        engines=SimpleNamespace(get=lambda _engine_id: engine), plugin_defaults={},
    ))
    assert frozen_plan["slots"]["tts"]["config"]["voice"] == "auto"
    assert "separate.asr" in frozen_plan["slots"]

    counts: Counter[str] = Counter()
    requests: dict[str, list] = {purpose: [] for purpose in frozen_plan["slots"]}
    registry = PluginRegistry()
    for factory in builtin_factories():
        wrapped = _CountingFactory(factory, counts, requests)
        registry.register(wrapped.manifest, wrapped)
    manager = PluginManager(registry)

    events = []
    first = orchestrator.run_pipeline(
        params, events.append, plugin_manager=manager, plugin_plan=frozen_plan,
        credential_resolver=lambda _ref: "fixture-secret", legacy_cache=False,
    )
    assert first.status == "SUCCESS"
    assert Path(first.outputs["video"]).is_file()
    assert Path(first.outputs["dubbedVideo"]).is_file()
    assert Path(first.outputs["subtitle"]).is_file()
    assert generated_voice == ["zf_001"]
    assert {"video", "audio", "subtitle"}.issubset(
        {stream["codec_type"] for stream in _probe(Path(first.outputs["dubbedVideo"]))}
    )
    assert set(counts) == set(frozen_plan["slots"])
    assert all(value == 1 for value in counts.values())
    assert counts["download"] == (1 if source_type == "url" else 0)

    initial_counts = counts.copy()
    second_events = []
    second = orchestrator.run_pipeline(
        params, second_events.append, plugin_manager=manager, plugin_plan=frozen_plan,
        credential_resolver=lambda _ref: "fixture-secret", legacy_cache=False,
    )
    assert second.status == "SUCCESS"
    assert counts == initial_counts
    assert generated_voice == ["zf_001"]
    assert second.outputs == first.outputs
