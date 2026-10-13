from pathlib import Path

import pytest

from src.plugin_system import (
    DubMuxRequest,
    FakeContext,
    PluginCancelled,
    PluginError,
    SubtitleRenderRequest,
    SubtitleRenderResult,
    DubMuxResult,
)
from src.plugins.builtin import subtitle_ffmpeg


def _video_request(task_dir: Path, video: Path, srt: Path, mode="soft"):
    from src.config import output_video_filename

    return SubtitleRenderRequest(
        stage="render", operation="subtitle", purpose="render.subtitle",
        source_video=video, translated_srt=srt,
        output_video=task_dir / output_video_filename(mode), mode=mode,
    )


def _mux_request(task_dir, base, source, speech, background, vocal, mode):
    return DubMuxRequest(
        stage="render", operation="dub_mux", purpose="render.dub_mux",
        base_video=base, source_audio=source, speech_audio=speech,
        background_audio=background, vocal_audio=vocal,
        mixed_audio=task_dir / "dubbed_mix.wav",
        output_video=task_dir / "output_dubbed.mp4",
        original_voice_mode=mode,
    )


def test_subtitle_request_uses_frozen_core_output_and_cancellation(tmp_path, monkeypatch):
    video = tmp_path / "source.mp4"
    srt = tmp_path / "translated.srt"
    video.write_bytes(b"video")
    srt.write_text("subtitle", encoding="utf-8")
    context = FakeContext(task_dir=tmp_path, purpose="render.subtitle")
    session = subtitle_ffmpeg.factory.create_session({"mode": "soft"}, context)
    fake_output = tmp_path / "output_soft.mp4"

    class BurnResult:
        output_path = fake_output

    def fake_burn(*args, **kwargs):
        fake_output.write_bytes(b"rendered")
        return BurnResult()

    monkeypatch.setattr(subtitle_ffmpeg.subtitle_burner, "burn_subtitles", fake_burn)
    result = session.run(_video_request(tmp_path, video, srt))
    assert result == SubtitleRenderResult(video_path=fake_output)

    mismatched = subtitle_ffmpeg.factory.create_session({"mode": "hard"}, context)
    with pytest.raises(PluginError) as config_error:
        mismatched.validate(_video_request(tmp_path, video, srt, mode="soft"))
    assert config_error.value.code == "invalid_input"

    cancelled_context = FakeContext(
        task_dir=tmp_path, purpose="render.subtitle", cancelled=True
    )
    cancelled = subtitle_ffmpeg.factory.create_session(
        {"mode": "soft"}, cancelled_context
    )
    with pytest.raises(PluginCancelled):
        cancelled.run(_video_request(tmp_path, video, srt))


def test_dub_mux_validates_stems_and_returns_exact_requested_paths(tmp_path, monkeypatch):
    base, source, speech, bg, vocal = [tmp_path / name for name in (
        "base.mp4", "source.wav", "speech.wav", "background.wav", "vocal.wav"
    )]
    for path in (base, source, speech, bg, vocal):
        path.write_bytes(b"input")
    context = FakeContext(task_dir=tmp_path, purpose="render.dub_mux")
    session = subtitle_ffmpeg.factory.create_session({"original_voice_mode": "lower"}, context)

    def fake_run_ffmpeg(cmd, on_tick=None, **kwargs):
        if "-filter_complex" in cmd:
            Path(cmd[-1]).write_bytes(b"mixed")
        else:
            Path(cmd[-1]).write_bytes(b"video")
        if on_tick:
            on_tick(0.5)

    monkeypatch.setattr(subtitle_ffmpeg.ffmpeg_utils, "run_ffmpeg", fake_run_ffmpeg)
    result = session.run(_mux_request(tmp_path, base, source, speech, bg, vocal, "lower"))
    assert result == DubMuxResult(tmp_path / "dubbed_mix.wav", tmp_path / "output_dubbed.mp4")
    assert len(context.progress) >= 1

    invalid = _mux_request(tmp_path, base, source, speech, None, vocal, "replace")
    replace_session = subtitle_ffmpeg.factory.create_session(
        {"original_voice_mode": "replace"}, context
    )
    with pytest.raises(PluginError) as exc_info:
        replace_session.validate(invalid)
    assert exc_info.value.code == "invalid_input"


def test_render_errors_preserve_cause_and_cancelled_ffmpeg_is_not_wrapped(tmp_path, monkeypatch):
    base, source, speech = [tmp_path / name for name in ("base.mp4", "source.wav", "speech.wav")]
    for path in (base, source, speech):
        path.write_bytes(b"input")
    request = _mux_request(tmp_path, base, source, speech, None, None, "keep")
    cause = RuntimeError("ffmpeg execution failure")
    context = FakeContext(task_dir=tmp_path, purpose="render.dub_mux")
    session = subtitle_ffmpeg.factory.create_session({"original_voice_mode": "keep"}, context)

    def fail(*args, **kwargs):
        raise cause

    monkeypatch.setattr(subtitle_ffmpeg.ffmpeg_utils, "run_ffmpeg", fail)
    with pytest.raises(PluginError) as exc_info:
        session.run(request)
    assert exc_info.value.cause is cause
    assert exc_info.value.code == "execution_error"

    def cancel(*args, **kwargs):
        kwargs["on_tick"](0.5)

    cancelled_context = FakeContext(
        task_dir=tmp_path, purpose="render.dub_mux", cancelled=True
    )
    cancelled = subtitle_ffmpeg.factory.create_session(
        {"original_voice_mode": "keep"}, cancelled_context
    )
    monkeypatch.setattr(subtitle_ffmpeg.ffmpeg_utils, "run_ffmpeg", cancel)
    with pytest.raises(PluginCancelled):
        cancelled.run(request)


def test_render_dependency_cause_is_actionable_and_existing_core_code_wins(tmp_path, monkeypatch):
    base, source, speech = [tmp_path / name for name in ("base.mp4", "source.wav", "speech.wav")]
    for path in (base, source, speech):
        path.write_bytes(b"input")
    request = _mux_request(tmp_path, base, source, speech, None, None, "keep")
    context = FakeContext(task_dir=tmp_path, purpose="render.dub_mux")
    session = subtitle_ffmpeg.factory.create_session({"original_voice_mode": "keep"}, context)

    missing_binary = FileNotFoundError("/private/path/to/ffmpeg")
    wrapped = RuntimeError("failed to spawn process")
    wrapped.__cause__ = missing_binary

    def fail_missing(*args, **kwargs):
        raise wrapped

    monkeypatch.setattr(subtitle_ffmpeg.ffmpeg_utils, "run_ffmpeg", fail_missing)
    with pytest.raises(PluginError) as missing:
        session.run(request)
    assert missing.value.code == "dependency_missing"
    assert "FFmpeg" in missing.value.message
    assert "/private/path" not in missing.value.message
    assert missing.value.cause is wrapped

    core_error = RuntimeError("core reported a stable code")
    core_error.code = "codec_not_supported"
    core_error.__cause__ = ImportError("private import detail")

    def fail_core(*args, **kwargs):
        raise core_error

    monkeypatch.setattr(subtitle_ffmpeg.ffmpeg_utils, "run_ffmpeg", fail_core)
    with pytest.raises(PluginError) as preserved:
        session.run(request)
    assert preserved.value.code == "codec_not_supported"
    assert preserved.value.cause is core_error
