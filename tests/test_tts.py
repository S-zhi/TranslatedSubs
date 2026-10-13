import dataclasses
import wave
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest

from src.core import tts
from src.service.orchestrator import (
    PipelineCancelledError,
    PipelineHandler,
    PipelineParams,
    SubtitleBurningHandler,
)
from src.config import settings, storage
from src.core import audio_extractor, subtitle_burner, vocal_separator
from src.plugin_system.manager import PluginManager
from src.plugin_system.registry import PluginRegistry
from src.plugins.builtin import audio_ffmpeg, subtitle_ffmpeg, tts_kokoro, vocal_demucs
from src.service import orchestrator
from src.service.plugin_plan import parse_plugin_plan, resolve_plugin_plan
from src.core.ffmpeg_utils import run_ffmpeg


def _srt(path: Path, text: str) -> Path:
    path.write_text(f"1\n00:00:00,500 --> 00:00:01,500\n{text}\n", encoding="utf-8")
    return path


def test_bilingual_tts_only_reads_translation_and_places_audio_on_timeline(tmp_path, monkeypatch):
    seen = []

    class FakePipeline:
        def __call__(self, text, **kwargs):
            seen.append(text)
            yield SimpleNamespace(audio=np.full(2400, 0.1, dtype=np.float32))

    monkeypatch.setattr(tts, "_pipeline", lambda language: FakePipeline())
    original = _srt(tmp_path / "original.srt", "Hello world")
    translated = _srt(tmp_path / "translated.srt", "Hello world\n你好世界")
    output = tmp_path / "dubbed.wav"

    tts.generate_timeline_audio(
        translated, original, output, target_lang="zh-CN", voice="zf_001",
        bilingual=True, duration=2.0,
    )

    assert seen == ["你好世界"]
    with tts.wave.open(str(output), "rb") as wav:
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        assert wav.getframerate() == 24000
        assert len(samples) == 2 * 24000
    assert np.max(np.abs(samples[:12000])) == 0
    assert np.max(np.abs(samples[12000:36000])) > 0
    assert np.max(np.abs(samples[36000:])) == 0


def test_bilingual_tts_rejects_unrecognized_single_line_prefix(tmp_path):
    with pytest.raises(tts.TTSError, match="原文前缀"):
        tts._text_for_translation("some unrelated line", "original line", True)


def test_long_unpunctuated_text_is_split_with_a_hard_limit():
    segments = tts._split_sentences("字" * 601, "zh")
    assert len(segments) > 1
    assert all(0 < len(segment) <= 120 for segment in segments)


def test_tts_cancellation_is_not_wrapped_as_generation_failure(tmp_path, monkeypatch):
    class FakePipeline:
        def __call__(self, text, **kwargs):
            yield SimpleNamespace(audio=np.ones(100, dtype=np.float32))

    monkeypatch.setattr(tts, "_pipeline", lambda language: FakePipeline())
    source = _srt(tmp_path / "original.srt", "source")
    target = _srt(tmp_path / "translated.srt", "First sentence. Second sentence.")

    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        if calls == 3:  # cancel on the second generated sentence
            raise PipelineCancelledError("cancel")

    with pytest.raises(PipelineCancelledError):
        tts.generate_timeline_audio(
            target, source, tmp_path / "cancel.wav", target_lang="en", voice="af_maple",
            cancel_check=cancel,
        )


class _SeedSubtitleInputs(PipelineHandler):
    """Seed completed upstream artifacts before exercising the render handler."""

    def __init__(self, task_dir: Path):
        super().__init__()
        self._task_dir = task_dir

    def process(self, context):
        context.resources.video_path = self._task_dir / "source.mp4"
        context.resources.original_srt_path = self._task_dir / "original.srt"
        context.resources.translated_srt_path = self._task_dir / "translated.srt"


