import wave
from pathlib import Path

import pytest

from src.core import tts as tts_core
from src.plugin_system import FakeContext, PluginCancelled, PluginError, TTSRequest, TTSResult
from src.plugin_system.manager import PluginManager
from src.plugin_system.registry import PluginRegistry
from src.plugins.builtin import tts_kokoro


def _request(task_dir: Path, *, target_lang="zh-CN", voice="auto", bilingual=True):
    original = task_dir / "original.srt"
    translated = task_dir / "translated.srt"
    original.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8"
    )
    translated.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n你好\n", encoding="utf-8"
    )
    return TTSRequest(
        stage="tts",
        operation="tts",
        purpose="tts",
        translated_srt=translated,
        original_srt=original,
        output_audio=task_dir / "dubbed.wav",
        target_lang=target_lang,
        voice=voice,
        bilingual=bilingual,
        duration_seconds=2.0,
    )


def _config(request):
    return {
        "voice": request.voice,
        "target_lang": request.target_lang,
        "bilingual": request.bilingual,
    }


def _manager():
    registry = PluginRegistry()
    registry.register(tts_kokoro.factory.manifest, tts_kokoro.factory)
    return PluginManager(registry)


def _write_wav(path: Path, *, rate=24000, channels=1, frames=24000):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x01\0" * channels * frames)


def test_factory_is_lightweight_and_declares_builtin_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tts_core, "_pipeline", lambda language: pytest.fail("model loaded during factory creation")
    )
    context = FakeContext(task_dir=tmp_path, purpose="tts")
    session = tts_kokoro.factory.create_session(
        {"voice": "auto", "target_lang": "zh-CN", "bilingual": True}, context
    )
    assert tts_kokoro.factory.manifest.plugin_id == "builtin.kokoro"
    assert tts_kokoro.factory.manifest.implementation_version == "1"
    assert session._closed is False


@pytest.mark.parametrize(
    ("target_lang", "expected_voice"),
    [("zh-CN", "zf_001"), ("en", "af_maple"), ("fr", "af_maple")],
)
def test_manager_uses_auto_voice_mapping_and_passes_timeline_semantics(
    tmp_path, monkeypatch, target_lang, expected_voice
):
    request = _request(tmp_path, target_lang=target_lang)
    calls = []

    def fake_generate(translated_srt, original_srt, output_path, **kwargs):
        calls.append((translated_srt, original_srt, output_path, kwargs))
        _write_wav(output_path)
        kwargs["on_progress"](36.5)
        return Path(output_path)

    monkeypatch.setattr(tts_kokoro.tts_core, "generate_timeline_audio", fake_generate)
    context = FakeContext(task_id="task-a6", task_dir=tmp_path, purpose="tts")
    result = _manager().run(
        "builtin.kokoro", "1", request, _config(request), context
    )

    assert result == TTSResult(tmp_path / "dubbed.wav", 1.0)
    assert calls[0][0] == request.translated_srt
    assert calls[0][1] == request.original_srt
    assert calls[0][2] == request.output_audio
    assert calls[0][3]["voice"] == expected_voice
    assert calls[0][3]["bilingual"] is True
    assert calls[0][3]["duration"] == 2.0
    assert calls[0][3]["task_id"] == "task-a6"
    assert context.progress == [36.5]


def test_explicit_voice_is_preserved(tmp_path, monkeypatch):
    request = _request(tmp_path, target_lang="zh", voice="zf_custom")
    observed = {}

    def fake_generate(*args, **kwargs):
        observed.update(kwargs)
        _write_wav(args[2])
        return Path(args[2])

    monkeypatch.setattr(tts_kokoro.tts_core, "generate_timeline_audio", fake_generate)
    _manager().run(
        "builtin.kokoro", "1", request, _config(request),
        FakeContext(task_id="task-a6", task_dir=tmp_path, purpose="tts"),
    )
    assert observed["voice"] == "zf_custom"


def test_config_mismatch_fails_before_loading_tts_core(tmp_path, monkeypatch):
    request = _request(tmp_path)
    called = []
    monkeypatch.setattr(
        tts_kokoro.tts_core, "generate_timeline_audio", lambda *a, **k: called.append(True)
    )
    config = _config(request)
    config["bilingual"] = False
    with pytest.raises(PluginError) as raised:
        _manager().run(
            "builtin.kokoro", "1", request, config,
            FakeContext(task_id="task-a6", task_dir=tmp_path, purpose="tts"),
        )
    assert raised.value.code == "invalid_input"
    assert called == []


def test_generation_error_has_safe_message_and_preserves_cause(tmp_path, monkeypatch):
    request = _request(tmp_path)
    failure = RuntimeError("provider echoed API_KEY=secret-value")
    monkeypatch.setattr(
        tts_kokoro.tts_core,
        "generate_timeline_audio",
        lambda *a, **k: (_ for _ in ()).throw(failure),
    )
    with pytest.raises(PluginError) as raised:
        _manager().run(
            "builtin.kokoro", "1", request, _config(request),
            FakeContext(task_id="task-a6", task_dir=tmp_path, purpose="tts"),
        )
    assert raised.value.code == "execution_error"
    assert raised.value.cause is failure
    assert raised.value.__cause__ is failure
    assert "secret-value" not in raised.value.message


def test_missing_dependency_wrapped_by_tts_core_maps_to_dependency_missing(
    tmp_path, monkeypatch
):
    request = _request(tmp_path)
    try:
        raise ImportError("numpy unavailable")
    except ImportError as import_error:
        failure = tts_core.TTSError("Kokoro could not start")
        failure.__cause__ = import_error
    monkeypatch.setattr(
        tts_kokoro.tts_core,
        "generate_timeline_audio",
        lambda *a, **k: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(PluginError) as raised:
        _manager().run(
            "builtin.kokoro", "1", request, _config(request),
            FakeContext(task_id="task-a6", task_dir=tmp_path, purpose="tts"),
        )
    assert raised.value.code == "dependency_missing"
    assert raised.value.cause is failure


@pytest.mark.parametrize("wrapped", [False, True])
def test_cancellation_is_not_wrapped_and_removes_partial_output(
    tmp_path, monkeypatch, wrapped
):
    request = _request(tmp_path)
    context = FakeContext(task_id="task-a6", task_dir=tmp_path, purpose="tts")

    def cancel(*args, **kwargs):
        request.output_audio.write_bytes(b"partial")
        if wrapped:
            try:
                raise PluginCancelled("cancelled")
            except PluginCancelled as exc:
                raise tts_core.TTSError("generation aborted") from exc
        context.cancelled = True
        kwargs["cancel_check"]()

    monkeypatch.setattr(tts_kokoro.tts_core, "generate_timeline_audio", cancel)
    with pytest.raises(PluginCancelled):
        _manager().run("builtin.kokoro", "1", request, _config(request), context)
    assert not request.output_audio.exists()


def test_invalid_wav_is_rejected_and_partial_output_removed(tmp_path, monkeypatch):
    request = _request(tmp_path)

    def wrong_format(*args, **kwargs):
        _write_wav(args[2], rate=16000, channels=2)
        return Path(args[2])

    monkeypatch.setattr(tts_kokoro.tts_core, "generate_timeline_audio", wrong_format)
    with pytest.raises(PluginError) as raised:
        _manager().run(
            "builtin.kokoro", "1", request, _config(request),
            FakeContext(task_id="task-a6", task_dir=tmp_path, purpose="tts"),
        )
    assert raised.value.code == "invalid_result"
    assert not request.output_audio.exists()
