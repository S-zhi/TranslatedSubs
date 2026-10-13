"""CPU 人声分离封装的单元测试。

Demucs 和 ffmpeg 都是外部依赖，因此测试只验证命令拼接、错误包装和产物
校验，不下载模型也不启动真实推理。
"""

from __future__ import annotations

import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import vocal_separator as vs


def _settings(**overrides):
    values = {
        "vocal_separation_backend": "demucs",
        "vocal_separation_command": "python -m demucs.separate",
        "vocal_separation_timeout": 900,
        "ffmpeg_bin": "ffmpeg",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_separate_vocals_builds_cpu_demucs_command_and_normalizes_output(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    task_dir = tmp_path / "task"
    calls = []

    monkeypatch.setattr(vs, "settings", _settings())
    monkeypatch.setattr(vs, "ensure_task_dir", lambda task_id: task_dir)

    def fake_external(
        cmd, *, timeout, task_id, cancel_check=None, threads=None,
        register_process=None, unregister_process=None,
    ):
        calls.append((cmd, {"timeout": timeout, "task_id": task_id}))
        if len(calls) == 1:
            work_dir = Path(cmd[cmd.index("-o") + 1])
            demucs_output = work_dir / "htdemucs" / source.stem / "vocals.wav"
            demucs_output.parent.mkdir(parents=True, exist_ok=True)
            demucs_output.write_bytes(b"vocals")
        else:
            Path(cmd[-1]).write_bytes(b"normalized")
        return 0, "", ""

    monkeypatch.setattr(vs, "_run_external", fake_external)

    result = vs.separate_vocals(source, "task-1")

    assert result == task_dir / "vocal.wav"
    demucs_cmd, demucs_kwargs = calls[0]
    assert demucs_cmd[:3] == ["python", "-m", "demucs.separate"]
    assert "--two-stems=vocals" in demucs_cmd
    assert demucs_cmd[demucs_cmd.index("--device") + 1] == "cpu"
    assert demucs_cmd[demucs_cmd.index("--jobs") + 1] == "1"
    assert demucs_cmd[demucs_cmd.index("--shifts") + 1] == "0"
    assert demucs_kwargs["timeout"] == 900
    normalize_cmd, _ = calls[1]
    assert normalize_cmd[0] == "ffmpeg"
    staged_normalized_output = Path(normalize_cmd[-1])
    assert staged_normalized_output.parent == task_dir
    assert staged_normalized_output.name.startswith(".vocal.wav.")
    assert staged_normalized_output.name.endswith(".tmp")
    assert "-ac" in normalize_cmd and normalize_cmd[normalize_cmd.index("-ac") + 1] == "1"
    assert "-ar" in normalize_cmd and normalize_cmd[normalize_cmd.index("-ar") + 1] == "16000"


@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_normalize_failure_or_cancellation_cleans_registered_temp(tmp_path, monkeypatch, failure):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    task_dir = tmp_path / "task"
    monkeypatch.setattr(vs, "settings", _settings())
    monkeypatch.setattr(vs, "ensure_task_dir", lambda task_id: task_dir)
    calls = 0

    def fake_external(cmd, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            work_dir = Path(cmd[cmd.index("-o") + 1])
            vocals = work_dir / "htdemucs" / source.stem / "vocals.wav"
            vocals.parent.mkdir(parents=True, exist_ok=True)
            vocals.write_bytes(b"vocals")
            return 0, "", ""
        if failure == "cancel":
            raise vs.VocalSeparationCancelledError("cancelled during normalize")
        raise vs.VocalSeparationError("normalize failed")

    monkeypatch.setattr(vs, "_run_external", fake_external)
    expected = vs.VocalSeparationCancelledError if failure == "cancel" else vs.VocalSeparationError
    with pytest.raises(expected):
        vs.separate_vocals(source, "task-1")

    assert not list(task_dir.glob(".vocal.wav.*.tmp"))
    assert not list((task_dir / ".vocal-separation").rglob("*.wav"))


def test_metadata_replace_failure_cleans_registered_temp(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    task_dir = tmp_path / "task"
    monkeypatch.setattr(vs, "settings", _settings())
    monkeypatch.setattr(vs, "ensure_task_dir", lambda task_id: task_dir)

    def fake_external(cmd, **kwargs):
        if "--two-stems=vocals" in cmd:
            work_dir = Path(cmd[cmd.index("-o") + 1])
            vocals = work_dir / "htdemucs" / source.stem / "vocals.wav"
            vocals.parent.mkdir(parents=True, exist_ok=True)
            vocals.write_bytes(b"vocals")
        else:
            Path(cmd[-1]).write_bytes(b"normalized")
        return 0, "", ""

    monkeypatch.setattr(vs, "_run_external", fake_external)
    real_replace = vs.os.replace

    def fail_metadata_replace(src, dst):
        if Path(dst).name == "separation.asr.meta.json":
            raise OSError("metadata replace failed")
        return real_replace(src, dst)

    monkeypatch.setattr(vs.os, "replace", fail_metadata_replace)
    with pytest.raises(OSError, match="metadata replace failed"):
        vs.separate_vocals(source, "task-1")

    assert not list(task_dir.glob(".*.tmp"))


def test_separate_vocals_rejects_missing_source(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, "settings", _settings())

    with pytest.raises(vs.VocalSeparationError, match="输入音频不存在"):
        vs.separate_vocals(tmp_path / "missing.wav", "task-1")


def test_separate_vocals_wraps_demucs_timeout(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr(vs, "settings", _settings())
    monkeypatch.setattr(vs, "ensure_task_dir", lambda task_id: tmp_path / "task")

    def timeout(*args, **kwargs):
        raise vs.VocalSeparationError("人声分离超时")

    monkeypatch.setattr(vs, "_run_external", timeout)

    with pytest.raises(vs.VocalSeparationError, match="人声分离超时"):
        vs.separate_vocals(source, "task-1")


def test_separate_vocals_rejects_unsupported_backend(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr(vs, "settings", _settings(vocal_separation_backend="spleeter"))

    with pytest.raises(vs.VocalSeparationError, match="不支持的人声分离 backend"):
        vs.separate_vocals(source, "task-1")


def test_separate_vocals_requires_vocals_artifact(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    task_dir = tmp_path / "task"
    monkeypatch.setattr(vs, "settings", _settings())
    monkeypatch.setattr(vs, "ensure_task_dir", lambda task_id: task_dir)
    monkeypatch.setattr(vs, "_run_external", lambda *args, **kwargs: (0, "", ""))

    with pytest.raises(vs.VocalSeparationError, match="未生成 vocals.wav"):
        vs.separate_vocals(source, "task-1")


def test_cached_vocals_rejects_cache_without_metadata(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    (tmp_path / "vocal.wav").write_bytes(b"vocals")
    monkeypatch.setattr(vs, "ensure_task_dir", lambda task_id: tmp_path)

    assert vs.cached_vocals_are_current(source, "task-1") is False


def test_cached_vocals_rejects_stale_metadata(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    (tmp_path / "vocal.wav").write_bytes(b"vocals")
    (tmp_path / "vocal.meta.json").write_text(
        json.dumps({
            "source": str((tmp_path / "other.wav").resolve()),
            "source_size": source.stat().st_size,
            "source_mtime_ns": source.stat().st_mtime_ns,
            "backend": "demucs",
            "command": "python -m demucs.separate",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(vs, "ensure_task_dir", lambda task_id: tmp_path)
    monkeypatch.setattr(vs, "settings", _settings())

    assert vs.cached_vocals_are_current(source, "task-1") is False


def test_cached_vocals_accepts_matching_metadata(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    (tmp_path / "vocal.wav").write_bytes(b"vocals")
    monkeypatch.setattr(vs, "settings", _settings())
    (tmp_path / "vocal.meta.json").write_text(
        json.dumps({
            "source": str(source.resolve()),
            "source_size": source.stat().st_size,
            "source_mtime_ns": source.stat().st_mtime_ns,
            "backend": "demucs",
            "command": "python -m demucs.separate",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(vs, "ensure_task_dir", lambda task_id: tmp_path)

    assert vs.cached_vocals_are_current(source, "task-1") is True


def _write_test_wav(path: Path, *, rate: int = 48000, channels: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * channels * 32)


def test_separate_stems_uses_explicit_snapshot_and_dubbing_does_not_touch_asr(
    tmp_path, monkeypatch
):
    source = tmp_path / "tts_source.wav"
    _write_test_wav(source)
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    asr_vocal = task_dir / "vocal.wav"
    _write_test_wav(asr_vocal, rate=16000, channels=1)
    old_asr_vocal = asr_vocal.read_bytes()
    calls = []
    monkeypatch.setattr(vs, "settings", _settings(
        vocal_separation_backend="wrong-backend",
        vocal_separation_model="wrong-model",
        vocal_separation_threads=99,
        vocal_separation_command="wrong-command",
        vocal_separation_timeout=1,
    ))

    def fake_external(
        command, *, timeout, task_id, cancel_check=None, threads=None,
        register_process=None, unregister_process=None,
    ):
        calls.append((command, timeout, threads))
        if "--two-stems=vocals" in command:
            work_dir = Path(command[command.index("-o") + 1])
            stem_dir = work_dir / "htdemucs" / source.stem
            _write_test_wav(stem_dir / "vocals.wav")
            _write_test_wav(stem_dir / "no_vocals.wav")
        else:
            _write_test_wav(Path(command[-1]), rate=16000, channels=1)
        return 0, "", ""

    monkeypatch.setattr(vs, "_run_external", fake_external)
    result = vs.separate_stems(
        source,
        "task",
        purpose="separate.dubbing",
        vocal_path=None,
        background_path=task_dir / "background.wav",
        backend="demucs",
        model="snapshot-model",
        threads=4,
        command="python -m demucs.separate",
        timeout_seconds=83,
        invocation_id="dubbing-call-1",
        task_dir=task_dir,
    )

    assert result.vocal_path is None
    assert result.background_path == (task_dir / "background.wav").resolve()
    with wave.open(str(result.background_path), "rb") as output:
        assert output.getframerate() == 48000
        assert output.getnchannels() == 2
    assert asr_vocal.read_bytes() == old_asr_vocal
    command, timeout, threads = calls[0]
    assert command[command.index("-n") + 1] == "snapshot-model"
    assert command[command.index("--jobs") + 1] == "4"
    assert timeout == 83 and threads == 4


def test_separate_stems_checks_cancel_while_waiting_for_semaphore(tmp_path, monkeypatch):
    class BusySemaphore:
        def acquire(self, timeout):
            return False

        def release(self):
            raise AssertionError("unacquired semaphore must not be released")

    monkeypatch.setattr(vs, "_SEPARATION_SLOT", BusySemaphore())
    checks = []

    def cancel_check():
        checks.append(True)
        raise vs.VocalSeparationCancelledError("cancelled while waiting")

    with pytest.raises(vs.VocalSeparationCancelledError):
        vs.separate_stems(
            tmp_path / "audio.wav",
            "task",
            purpose="separate.asr",
            vocal_path=tmp_path / "task" / "vocal.wav",
            background_path=None,
            backend="demucs",
            model="htdemucs",
            threads=2,
            command="python -m demucs.separate",
            timeout_seconds=60,
            invocation_id="waiting-call",
            task_dir=tmp_path / "task",
            cancel_check=cancel_check,
        )
    assert checks
