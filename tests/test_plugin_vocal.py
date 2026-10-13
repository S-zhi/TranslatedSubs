"""Built-in Demucs plugin tests with real WAV containers and mocked subprocesses."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from src.core import vocal_separator
from src.plugin_system import FakeContext, PluginCancelled, PluginError, VocalSeparateRequest
from src.plugins.builtin import vocal_demucs


def _write_wav(path: Path, *, sample_rate: int = 48000, channels: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * channels * 64)


def _config(**overrides):
    config = {
        "backend": "demucs",
        "model": "htdemucs",
        "threads": 3,
        "command": "python -m demucs.separate",
        "timeout_seconds": 77,
    }
    config.update(overrides)
    return config


def _request(task_dir: Path, *, purpose: str, vocal: bool, background: bool) -> VocalSeparateRequest:
    asr = purpose == "separate.asr"
    return VocalSeparateRequest(
        stage="vocal_separate",
        operation="vocal_separate",
        purpose=purpose,
        audio_path=task_dir / ("audio.wav" if asr else "tts_source.wav"),
        vocal_path=(task_dir / ("vocal.wav" if asr else "vocal_stem.wav")) if vocal else None,
        background_path=(task_dir / "background.wav") if background else None,
    )


def _fake_demucs(monkeypatch, *, omit_background: bool = False):
    calls = []
    registrations = []

    def fake_external(
        command, *, timeout, task_id, cancel_check=None, threads=None,
        register_process=None, unregister_process=None,
    ):
        calls.append({
            "command": command,
            "timeout": timeout,
            "task_id": task_id,
            "threads": threads,
        })
        if register_process is not None:
            proc = object()
            register_process(proc)
            registrations.append(proc)
            unregister_process(proc)
        if "--two-stems=vocals" in command:
            work_dir = Path(command[command.index("-o") + 1])
            source = Path(command[-1])
            stem_root = work_dir / "htdemucs" / source.stem
            _write_wav(stem_root / "vocals.wav")
            if not omit_background:
                _write_wav(stem_root / "no_vocals.wav")
        else:
            _write_wav(Path(command[-1]), sample_rate=16000, channels=1)
        return 0, "", ""

    monkeypatch.setattr(vocal_separator, "_run_external", fake_external)
    return calls, registrations


def test_asr_plugin_uses_frozen_config_and_outputs_real_16k_mono_wav(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_wav(task_dir / "audio.wav")
    monkeypatch.setattr(
        vocal_separator,
        "settings",
        type("Settings", (), {
            "vocal_separation_backend": "unexpected",
            "vocal_separation_model": "changed-model",
            "vocal_separation_threads": 99,
            "vocal_separation_command": "wrong-command",
            "vocal_separation_timeout": 1,
            "ffmpeg_bin": "ffmpeg",
        })(),
    )
    calls, registrations = _fake_demucs(monkeypatch)
    context = FakeContext(task_id="task", task_dir=task_dir, purpose="separate.asr")
    session = vocal_demucs.factory.create_session(_config(), context)
    request = _request(task_dir, purpose="separate.asr", vocal=True, background=False)

    result = session.run(request)

    assert result.vocal_path == (task_dir / "vocal.wav").resolve()
    assert result.background_path is None
    with wave.open(str(result.vocal_path), "rb") as output:
        assert output.getframerate() == 16000
        assert output.getnchannels() == 1
    demucs = calls[0]
    assert demucs["command"][:3] == ["python", "-m", "demucs.separate"]
    assert demucs["command"][demucs["command"].index("-n") + 1] == "htdemucs"
    assert demucs["command"][demucs["command"].index("--jobs") + 1] == "3"
    assert demucs["threads"] == 3
    assert demucs["timeout"] == 77
    assert len(registrations) == 2  # Demucs and the ASR WAV normalization process.
    assert context.registered_processes == []
    assert context.unregistered_processes == registrations
    session.close()


def test_dubbing_replace_outputs_only_background_without_touching_asr_vocal(
    tmp_path, monkeypatch
):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_wav(task_dir / "tts_source.wav")
    existing_asr_vocal = task_dir / "vocal.wav"
    _write_wav(existing_asr_vocal, sample_rate=16000, channels=1)
    before = existing_asr_vocal.read_bytes()
    _fake_demucs(monkeypatch)
    context = FakeContext(task_id="task", task_dir=task_dir, purpose="separate.dubbing")
    session = vocal_demucs.factory.create_session(_config(), context)
    request = _request(task_dir, purpose="separate.dubbing", vocal=False, background=True)

    result = session.run(request)

    assert result.vocal_path is None
    assert result.background_path == (task_dir / "background.wav").resolve()
    with wave.open(str(result.background_path), "rb") as output:
        assert output.getframerate() == 48000
        assert output.getnchannels() == 2
    assert existing_asr_vocal.read_bytes() == before
    assert not (task_dir / "vocal_stem.wav").exists()
    session.close()


def test_dubbing_lower_requires_and_publishes_both_requested_stems(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_wav(task_dir / "tts_source.wav")
    calls, _ = _fake_demucs(monkeypatch)
    context = FakeContext(task_id="task", task_dir=task_dir, purpose="separate.dubbing")
    session = vocal_demucs.factory.create_session(_config(), context)
    request = _request(task_dir, purpose="separate.dubbing", vocal=True, background=True)

    result = session.run(request)

    assert result.vocal_path == (task_dir / "vocal_stem.wav").resolve()
    assert result.background_path == (task_dir / "background.wav").resolve()
    assert len(calls) == 1
    with wave.open(str(result.vocal_path), "rb") as output:
        assert output.getframerate() == 48000
        assert output.getnchannels() == 2
    session.close()


def test_cache_fingerprint_includes_source_purpose_and_config(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    source = task_dir / "audio.wav"
    _write_wav(source)
    calls, _ = _fake_demucs(monkeypatch)
    request = _request(task_dir, purpose="separate.asr", vocal=True, background=False)

    for invocation_id in ("invoke-one", "invoke-two"):
        session = vocal_demucs.factory.create_session(
            _config(),
            FakeContext(
                task_id="task", invocation_id=invocation_id,
                task_dir=task_dir, purpose="separate.asr",
            ),
        )
        session.run(request)
        session.close()
    assert len(calls) == 2  # Second call reused the exact plan/input cache.

    changed = vocal_demucs.factory.create_session(
        _config(model="htdemucs_ft"),
        FakeContext(
            task_id="task", invocation_id="invoke-three",
            task_dir=task_dir, purpose="separate.asr",
        ),
    )
    changed.run(request)
    changed.close()
    assert len(calls) == 4  # Config change requires Demucs plus normalization.

    with source.open("ab") as stream:
        stream.write(b"source changed")
    changed_input = vocal_demucs.factory.create_session(
        _config(model="htdemucs_ft"),
        FakeContext(
            task_id="task", invocation_id="invoke-four",
            task_dir=task_dir, purpose="separate.asr",
        ),
    )
    changed_input.run(request)
    changed_input.close()
    assert len(calls) == 6


def test_missing_requested_background_fails_and_never_publishes_partial_stems(
    tmp_path, monkeypatch
):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_wav(task_dir / "tts_source.wav")
    calls, _ = _fake_demucs(monkeypatch, omit_background=True)
    context = FakeContext(task_id="task", task_dir=task_dir, purpose="separate.dubbing")
    session = vocal_demucs.factory.create_session(_config(), context)
    request = _request(task_dir, purpose="separate.dubbing", vocal=True, background=True)

    with pytest.raises(PluginError) as failure:
        session.run(request)
    assert failure.value.code == "execution_error"
    assert calls[0]["command"]
    assert not (task_dir / "background.wav").exists()
    assert not (task_dir / "vocal_stem.wav").exists()
    assert not list((task_dir / ".vocal-separation").rglob("vocals.wav"))
    session.close()


def test_cancellation_is_preserved_as_plugin_control_flow(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_wav(task_dir / "audio.wav")
    monkeypatch.setattr(
        vocal_separator,
        "_run_external",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            vocal_separator.VocalSeparationCancelledError("cancelled")
        ),
    )
    context = FakeContext(task_id="task", task_dir=task_dir, purpose="separate.asr")
    session = vocal_demucs.factory.create_session(_config(), context)
    with pytest.raises(PluginCancelled):
        session.run(_request(task_dir, purpose="separate.asr", vocal=True, background=False))
    session.close()


@pytest.mark.parametrize("missing_error", [FileNotFoundError("python"), ImportError("demucs")])
def test_missing_demucs_dependency_has_actionable_stable_error(tmp_path, monkeypatch, missing_error):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_wav(task_dir / "audio.wav")

    def missing_dependency(*_args, **_kwargs):
        raise vocal_separator.VocalSeparationError("external process failed") from missing_error

    monkeypatch.setattr(vocal_separator, "separate_stems", missing_dependency)
    context = FakeContext(task_id="task", task_dir=task_dir, purpose="separate.asr")
    session = vocal_demucs.factory.create_session(_config(), context)
    with pytest.raises(PluginError) as raised:
        session.run(_request(task_dir, purpose="separate.asr", vocal=True, background=False))

    assert raised.value.code == "dependency_missing"
    assert "install demucs" in raised.value.message
    assert "python" not in raised.value.message
    assert "demucs" in raised.value.message
    session.close()


def test_semaphore_wait_checks_cancellation(tmp_path, monkeypatch):
    class BusySemaphore:
        def acquire(self, timeout):
            return False

        def release(self):
            raise AssertionError("not acquired")

    monkeypatch.setattr(vocal_separator, "_SEPARATION_SLOT", BusySemaphore())
    calls = 0

    def cancel_after_first_check():
        nonlocal calls
        calls += 1
        if calls >= 1:
            raise PluginCancelled("cancelled while waiting")

    with pytest.raises(PluginCancelled):
        vocal_separator.separate_stems(
            tmp_path / "missing.wav",
            "task",
            purpose="separate.asr",
            vocal_path=tmp_path / "task" / "vocal.wav",
            background_path=None,
            backend="demucs",
            model="htdemucs",
            threads=2,
            command="python -m demucs.separate",
            timeout_seconds=60,
            invocation_id="wait-test",
            cancel_check=cancel_after_first_check,
        )
