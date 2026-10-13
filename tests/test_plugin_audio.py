import wave
from pathlib import Path

import pytest

from src.core import audio_extractor
from src.core.audio_extractor import AudioResult
from src.plugin_system import (
    AudioExtractRequest,
    FakeContext,
    PluginCancelled,
    PluginError,
)
from src.plugin_system.manager import PluginManager
from src.plugin_system.registry import PluginRegistry
from src.plugins.builtin import audio_ffmpeg


def _write_wav(path: Path, sample_rate: int, channels: int, duration: float = 0.2):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\0\0" * channels * int(sample_rate * duration))


def _request(task_dir: Path, purpose="extract.dubbing"):
    return AudioExtractRequest(
        stage="audio_extract",
        operation="audio_extract",
        purpose=purpose,
        source_path=task_dir / "source.mp4",
        output_path=task_dir / ("audio.wav" if purpose == "extract.asr" else "tts_source.wav"),
        sample_rate=48000 if purpose == "extract.dubbing" else 16000,
        channels=2 if purpose == "extract.dubbing" else 1,
    )


def _config(request):
    return {"sample_rate": request.sample_rate, "channels": request.channels}


def _manager():
    registry = PluginRegistry()
    registry.register(audio_ffmpeg.factory.manifest, audio_ffmpeg.factory)
    return PluginManager(registry)


def test_factory_is_lazy_and_declares_audio_plugin_identity(tmp_path):
    context = FakeContext(task_dir=tmp_path, purpose="extract.dubbing")
    session = audio_ffmpeg.factory.create_session(
        {"sample_rate": 48000, "channels": 2}, context
    )
    assert audio_ffmpeg.factory.manifest.plugin_id == "builtin.ffmpeg_audio"
    assert audio_ffmpeg.factory.manifest.implementation_version == "1"
    assert session._closed is False


@pytest.mark.parametrize("purpose", ["extract.asr", "extract.dubbing"])
def test_manager_routes_to_the_reserved_output_and_returns_real_wav_metadata(
    tmp_path, monkeypatch, purpose
):
    request = _request(tmp_path, purpose)
    request.source_path.write_bytes(b"video")
    original = tmp_path / "audio.wav"
    original.write_bytes(b"existing ASR output")
    calls = []

    def fake_extract(video_path, task_id, on_progress=None, **kwargs):
        calls.append((video_path, task_id, kwargs))
        output = Path(kwargs["output_path"])
        _write_wav(output, kwargs["sample_rate"], kwargs["channels"])
        on_progress(audio_extractor.AudioProgress(25, 0.1, 0.4))
        with wave.open(str(output), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
        return AudioResult(output, kwargs["sample_rate"], kwargs["channels"], duration, output.stat().st_size)

    monkeypatch.setattr(audio_ffmpeg.audio_extractor, "extract_audio", fake_extract)
    context = FakeContext(task_id="task-a2", task_dir=tmp_path, purpose=purpose)
    manager = _manager()
    result = manager.run(
        "builtin.ffmpeg_audio", "1", request, _config(request), context
    )

    assert result.audio_path == request.output_path
    assert result.sample_rate == request.sample_rate
    assert result.channels == request.channels
    assert result.duration_seconds == pytest.approx(0.2)
    assert calls[0][1] == "task-a2"
    assert calls[0][2]["output_path"] == request.output_path
    assert context.progress == [25]
    if purpose == "extract.dubbing":
        assert original.read_bytes() == b"existing ASR output"


def test_manager_maps_context_cancellation_and_cleans_new_partial_audio(
    tmp_path, monkeypatch
):
    request = _request(tmp_path)
    request.source_path.write_bytes(b"video")
    context = FakeContext(task_id="task-a2", task_dir=tmp_path, purpose="extract.dubbing")

    def fake_extract(video_path, task_id, on_progress=None, **kwargs):
        output = Path(kwargs["output_path"])
        output.write_bytes(b"partial")
        context.cancelled = True
        on_progress(audio_extractor.AudioProgress(35, 0.1, 0.4))
        return AudioResult(output, 48000, 2, 0.2, 7)

    monkeypatch.setattr(audio_ffmpeg.audio_extractor, "extract_audio", fake_extract)
    manager = _manager()
    with pytest.raises(PluginCancelled):
        manager.run("builtin.ffmpeg_audio", "1", request, _config(request), context)
    assert not request.output_path.exists()


def test_request_and_frozen_config_must_match_before_extraction(tmp_path, monkeypatch):
    request = _request(tmp_path)
    request.source_path.write_bytes(b"video")
    calls = []
    monkeypatch.setattr(audio_ffmpeg.audio_extractor, "extract_audio", lambda *a, **k: calls.append(1))
    manager = _manager()

    with pytest.raises(PluginError) as raised:
        manager.run(
            "builtin.ffmpeg_audio", "1", request,
            {"sample_rate": 44100, "channels": 2},
            FakeContext(task_id="task-a2", task_dir=tmp_path, purpose="extract.dubbing"),
        )
    assert raised.value.code == "invalid_input"
    assert calls == []


def test_core_failure_is_sanitized_and_keeps_original_cause(tmp_path, monkeypatch):
    request = _request(tmp_path)
    request.source_path.write_bytes(b"video")
    failure = audio_extractor.AudioExtractError("decoder failed for secret-path")
    monkeypatch.setattr(
        audio_ffmpeg.audio_extractor,
        "extract_audio",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )
    with pytest.raises(PluginError) as raised:
        _manager().run(
            "builtin.ffmpeg_audio", "1", request, _config(request),
            FakeContext(task_id="task-a2", task_dir=tmp_path, purpose="extract.dubbing"),
        )
    assert raised.value.code == "execution_error"
    assert raised.value.cause is failure
    assert raised.value.__cause__ is failure
    assert "secret-path" not in raised.value.message
