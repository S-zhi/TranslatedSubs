"""Real FFmpeg acceptance for subtitle and dubbing artifact interchange."""

import array
import dataclasses
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import settings
from src.plugin_system import DubMuxRequest, FakeContext, SubtitleRenderRequest
from src.plugin_system import AudioExtractRequest, TTSRequest, VocalSeparateRequest
from src.plugin_system.manager import PluginManager
from src.plugin_system.registry import PluginRegistry
from src.plugins.builtin import audio_ffmpeg, tts_kokoro, vocal_demucs
from src.plugins.builtin import subtitle_ffmpeg
from src.service.plugin_plan import parse_plugin_plan, resolve_plugin_plan


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="FFmpeg tools are required")


def _run(args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _tone(path, frequency, amplitude, sample_rate=48000):
    _run([
        FFMPEG, "-y", "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=1:sample_rate={sample_rate}",
        "-filter:a", f"volume={amplitude}", "-ac", "1", "-c:a", "pcm_s16le", str(path),
    ])


def _rms(path):
    decoded = subprocess.run([
        FFMPEG, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "48000", "pipe:1",
    ], check=True, capture_output=True).stdout
    samples = array.array("f")
    samples.frombytes(decoded[: len(decoded) - len(decoded) % samples.itemsize])
    return (sum(value * value for value in samples) / len(samples)) ** 0.5


def _probe(path):
    result = subprocess.run([
        FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
        "-of", "json", str(path),
    ], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)["streams"]


def test_real_subtitle_and_dub_artifacts_interoperate(tmp_path, monkeypatch):
    task_dir = tmp_path / "task-real"
    task_dir.mkdir()
    monkeypatch.setattr(subtitle_ffmpeg, "settings", dataclasses.replace(
        settings, _ffmpeg_bin=FFMPEG, _ffprobe_bin=FFPROBE,
    ))
    monkeypatch.setattr(subtitle_ffmpeg.subtitle_burner, "settings", subtitle_ffmpeg.settings)
    monkeypatch.setattr(subtitle_ffmpeg.subtitle_burner, "ensure_task_dir", lambda task_id: task_dir)

    source_video = task_dir / "source.mp4"
    _run([
        FFMPEG, "-y", "-f", "lavfi", "-i", "color=c=black:s=160x120:r=25:d=1",
        "-f", "lavfi", "-i", "sine=frequency=330:duration=1:sample_rate=48000",
        "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(source_video),
    ])
    subtitle = task_dir / "translated.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,800\nhello\n", encoding="utf-8")
    context = FakeContext(task_id="task-real", task_dir=task_dir, purpose="render.subtitle")
    session = subtitle_ffmpeg.factory.create_session({"mode": "hard"}, context)

    hard = task_dir / "output_hard.mp4"
    hard_result = session.run(SubtitleRenderRequest(
        stage="render", operation="subtitle", purpose="render.subtitle",
        source_video=source_video, translated_srt=subtitle,
        output_video=hard, mode="hard",
    ))
    assert hard_result.video_path == hard and hard.stat().st_size > 0
    soft = task_dir / "output_soft.mp4"
    session = subtitle_ffmpeg.factory.create_session({"mode": "soft"}, context)
    soft_result = session.run(SubtitleRenderRequest(
        stage="render", operation="subtitle", purpose="render.subtitle",
        source_video=source_video, translated_srt=subtitle,
        output_video=soft, mode="soft",
    ))
    assert soft_result.video_path == soft
    assert any(stream["codec_type"] == "subtitle" for stream in _probe(soft))

    source_audio = task_dir / "tts_source.wav"
    background = task_dir / "background.wav"
    vocal = task_dir / "vocal_stem.wav"
    speech = task_dir / "dubbed.wav"
    _tone(source_audio, 440, 0.5)
    _tone(background, 220, 0.2)
    _tone(vocal, 880, 0.4)
    _tone(speech, 1320, 0.05)

    measured = {}
    for mode in ("keep", "lower", "replace"):
        context = FakeContext(task_id="task-real", task_dir=task_dir, purpose="render.dub_mux")
        session = subtitle_ffmpeg.factory.create_session(
            {"original_voice_mode": mode}, context
        )
        request = DubMuxRequest(
            stage="render", operation="dub_mux", purpose="render.dub_mux",
            base_video=soft, source_audio=source_audio, speech_audio=speech,
            background_audio=None if mode == "keep" else background,
            vocal_audio=vocal if mode == "lower" else None,
            mixed_audio=task_dir / "dubbed_mix.wav",
            output_video=task_dir / f"output_dubbed_{mode}.mp4",
            original_voice_mode=mode,
        )
        # The contract uses stable task artifact names; use a single requested output per run.
        request = dataclasses.replace(
            request,
            output_video=task_dir / "output_dubbed.mp4",
        )
        result = session.run(request)
        assert result.video_path == request.output_video
        measured[mode] = _rms(result.mixed_audio)
        streams = _probe(result.video_path)
        assert any(stream["codec_type"] == "video" for stream in streams)
        assert any(stream["codec_type"] == "audio" for stream in streams)
        assert any(stream["codec_type"] == "subtitle" for stream in streams)
    assert measured["keep"] > measured["lower"] > measured["replace"]
    assert measured["keep"] > measured["replace"] * 1.5


