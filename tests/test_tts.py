from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest

from src.core import tts
from src.service.orchestrator import (
    PipelineCancelledError,
    PipelineContext,
    PipelineParams,
    SubtitleBurningHandler,
)
from src.config import settings
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


def test_tts_generation_failure_keeps_regular_video_success_path(tmp_path, monkeypatch):
    import src.config as config_module
    events = []
    params = PipelineParams(
        task_id="task_tts_error", url="u", source_lang="en", target_lang="zh-CN",
        tts_enabled=True,
    )
    context = PipelineContext(params=params, on_event=events.append)
    context.resources.video_path = tmp_path / "source.mp4"
    context.resources.translated_srt_path = tmp_path / "translated.srt"
    context.resources.original_srt_path = tmp_path / "original.srt"
    context.resources.output_video_path = tmp_path / "output_hard.mp4"
    monkeypatch.setattr(config_module, "task_dir", lambda _: tmp_path)
    monkeypatch.setattr("src.service.orchestrator.run_ffmpeg", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "src.service.orchestrator.generate_timeline_audio",
        lambda *args, **kwargs: (_ for _ in ()).throw(tts.TTSError("weights unavailable")),
    )
    outputs = {"video": str(context.resources.output_video_path)}

    SubtitleBurningHandler()._add_dubbed_video(context, outputs)

    assert outputs == {"video": str(context.resources.output_video_path)}
    assert events[-1].tts_status == "FAILED"
    assert "weights unavailable" in events[-1].tts_error


@pytest.mark.parametrize(
    ("mode", "expected_input_names", "expected_filters", "expected_amix"),
    [
        ("keep", ["tts_source.wav", "speech.wav"], [], "amix=inputs=2"),
        ("lower", ["background.wav", "vocal_stem.wav", "speech.wav"],
         ["[1:a]volume=0.25[vocal]"], "amix=inputs=3"),
        ("replace", ["background.wav", "speech.wav"], [], "amix=inputs=2"),
    ],
)
def test_dubbed_mix_uses_expected_original_voice_stems(
    tmp_path, monkeypatch, mode, expected_input_names, expected_filters, expected_amix,
):
    import src.config as config_module
    import src.core.vocal_separator as separator
    from src.service import orchestrator

    events = []
    context = PipelineContext(
        params=PipelineParams(
            task_id=f"task_tts_{mode}", url="u", source_lang="en", target_lang="zh-CN",
            tts_enabled=True, original_voice_mode=mode,
        ),
        on_event=events.append,
    )
    context.resources.video_path = tmp_path / "source.mp4"
    context.resources.audio_path = tmp_path / "audio.wav"
    context.resources.original_srt_path = tmp_path / "original.srt"
    context.resources.translated_srt_path = tmp_path / "translated.srt"
    context.resources.output_video_path = tmp_path / "output_hard.mp4"
    (tmp_path / "vocal_stem.wav").write_bytes(b"stem")
    background = tmp_path / "background.wav"
    background.write_bytes(b"background")
    speech = tmp_path / "speech.wav"
    speech.write_bytes(b"speech")
    commands = []
    monkeypatch.setattr(config_module, "task_dir", lambda _: tmp_path)
    monkeypatch.setattr(orchestrator, "run_ffmpeg", lambda command, **kwargs: commands.append(command))
    monkeypatch.setattr(orchestrator, "probe_duration", lambda *args: 2.0)
    monkeypatch.setattr(orchestrator, "generate_timeline_audio", lambda *args, **kwargs: speech)
    monkeypatch.setattr(separator, "separate_background", lambda *args, **kwargs: background)

    SubtitleBurningHandler()._add_dubbed_video(context, {"video": str(context.resources.output_video_path)})

    mix_command = next(command for command in commands if "-filter_complex" in command)
    input_names = [Path(mix_command[index + 1]).name for index, token in enumerate(mix_command[:-1]) if token == "-i"]
    filter_graph = mix_command[mix_command.index("-filter_complex") + 1]
    assert input_names == expected_input_names
    assert expected_amix in filter_graph
    for expected in expected_filters:
        assert expected in filter_graph
    if mode == "replace":
        assert "[1:a]volume=0.25[vocal]" not in filter_graph


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