def _run_formal_dub_pipeline(tmp_path, monkeypatch, mode, *, fail_tts=False):
    from src.plugins.builtin import factories as builtin_factories

    task_id = f"plugin-dub-{mode}-{'error' if fail_tts else 'ok'}"
    data_root = tmp_path / "data"
    task_dir = data_root / task_id
    task_dir.mkdir(parents=True)
    runtime_settings = dataclasses.replace(settings, _data_dir=data_root)
    monkeypatch.setattr(storage, "settings", runtime_settings)

    (task_dir / "source.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2")
    _srt(task_dir / "original.srt", "original text")
    _srt(task_dir / "translated.srt", "translated text")
    # The custom handler seeds completed upstream outputs; provide provenance
    # records so the normal plugin input fingerprint path remains enforced.
    (task_dir / "transcribe.plugin-meta.json").write_text("{}", encoding="utf-8")
    (task_dir / "translate.plugin-meta.json").write_text("{}", encoding="utf-8")

    def fake_extract(video_path, output_task_id, *, sample_rate, channels, output_path, **kwargs):
        with wave.open(str(output_path), "wb") as output:
            output.setnchannels(channels)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(b"\x01\x00" * channels * sample_rate)
        return audio_extractor.AudioResult(
            audio_path=output_path, sample_rate=sample_rate, channels=channels,
            duration=1.0, filesize=output_path.stat().st_size,
        )

    monkeypatch.setattr(audio_ffmpeg.audio_extractor, "extract_audio", fake_extract)

    def fake_burn(video_path, srt_path, burn_task_id, *, mode, **kwargs):
        output_path = task_dir / f"output_{mode}.mp4"
        output_path.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2")
        return subtitle_burner.BurnResult(
            output_path=output_path, mode=mode, filesize=output_path.stat().st_size,
        )

    monkeypatch.setattr(subtitle_ffmpeg.subtitle_burner, "burn_subtitles", fake_burn)

    def fake_tts(translated, original, output_path, **kwargs):
        if fail_tts:
            raise tts.TTSError("weights unavailable")
        with wave.open(str(output_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(b"\x02\x00" * 24000)
        return output_path

    monkeypatch.setattr(tts_kokoro.tts_core, "generate_timeline_audio", fake_tts)

    def fake_separate(audio_path, separation_task_id, *, purpose, vocal_path,
                      background_path, **kwargs):
        for path in (vocal_path, background_path):
            if path is None:
                continue
            with wave.open(str(path), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(48000)
                output.writeframes(b"\x03\x00" * 2 * 48000)
        return vocal_separator.SeparationOutputs(
            vocal_path=vocal_path, background_path=background_path,
        )

    monkeypatch.setattr(vocal_demucs.vocal_separator, "separate_stems", fake_separate)

    commands = []

    def fake_ffmpeg(command, on_progress=None, *, task_id=None, **kwargs):
        commands.append(command)
        output_path = Path(command[-1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.suffix == ".wav":
            with wave.open(str(output_path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(48000)
                output.writeframes(b"\x01\x00" * 48000)
        else:
            output_path.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2")

    monkeypatch.setattr(subtitle_ffmpeg.ffmpeg_utils, "run_ffmpeg", fake_ffmpeg)

    plan_settings = SimpleNamespace(
        vocal_separation_enabled=True,
        vocal_separation_backend="demucs", vocal_separation_model="htdemucs",
        vocal_separation_threads=1, vocal_separation_command="python -m demucs.separate",
        vocal_separation_timeout=30,
    )
    engine = SimpleNamespace(
        id="fixture-engine", api_type="openai_compatible",
        base_url="https://translate.example.test/v1", model="fixture-model", api_key=None,
    )
    params = PipelineParams(
        task_id=task_id, url="", source_lang="en", target_lang="zh-CN",
        mode="mono", burn="soft", model="local:tiny", engine="fixture-engine",
        source_type="upload", need_subtitle=True, tts_enabled=True,
        tts_voice="auto", original_voice_mode=mode,
    )
    plan = parse_plugin_plan(resolve_plugin_plan(
        source_type="upload", source_lang="en", target_lang="zh-CN", mode="mono",
        burn="soft", model="local:tiny", engine="fixture-engine", need_subtitle=True,
        quality=None, tts_enabled=True, tts_voice="auto", original_voice_mode=mode,
        settings=plan_settings, engines=SimpleNamespace(get=lambda _engine_id: engine),
        plugin_defaults={},
    ))
    registry = PluginRegistry()
    for factory in builtin_factories():
        registry.register(factory.manifest, factory)
    manager = PluginManager(registry)
    events = []
    handler_chain = _SeedSubtitleInputs(task_dir)
    handler_chain.set_next(SubtitleBurningHandler())
    result = orchestrator.run_pipeline(
        params, events.append, plugin_manager=manager, plugin_plan=plan,
        credential_resolver=lambda _ref: "fixture-secret", legacy_cache=False,
        handler_chain=handler_chain,
    )
    if result.status != "SUCCESS":
        raise AssertionError([(event.current_step, event.error) for event in events])
    return result, events, commands, task_dir


def test_tts_generation_failure_keeps_regular_video_success_path(tmp_path, monkeypatch):
    result, events, _, task_dir = _run_formal_dub_pipeline(
        tmp_path, monkeypatch, "keep", fail_tts=True,
    )

    assert result.status == "SUCCESS"
    assert result.outputs["video"] == str(task_dir / "output_soft.mp4")
    assert result.outputs["subtitle"] == str(task_dir / "translated.srt")
    assert "dubbedVideo" not in result.outputs
    failure = next(event for event in events if event.tts_status == "FAILED")
    assert failure.tts_error
    assert "weights unavailable" not in failure.tts_error


@pytest.mark.parametrize(
    ("mode", "expected_input_names", "expected_filters", "expected_amix"),
    [
        ("keep", ["tts_source.wav", "dubbed.wav"], [], "amix=inputs=2"),
        ("lower", ["background.wav", "vocal_stem.wav", "dubbed.wav"],
         ["[1:a]volume=0.25[vocal]"], "amix=inputs=3"),
        ("replace", ["background.wav", "dubbed.wav"], [], "amix=inputs=2"),
    ],
)
def test_dubbed_mix_uses_expected_original_voice_stems(
    tmp_path, monkeypatch, mode, expected_input_names, expected_filters, expected_amix,
):
    result, events, commands, task_dir = _run_formal_dub_pipeline(
        tmp_path, monkeypatch, mode,
    )
    mix_command = next(command for command in commands if "-filter_complex" in command)
    input_names = [Path(mix_command[index + 1]).name for index, token in enumerate(mix_command[:-1]) if token == "-i"]
    filter_graph = mix_command[mix_command.index("-filter_complex") + 1]
    assert input_names == expected_input_names
    assert expected_amix in filter_graph
    for expected in expected_filters:
        assert expected in filter_graph
    if mode == "replace":
        assert "[1:a]volume=0.25[vocal]" not in filter_graph
    assert result.status == "SUCCESS"
    assert result.outputs["dubbedVideo"] == str(task_dir / "output_dubbed.mp4")
    assert not any(event.tts_status == "FAILED" for event in events)


def test_overlong_fake_kokoro_audio_is_compressed_without_losing_tail(tmp_path, monkeypatch):
    class FakePipeline:
        def __call__(self, text, **kwargs):
            yield SimpleNamespace(audio=np.linspace(-0.5, 0.5, 96000, dtype=np.float32))

    commands = []

    def fake_atempo(command, **kwargs):
        commands.append(command)
        source = Path(command[command.index("-i") + 1])
        target = Path(command[-1])
        with tts.wave.open(str(source), "rb") as wav:
            values = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        # Simulate the expected 4x duration reduction from chained atempo.
        tts._write_wav(target, values[::4].astype(np.float32) / 32768)

    monkeypatch.setattr(tts, "_pipeline", lambda language: FakePipeline())
    monkeypatch.setattr(tts, "run_ffmpeg", fake_atempo)
    source = _srt(tmp_path / "original.srt", "source")
    target = _srt(tmp_path / "translated.srt", "A long translated sentence.")
    output = tmp_path / "compressed.wav"

    tts.generate_timeline_audio(
        target, source, output, target_lang="en", voice="af_maple",
        duration=2.0,
    )

    assert commands
    atempo_filter = commands[0][commands[0].index("-filter:a") + 1]
    assert atempo_filter == "atempo=2.0000,atempo=2.0000"
    with tts.wave.open(str(output), "rb") as wav:
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    assert len(samples) == 2 * 24000
    # The generated segment's final samples survived the time compression.
    assert samples[12000 + 23900] > 10000


@pytest.mark.skipif(not shutil.which(settings.ffmpeg_bin), reason="FFmpeg not installed")
def test_ffmpeg_dubbed_mux_keeps_soft_subtitle_stream(tmp_path):
    captions = _srt(tmp_path / "captions.srt", "Sample caption")
    base = tmp_path / "base.mp4"
    dubbed_audio = tmp_path / "dubbed.wav"
    output = tmp_path / "output_dubbed.mp4"
    subprocess.run([
        settings.ffmpeg_bin, "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=2:r=24",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-i", str(captions), "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0",
        "-c:v", "mpeg4", "-c:a", "aac", "-c:s", "mov_text", "-t", "2", str(base),
    ], check=True, capture_output=True)
    tts._write_wav(dubbed_audio, np.full(24000 * 2, 0.05, dtype=np.float32))

    run_ffmpeg([
        settings.ffmpeg_bin, "-y", "-i", str(base), "-i", str(dubbed_audio),
        "-map", "0:v:0", "-map", "1:a:0", "-map", "0:s?", "-c:v", "copy",
        "-c:a", "aac", "-c:s", "copy", "-progress", "pipe:1", "-nostats",
        "-loglevel", "error", str(output),
    ])
    probe = subprocess.run([
        settings.ffprobe_bin, "-v", "error", "-show_entries", "stream=codec_type",
        "-of", "csv=p=0", str(output),
    ], check=True, capture_output=True, text=True)
    assert sorted(probe.stdout.splitlines()) == ["audio", "subtitle", "video"]