def test_official_adapters_interoperate_through_plugin_manager(tmp_path, monkeypatch):
    """Use official A2/A3/A6/A7 adapters, Manager validation, real WAV and FFmpeg.

    Demucs and Kokoro inference are stubbed at their expensive backend boundary;
    the adapters, typed contracts, WAV checks, audio extraction, and FFmpeg muxes
    all remain real.
    """
    task_dir = tmp_path / "task-manager-interop"
    task_dir.mkdir()
    runtime_settings = dataclasses.replace(settings, _ffmpeg_bin=FFMPEG, _ffprobe_bin=FFPROBE)
    monkeypatch.setattr(audio_ffmpeg.audio_extractor, "settings", runtime_settings)
    monkeypatch.setattr(audio_ffmpeg.audio_extractor, "ensure_task_dir", lambda task_id: task_dir)
    monkeypatch.setattr(subtitle_ffmpeg, "settings", runtime_settings)
    monkeypatch.setattr(subtitle_ffmpeg.subtitle_burner, "settings", runtime_settings)
    monkeypatch.setattr(subtitle_ffmpeg.subtitle_burner, "ensure_task_dir", lambda task_id: task_dir)
    monkeypatch.setattr(vocal_demucs.vocal_separator, "settings", runtime_settings)

    source_video = task_dir / "source.mp4"
    _run([
        FFMPEG, "-y", "-f", "lavfi", "-i", "color=c=black:s=160x120:r=25:d=1",
        "-f", "lavfi", "-i", "sine=frequency=330:duration=1:sample_rate=48000",
        "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(source_video),
    ])
    original_srt = task_dir / "original.srt"
    translated_srt = task_dir / "translated.srt"
    original_srt.write_text("1\n00:00:00,000 --> 00:00:00,800\nhello\n", encoding="utf-8")
    translated_srt.write_text("1\n00:00:00,000 --> 00:00:00,800\nhola\n", encoding="utf-8")

    def fake_demucs_or_normalize(cmd, **kwargs):
        if "--two-stems=vocals" in cmd:
            output_root = Path(cmd[cmd.index("-o") + 1]) / "htdemucs" / Path(cmd[-1]).stem
            output_root.mkdir(parents=True)
            _tone(output_root / "vocals.wav", 880, 0.4)
            _tone(output_root / "no_vocals.wav", 220, 0.2)
            return 0, "", ""
        completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
        return completed.returncode, completed.stdout, completed.stderr

    monkeypatch.setattr(vocal_demucs.vocal_separator, "_run_external", fake_demucs_or_normalize)

    def fake_tts(translated, original, output, **kwargs):
        _tone(Path(output), 1320, 0.05, sample_rate=tts_kokoro.tts_core.SAMPLE_RATE)
        return Path(output)

    monkeypatch.setattr(tts_kokoro.tts_core, "generate_timeline_audio", fake_tts)

    def frozen_slots(voice_mode):
        configured = SimpleNamespace(
            vocal_separation_enabled=True,
            vocal_separation_backend="demucs",
            vocal_separation_model="htdemucs",
            vocal_separation_threads=1,
            vocal_separation_command="python -m demucs.separate",
            vocal_separation_timeout=15,
            transcriber_backend="local_whisper",
            local_whisper_beam_size=5,
            translate_batch_size=20,
            local_translate_batch_size=8,
        )
        engine = SimpleNamespace(
            id="test-engine", api_type="openai_compatible",
            base_url="https://translate.example.test/v1", model="test-model",
            api_key=None,
        )
        raw_plan = resolve_plugin_plan(
            source_type="upload", source_lang="en", target_lang="zh-CN",
            mode="mono", burn="soft", model="local:tiny", engine="test-engine",
            need_subtitle=True, quality=None, tts_enabled=True,
            tts_voice="af_maple", original_voice_mode=voice_mode,
            settings=configured, engines=SimpleNamespace(get=lambda engine_id: engine),
            plugin_defaults={},
        )
        return parse_plugin_plan(raw_plan)["slots"]

    registry = PluginRegistry()
    for factory in (audio_ffmpeg.factory, vocal_demucs.factory, tts_kokoro.factory, subtitle_ffmpeg.factory):
        registry.register(factory.manifest, factory)
    manager = PluginManager(registry)

    def invoke(slot, request):
        context = FakeContext(
            task_id="task-manager-interop", task_dir=task_dir, purpose=request.purpose,
        )
        return manager.run(
            slot["plugin_id"], slot["implementation_version"],
            request, slot["config"], context,
        )

    slots = frozen_slots("lower")
    asr_audio = task_dir / "audio.wav"
    asr_extracted = invoke(
        slots["extract.asr"],
        AudioExtractRequest(
            stage="audio_extract", operation="audio_extract", purpose="extract.asr",
            source_path=source_video, output_path=asr_audio, sample_rate=48000, channels=2,
        ),
    )
    dub_source = task_dir / "tts_source.wav"
    dub_extracted = invoke(
        slots["extract.dubbing"],
        AudioExtractRequest(
            stage="audio_extract", operation="audio_extract", purpose="extract.dubbing",
            source_path=source_video, output_path=dub_source, sample_rate=48000, channels=2,
        ),
    )
    assert asr_extracted.audio_path == asr_audio
    assert dub_extracted.audio_path == dub_source
    assert asr_audio != dub_source and asr_audio.stat().st_size > 44

    asr_vocal = task_dir / "vocal.wav"
    asr_stems = invoke(
        slots["separate.asr"],
        VocalSeparateRequest(
            stage="vocal_separate", operation="vocal_separate", purpose="separate.asr",
            audio_path=asr_audio, vocal_path=asr_vocal, background_path=None,
        ),
    )
    background = task_dir / "background.wav"
    vocal = task_dir / "vocal_stem.wav"
    dub_stems = invoke(
        slots["separate.dubbing"],
        VocalSeparateRequest(
            stage="vocal_separate", operation="vocal_separate", purpose="separate.dubbing",
            audio_path=dub_source, vocal_path=vocal, background_path=background,
        ),
    )
    assert asr_stems.vocal_path == asr_vocal and asr_stems.background_path is None
    assert dub_stems.vocal_path == vocal and dub_stems.background_path == background
    assert (task_dir / "separation.asr.meta.json").is_file()
    assert (task_dir / "separation.dubbing.meta.json").is_file()
    assert asr_vocal != vocal

    tts_audio = task_dir / "dubbed.wav"
    tts_result = invoke(
        slots["tts"],
        TTSRequest(
            stage="tts", operation="tts", purpose="tts",
            translated_srt=translated_srt, original_srt=original_srt,
            output_audio=tts_audio, target_lang="zh-CN", voice="af_maple",
            bilingual=False, duration_seconds=1.0,
        ),
    )
    assert tts_result.speech_audio == tts_audio
    assert tts_audio.stat().st_size > 44

    soft_video = task_dir / "output_soft.mp4"
    subtitle_result = invoke(
        slots["render.subtitle"],
        SubtitleRenderRequest(
            stage="render", operation="subtitle", purpose="render.subtitle",
            source_video=source_video, translated_srt=translated_srt,
            output_video=soft_video, mode="soft",
        ),
    )
    assert subtitle_result.video_path == soft_video
    assert any(stream["codec_type"] == "subtitle" for stream in _probe(soft_video))

    measured = {}
    for mode in ("keep", "lower", "replace"):
        mode_slots = frozen_slots(mode)
        result = invoke(
            mode_slots["render.dub_mux"],
            DubMuxRequest(
                stage="render", operation="dub_mux", purpose="render.dub_mux",
                base_video=soft_video, source_audio=dub_source, speech_audio=tts_audio,
                background_audio=None if mode == "keep" else background,
                vocal_audio=vocal if mode == "lower" else None,
                mixed_audio=task_dir / "dubbed_mix.wav",
                output_video=task_dir / "output_dubbed.mp4",
                original_voice_mode=mode,
            ),
        )
        measured[mode] = _rms(result.mixed_audio)
        streams = _probe(result.video_path)
        assert any(stream["codec_type"] == "video" for stream in streams)
        assert any(stream["codec_type"] == "audio" for stream in streams)
        assert any(stream["codec_type"] == "subtitle" for stream in streams)
    assert measured["keep"] > measured["lower"] > measured["replace"]
