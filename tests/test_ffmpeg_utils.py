import subprocess

import pytest

from src.core import ffmpeg_utils


def test_has_subtitles_filter_does_not_share_results_between_binaries(monkeypatch):
    ffmpeg_utils._subtitles_filter_cache.clear()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[0])
        output = " subtitles " if cmd[0] == "with-libass" else ""
        return subprocess.CompletedProcess(cmd, 0, output, "")

    monkeypatch.setattr(ffmpeg_utils.shutil, "which", lambda binary: f"/bin/{binary}")
    monkeypatch.setattr(ffmpeg_utils.Path, "is_file", lambda self: False)
    monkeypatch.setattr(ffmpeg_utils.Path, "stat", lambda self: type("Stat", (), {"st_dev": 1, "st_ino": hash(str(self)), "st_size": 1, "st_mtime_ns": 1})())
    monkeypatch.setattr(ffmpeg_utils.subprocess, "run", fake_run)

    assert ffmpeg_utils.has_subtitles_filter("with-libass") is True
    assert ffmpeg_utils.has_subtitles_filter("without-libass") is False
    assert calls == ["with-libass", "without-libass"]


def test_has_subtitles_filter_rechecks_when_binary_metadata_changes(monkeypatch):
    ffmpeg_utils._subtitles_filter_cache.clear()
    mtime = [1]
    calls = []

    class Stat:
        st_dev = 1
        st_ino = 1
        st_size = 1

        @property
        def st_mtime_ns(self):
            return mtime[0]

    monkeypatch.setattr(ffmpeg_utils.shutil, "which", lambda _: "/bin/ffmpeg")
    monkeypatch.setattr(ffmpeg_utils.Path, "is_file", lambda self: False)
    monkeypatch.setattr(ffmpeg_utils.Path, "stat", lambda self: Stat())
    monkeypatch.setattr(ffmpeg_utils.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, " subtitles ", ""))

    assert ffmpeg_utils.has_subtitles_filter("ffmpeg") is True
    assert ffmpeg_utils.has_subtitles_filter("ffmpeg") is True
    mtime[0] = 2
    assert ffmpeg_utils.has_subtitles_filter("ffmpeg") is True
    assert len(calls) == 2


def test_has_subtitles_filter_caches_missing_binary_result(monkeypatch):
    ffmpeg_utils._subtitles_filter_cache.clear()
    calls = []
    monkeypatch.setattr(ffmpeg_utils.shutil, "which", lambda _: None)
    monkeypatch.setattr(ffmpeg_utils.subprocess, "run", lambda *args, **kwargs: calls.append(args) or (_ for _ in ()).throw(FileNotFoundError))

    assert ffmpeg_utils.has_subtitles_filter("missing-ffmpeg") is False
    assert ffmpeg_utils.has_subtitles_filter("missing-ffmpeg") is False
    assert len(calls) == 1


def test_run_ffmpeg_handles_large_stderr_without_deadlock(tmp_path):
    """验证当 stderr 产生超过 OS 管道缓冲区容量的大量数据时，run_ffmpeg 不会卡住，并能正常引发异常/返回。"""
    import sys
    script = (
        "import sys\n"
        "for i in range(2000):\n"
        "    sys.stderr.write(f'Stderr line {i}: ' + 'x' * 100 + '\\n')\n"
        "sys.stderr.flush()\n"
        "sys.stdout.write('out_time_us=1000000\\nprogress=continue\\n')\n"
        "sys.stdout.flush()\n"
        "for i in range(2000):\n"
        "    sys.stderr.write(f'More stderr line {i}: ' + 'y' * 100 + '\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(1)\n"
    )
    cmd = [sys.executable, "-c", script, "-progress", "pipe:1"]

    ticks = []
    with pytest.raises(RuntimeError) as exc_info:
        ffmpeg_utils.run_ffmpeg(cmd, on_tick=ticks.append)

    assert "Stderr line" in str(exc_info.value) or "More stderr line" in str(exc_info.value)
    assert ticks == [1.0]


def test_extract_audio_large_stderr_does_not_deadlock(tmp_path, monkeypatch):
    import sys
    from src.core.audio_extractor import extract_audio, AudioExtractError

    video = tmp_path / "source.mp4"
    video.write_bytes(b"dummy_video")

    monkeypatch.setattr("src.core.audio_extractor._has_audio_stream", lambda p: True)
    monkeypatch.setattr("src.core.audio_extractor._probe_duration", lambda p: 10.0)
    monkeypatch.setattr("src.core.audio_extractor.ensure_task_dir", lambda task_id: tmp_path)

    script = (
        "import sys\n"
        "for i in range(2000):\n"
        "    sys.stderr.write('E' * 100 + '\\n')\n"
        "sys.stderr.flush()\n"
        "sys.stdout.write('out_time_us=5000000\\nprogress=continue\\n')\n"
        "sys.stdout.flush()\n"
        "for i in range(2000):\n"
        "    sys.stderr.write('F' * 100 + '\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(1)\n"
    )

    original_run_ffmpeg = ffmpeg_utils.run_ffmpeg

    def fake_run_ffmpeg(cmd, *args, **kwargs):
        new_cmd = [sys.executable, "-c", script, "-progress", "pipe:1"]
        return original_run_ffmpeg(new_cmd, *args, **kwargs)

    monkeypatch.setattr("src.core.audio_extractor.run_ffmpeg", fake_run_ffmpeg)

    with pytest.raises(AudioExtractError, match="退出码"):
        extract_audio(video, "task1")


def test_burn_subtitles_large_stderr_does_not_deadlock(tmp_path, monkeypatch):
    import sys
    from src.core.subtitle_burner import burn_subtitles, BurnError
    from src.core.srt_utils import Subtitle, write_srt

    video = tmp_path / "source.mp4"
    video.write_bytes(b"dummy_video")
    srt = tmp_path / "subs.srt"
    write_srt([Subtitle(1, 0.0, 1.0, "hi")], srt)

    monkeypatch.setattr("src.core.subtitle_burner.ensure_task_dir", lambda task_id: tmp_path)
    monkeypatch.setattr("src.core.subtitle_burner.probe_duration", lambda p, b: 10.0)

    script = (
        "import sys\n"
        "for i in range(2000):\n"
        "    sys.stderr.write('Err ' * 20 + '\\n')\n"
        "sys.stderr.flush()\n"
        "sys.stdout.write('out_time_us=2000000\\nprogress=continue\\n')\n"
        "sys.stdout.flush()\n"
        "sys.exit(1)\n"
    )

    original_run_ffmpeg = ffmpeg_utils.run_ffmpeg

    def fake_run_ffmpeg(cmd, *args, **kwargs):
        new_cmd = [sys.executable, "-c", script, "-progress", "pipe:1"]
        return original_run_ffmpeg(new_cmd, *args, **kwargs)

    monkeypatch.setattr("src.core.subtitle_burner.run_ffmpeg", fake_run_ffmpeg)

    with pytest.raises(BurnError, match="退出码"):
        burn_subtitles(video, srt, "task1", mode="soft")
