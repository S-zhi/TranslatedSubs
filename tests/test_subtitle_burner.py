"""⑤ 烧录字幕 单测。

命令构造 / 错误处理用 mock；另含真实 ffmpeg 集成用例（硬烧录需 libass）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.core import subtitle_burner as sb
from src.core.srt_utils import Subtitle, write_srt
from src.core.subtitle_burner import BurnError, burn_subtitles


def _ensure(out_dir: Path):
    def fake(task_id):
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    return fake


@pytest.fixture
def inputs(tmp_path):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"fakevideo")
    srt = tmp_path / "translated.srt"
    write_srt([Subtitle(1, 0, 1, "hi")], srt)
    return video, srt


# ---------- 前置校验 ----------

def test_missing_video(tmp_path):
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    with pytest.raises(BurnError, match="视频不存在"):
        burn_subtitles(tmp_path / "nope.mp4", srt, "task1")


def test_missing_srt(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(BurnError, match="字幕不存在"):
        burn_subtitles(video, tmp_path / "nope.srt", "task1")


def test_unknown_mode(inputs, tmp_path, monkeypatch):
    video, srt = inputs
    monkeypatch.setattr(sb, "ensure_task_dir", _ensure(tmp_path / "out"))
    monkeypatch.setattr(sb, "probe_duration", lambda p, b: 1.0)
    with pytest.raises(BurnError, match="未知烧录模式"):
        burn_subtitles(video, srt, "task1", mode="weird")


# ---------- 命令构造 ----------

def test_hard_cmd_uses_subtitles_filter(inputs, tmp_path, monkeypatch):
    video, srt = inputs
    captured = {}

    def fake_run(cmd, on_tick=None, **kw):
        captured["cmd"] = cmd
        captured["cwd"] = kw.get("cwd")
        Path(cmd[-1]).write_bytes(b"out")

    out_dir = tmp_path / "out"
    monkeypatch.setattr(sb, "has_subtitles_filter", lambda b: True)  # 假装有 libass
    monkeypatch.setattr(sb, "ensure_task_dir", _ensure(out_dir))
    monkeypatch.setattr(sb, "probe_duration", lambda p, b: 10.0)
    monkeypatch.setattr(sb, "run_ffmpeg", fake_run)

    res = burn_subtitles(video, srt, "task1", mode="hard")
    assert res.mode == "hard"
    assert res.output_path.exists()
    cmd = captured["cmd"]
    # 滤镜引用 safe basename tmp_burn.srt，配合 out_dir cwd 规避路径转义
    assert any("subtitles=tmp_burn.srt" in str(c) for c in cmd)
    assert "-c:a" in cmd and "copy" in cmd
    assert captured["cwd"] == str(out_dir.resolve())


@pytest.mark.parametrize("filename", [
    "translated's v2.srt",
    "translated.草稿.srt",
    "my translated subs (v2) [final].srt",
    "sub:title=v1.srt",
    "path_with_backslash\\test.srt",
])
def test_hard_burn_with_special_chars_in_filename(inputs, tmp_path, monkeypatch, filename):
    video, _ = inputs
    special_srt = tmp_path / filename
    write_srt([Subtitle(1, 0, 1, "hi")], special_srt)

    captured = {}

    def fake_run(cmd, on_tick=None, **kw):
        captured["cmd"] = cmd
        captured["cwd"] = kw.get("cwd")
        # Ensure tmp_burn.srt was copied into out_dir during run_ffmpeg
        tmp_burn = Path(kw["cwd"]) / "tmp_burn.srt"
        assert tmp_burn.exists()
        Path(cmd[-1]).write_bytes(b"out")

    out_dir = tmp_path / "out_task"
    monkeypatch.setattr(sb, "has_subtitles_filter", lambda b: True)
    monkeypatch.setattr(sb, "ensure_task_dir", _ensure(out_dir))
    monkeypatch.setattr(sb, "probe_duration", lambda p, b: 10.0)
    monkeypatch.setattr(sb, "run_ffmpeg", fake_run)

    res = burn_subtitles(video, special_srt, "task_special", mode="hard")
    assert res.mode == "hard"
    assert res.output_path.exists()
    cmd = captured["cmd"]

    # Filter argument should use safe ASCII filename "tmp_burn.srt"
    assert any("subtitles=tmp_burn.srt" in str(c) for c in cmd)
    assert captured["cwd"] == str(out_dir.resolve())
    # Temporary file should be cleaned up after burn finishes
    assert not (out_dir / "tmp_burn.srt").exists()


def test_hard_mode_requires_libass(inputs, tmp_path, monkeypatch):
    """ffmpeg 缺 libass 时，硬烧录应给出清晰报错。"""
    video, srt = inputs
    monkeypatch.setattr(sb, "has_subtitles_filter", lambda b: False)
    monkeypatch.setattr(sb, "ensure_task_dir", _ensure(tmp_path / "out"))
    with pytest.raises(BurnError, match="libass"):
        burn_subtitles(video, srt, "task1", mode="hard")


def test_soft_cmd_uses_mov_text(inputs, tmp_path, monkeypatch):
    video, srt = inputs
    captured = {}

    def fake_run(cmd, on_tick=None, **kw):
        captured["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"out")

    monkeypatch.setattr(sb, "ensure_task_dir", _ensure(tmp_path / "out"))
    monkeypatch.setattr(sb, "probe_duration", lambda p, b: 10.0)
    monkeypatch.setattr(sb, "run_ffmpeg", fake_run)

    res = burn_subtitles(video, srt, "task1", mode="soft")
    assert res.mode == "soft"
    cmd = captured["cmd"]
    assert "-c:s" in cmd and "mov_text" in cmd
    assert "-map" in cmd


# ---------- 错误处理 ----------

def test_ffmpeg_failure_propagates(inputs, tmp_path, monkeypatch):
    video, srt = inputs

    def fake_run(cmd, on_tick=None, **kw):
        raise BurnError("boom")

    monkeypatch.setattr(sb, "ensure_task_dir", _ensure(tmp_path / "out"))
    monkeypatch.setattr(sb, "probe_duration", lambda p, b: 1.0)
    monkeypatch.setattr(sb, "run_ffmpeg", fake_run)

    with pytest.raises(BurnError, match="boom"):
        burn_subtitles(video, srt, "task1", mode="soft")


def test_missing_output_after_run(inputs, tmp_path, monkeypatch):
    video, srt = inputs

    def fake_run(cmd, on_tick=None, **kw):
        pass  # 不生成输出文件

    monkeypatch.setattr(sb, "ensure_task_dir", _ensure(tmp_path / "out"))
    monkeypatch.setattr(sb, "probe_duration", lambda p, b: 1.0)
    monkeypatch.setattr(sb, "run_ffmpeg", fake_run)

    with pytest.raises(BurnError, match="未生成有效输出"):
        burn_subtitles(video, srt, "task1", mode="soft")


def test_custom_output_path_keeps_default_output_untouched(inputs, tmp_path, monkeypatch):
    video, srt = inputs
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    previous = output_dir / "output_soft.mp4"
    previous.write_bytes(b"old-finished-video")
    temporary = output_dir / (".reburn-" + "a" * 32 + ".mp4")
    monkeypatch.setattr(sb, "ensure_task_dir", _ensure(output_dir))
    monkeypatch.setattr(sb, "probe_duration", lambda *args: 1.0)

    def run(cmd, *args, **kwargs):
        Path(cmd[-1]).write_bytes(b"rendered")

    monkeypatch.setattr(sb, "run_ffmpeg", run)
    result = burn_subtitles(video, srt, "task1", mode="soft", output_path=temporary)
    assert result.output_path == temporary
    assert temporary.read_bytes() == b"rendered"
    assert previous.read_bytes() == b"old-finished-video"


@pytest.mark.parametrize("target", ["outside", "input", "symlink", "hardlink", "not_mp4"])
def test_custom_output_rejects_unsafe_or_overlapping_paths(inputs, tmp_path, monkeypatch, target):
    video, srt = inputs
    monkeypatch.setattr(sb, "ensure_task_dir", _ensure(tmp_path))
    monkeypatch.setattr(sb, "run_ffmpeg", lambda *args, **kwargs: pytest.fail("invalid path reached FFmpeg"))
    output = tmp_path / "new.mp4"
    if target == "outside":
        output = tmp_path.parent / "outside.mp4"
    elif target == "input":
        output = video
    elif target == "symlink":
        output.symlink_to(video)
    elif target == "hardlink":
        output.hardlink_to(video)
    else:
        output = tmp_path / "new.txt"
    before = video.read_bytes()
    with pytest.raises(BurnError, match="输出路径"):
        burn_subtitles(video, srt, "task1", mode="soft", output_path=output)
    assert video.read_bytes() == before


# ---------- 真实 ffmpeg 集成 ----------

_FFMPEG_OK = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _has_subtitles_filter() -> bool:
    if not _FFMPEG_OK:
        return False
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=15
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return " subtitles " in out.stdout


def _make_video(tmp_path: Path) -> Path:
    src = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
            "-shortest", str(src),
        ],
        check=True, capture_output=True,
    )
    return src


def _make_srt(tmp_path: Path) -> Path:
    p = tmp_path / "subs.srt"
    write_srt([Subtitle(1, 0.0, 1.0, "hello world")], p)
    return p


@pytest.mark.skipif(not _FFMPEG_OK, reason="需要 ffmpeg / ffprobe")
def test_burn_soft_real(tmp_path, monkeypatch):
    video = _make_video(tmp_path)
    srt = _make_srt(tmp_path)
    monkeypatch.setattr(sb, "ensure_task_dir", _ensure(tmp_path / "out"))

    res = burn_subtitles(video, srt, "task1", mode="soft")
    assert res.output_path.exists() and res.filesize > 0

    # 输出应含字幕流
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(res.output_path)],
        capture_output=True, text=True,
    )
    assert out.stdout.strip(), "软字幕应内封一条字幕流"


@pytest.mark.skipif(not _has_subtitles_filter(), reason="ffmpeg 缺少 subtitles 滤镜（libass）")
def test_burn_hard_real(tmp_path, monkeypatch):
    video = _make_video(tmp_path)
    srt = _make_srt(tmp_path)
    monkeypatch.setattr(sb, "ensure_task_dir", _ensure(tmp_path / "out"))

    progress = []
    res = burn_subtitles(video, srt, "task1", mode="hard", on_progress=progress.append)
    assert res.output_path.exists() and res.filesize > 0
    # 硬烧录会重编码出视频流
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(res.output_path)],
        capture_output=True, text=True,
    )
    assert out.stdout.strip()
