"""② 提取音频 的单元测试。

纯逻辑（进度解析、ffprobe 判定、错误处理）全部 mock subprocess；
另含一个真实 ffmpeg 集成用例：自动生成带音轨的合成视频，校验产物为
16kHz / 单声道 / pcm_s16le。无 ffmpeg/ffprobe 时自动跳过。
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from src.core import audio_extractor as ax
from src.core.audio_extractor import (
    AudioExtractError,
    AudioProgress,
    extract_audio,
    _has_audio_stream,
    _parse_us,
    _probe_duration,
)


class FakeProc:
    """模拟 subprocess.run 的返回。"""

    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


# ---------- _parse_us ----------

def test_parse_us_valid():
    assert _parse_us("1000000") == 1.0
    assert _parse_us("500000") == 0.5


def test_parse_us_invalid():
    assert _parse_us("abc") is None
    assert _parse_us("") is None


# ---------- _has_audio_stream ----------

def test_has_audio_true(monkeypatch):
    monkeypatch.setattr(ax.subprocess, "run", lambda *a, **k: FakeProc(0, "0\n"))
    assert _has_audio_stream(Path("x.mp4")) is True


def test_has_audio_false_when_empty(monkeypatch):
    monkeypatch.setattr(ax.subprocess, "run", lambda *a, **k: FakeProc(0, "\n"))
    assert _has_audio_stream(Path("x.mp4")) is False


def test_has_audio_true_when_probe_fails(monkeypatch):
    # ffprobe 返回非 0 时，保守地交给 ffmpeg 兜底
    monkeypatch.setattr(ax.subprocess, "run", lambda *a, **k: FakeProc(1, ""))
    assert _has_audio_stream(Path("x.mp4")) is True


def test_has_audio_true_when_ffprobe_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(ax.subprocess, "run", boom)
    assert _has_audio_stream(Path("x.mp4")) is True


# ---------- _probe_duration ----------

def test_probe_duration_valid(monkeypatch):
    monkeypatch.setattr(ax.subprocess, "run", lambda *a, **k: FakeProc(0, "12.5\n"))
    assert _probe_duration(Path("x.mp4")) == 12.5


def test_probe_duration_invalid_output(monkeypatch):
    monkeypatch.setattr(ax.subprocess, "run", lambda *a, **k: FakeProc(0, "n/a\n"))
    assert _probe_duration(Path("x.mp4")) is None


def test_probe_duration_nonzero_exit(monkeypatch):
    monkeypatch.setattr(ax.subprocess, "run", lambda *a, **k: FakeProc(1, ""))
    assert _probe_duration(Path("x.mp4")) is None


def test_probe_duration_ffprobe_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(ax.subprocess, "run", boom)
    assert _probe_duration(Path("x.mp4")) is None


# ---------- extract_audio 前置校验 ----------

def test_extract_missing_input(tmp_path):
    with pytest.raises(AudioExtractError, match="不存在"):
        extract_audio(tmp_path / "nope.mp4", "task1")


def test_extract_no_audio_stream(tmp_path, monkeypatch):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr(ax, "_has_audio_stream", lambda p: False)
    with pytest.raises(AudioExtractError, match="音频流"):
        extract_audio(f, "task1")


def test_extract_ffmpeg_nonzero_exit(tmp_path, monkeypatch):
    """ffmpeg 退出码非 0 时应包装成 AudioExtractError。"""
    f = tmp_path / "video.mp4"
    f.write_bytes(b"x")
    out_dir = tmp_path / "out"
    monkeypatch.setattr(ax, "_has_audio_stream", lambda p: True)
    monkeypatch.setattr(ax, "_probe_duration", lambda p: None)
    monkeypatch.setattr(ax, "ensure_task_dir", _ensure(out_dir))

    class FailProc:
        stdout = iter([])  # 没有进度输出
        stderr = type("S", (), {"read": staticmethod(lambda: "boom")})()
        returncode = 1

        def wait(self):
            return 1

    monkeypatch.setattr(ax.subprocess, "Popen", lambda *a, **k: FailProc())

    with pytest.raises(AudioExtractError, match="退出码"):
        extract_audio(f, "task1")


def test_extract_ffmpeg_not_found(tmp_path, monkeypatch):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"x")
    out_dir = tmp_path / "out"
    monkeypatch.setattr(ax, "_has_audio_stream", lambda p: True)
    monkeypatch.setattr(ax, "_probe_duration", lambda p: None)
    monkeypatch.setattr(ax, "ensure_task_dir", _ensure(out_dir))

    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(ax.subprocess, "Popen", boom)

    with pytest.raises(AudioExtractError, match="找不到 ffmpeg"):
        extract_audio(f, "task1")


def _ensure(out_dir: Path):
    def fake_ensure_task_dir(task_id):
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    return fake_ensure_task_dir


# ---------- 真实 ffmpeg 集成用例 ----------

_FFMPEG_OK = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.mark.skipif(not _FFMPEG_OK, reason="需要 ffmpeg / ffprobe")
def test_extract_audio_real(tmp_path, monkeypatch):
    # 生成 1 秒带 440Hz 正弦音轨的合成视频
    src = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=128x72:rate=10",
            "-shortest", str(src),
        ],
        check=True,
        capture_output=True,
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setattr(ax, "ensure_task_dir", _ensure(out_dir))

    progress = []
    res = extract_audio(src, "task1", on_progress=progress.append)

    # 产物存在且参数正确
    assert res.audio_path.exists()
    assert res.audio_path.name == "audio.wav"
    assert res.sample_rate == 16000
    assert res.channels == 1
    assert res.duration is not None and res.duration > 0
    assert res.filesize and res.filesize > 0

    # 用 ffprobe 复核编码 / 采样率 / 声道
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_name,sample_rate,channels",
            "-of", "default=noprint_wrappers=1",
            str(res.audio_path),
        ],
        capture_output=True,
        text=True,
    )
    assert "pcm_s16le" in out.stdout
    assert "sample_rate=16000" in out.stdout
    assert "channels=1" in out.stdout

    # 至少产生过一次进度回调
    assert progress
    assert all(isinstance(p, AudioProgress) for p in progress)


@pytest.mark.skipif(not _FFMPEG_OK, reason="需要 ffmpeg / ffprobe")
def test_extract_audio_explicit_dubbing_path_keeps_asr_audio_separate(tmp_path, monkeypatch):
    src = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.8",
            "-f", "lavfi", "-i", "testsrc=duration=0.8:size=128x72:rate=10",
            "-shortest", str(src),
        ],
        check=True,
        capture_output=True,
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setattr(ax, "ensure_task_dir", _ensure(out_dir))
    asr_audio = out_dir / "audio.wav"
    asr_audio.write_bytes(b"existing ASR artifact")

    result = extract_audio(
        src,
        "task1",
        sample_rate=48000,
        channels=2,
        output_path=out_dir / "tts_source.wav",
    )

    assert result.audio_path == out_dir / "tts_source.wav"
    assert result.sample_rate == 48000
    assert result.channels == 2
    assert result.duration is not None and 0.7 <= result.duration <= 0.9
    assert asr_audio.read_bytes() == b"existing ASR artifact"
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_name,sample_rate,channels",
            "-of", "default=noprint_wrappers=1",
            str(result.audio_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "pcm_s16le" in probe.stdout
    assert "sample_rate=48000" in probe.stdout
    assert "channels=2" in probe.stdout
