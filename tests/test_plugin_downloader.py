from pathlib import Path

import pytest

from src.core import downloader
from src.core.downloader import DownloadResult as CoreDownloadResult
from src.core.downloader import ProbeResult as CoreProbeResult
from src.plugin_system import (
    DownloadRequest,
    FakeContext,
    PluginCancelled,
    PluginError,
    ProbeFormat,
    ProbeRequest,
    ProbeResult,
)
from src.plugins.builtin import downloader_ytdlp


def _config(**overrides):
    config = {
        "quality": "720p",
        "format_selector": None,
        "proxy": None,
        "socket_timeout": 31,
        "cookies_ref": None,
        "merge_output_format": "mkv",
        "retries": 5,
        "concurrent_fragments": 3,
    }
    config.update(overrides)
    return config


def _download_request(task_dir: Path):
    return DownloadRequest(
        stage="download",
        operation="download",
        purpose="download",
        url="https://video.example/watch?v=1",
        quality="720p",
        target_dir=task_dir,
    )


def test_factory_manifest_includes_download_and_probe_capability(tmp_path):
    session = downloader_ytdlp.factory.create_session(
        _config(), FakeContext(task_dir=tmp_path, purpose="download")
    )
    assert downloader_ytdlp.factory.stage == "download"
    assert downloader_ytdlp.factory.manifest.plugin_id == "builtin.ytdlp"
    assert downloader_ytdlp.factory.manifest.protocol_version == 1
    assert downloader_ytdlp.factory.manifest.implementation_version == "1"
    assert downloader_ytdlp.factory.manifest.operations == frozenset({"download"})
    assert downloader_ytdlp.factory.manifest.capabilities == frozenset({"probe"})
    session.close()


def test_download_adapter_passes_frozen_config_and_maps_result(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    source = task_dir / "source.mkv"
    source.write_bytes(b"media")
    cookie_ref = "settings:cookies_file"
    context = FakeContext(
        task_id="task",
        task_dir=task_dir,
        purpose="download",
        credentials={cookie_ref: "/private/task-cookies.txt"},
    )
    captured = {}

    def fake_download(url, task_id, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        captured["task_id"] = task_id
        kwargs["on_progress"](type("Progress", (), {"percent": 40.5})())
        return CoreDownloadResult(
            video_path=source, title="Example", duration=10.0, ext="mkv",
            filesize=5, width=1280, height=720, source_url=url,
        )

    monkeypatch.setattr(downloader_ytdlp.downloader, "download_video", fake_download)
    config = _config(
        proxy=None,
        cookies_ref=cookie_ref,
        merge_output_format="mkv",
        retries=5,
        concurrent_fragments=3,
    )
    session = downloader_ytdlp.factory.create_session(config, context)
    result = session.run(_download_request(task_dir))

    assert result.source_path == source
    assert result.title == "Example" and result.duration_seconds == 10.0
    assert captured["cookies_file"] == Path("/private/task-cookies.txt")
    assert captured["proxy"] is None
    assert captured["socket_timeout"] == 31
    assert captured["merge_output_format"] == "mkv"
    assert captured["retries"] == 5
    assert captured["concurrent_fragments"] == 3
    assert captured["quality"] == "720p"
    assert captured["task_id"] == "task"
    assert context.progress == [40.5]
    session.close()


def test_download_adapter_preserves_cancellation(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    source = task_dir / "source.mp4"
    source.write_bytes(b"x")
    context = FakeContext(
        task_id="task", task_dir=task_dir, purpose="download", cancelled=True
    )
    called = False

    def fake_download(*args, **kwargs):
        nonlocal called
        called = True
        kwargs["cancel_check"]()

    monkeypatch.setattr(downloader_ytdlp.downloader, "download_video", fake_download)
    session = downloader_ytdlp.factory.create_session(_config(), context)
    with pytest.raises(PluginCancelled):
        session.run(_download_request(task_dir))
    assert called is True


def test_probe_adapter_converts_exact_result_shape_and_uses_browser_mode(
    tmp_path, monkeypatch
):
    captured = {}
    core_result = CoreProbeResult(
        ok=True,
        title="Title",
        extractor="Example",
        duration=12.0,
        formats_count=1,
        webpage_url="https://video.example/watch?v=1",
        cached=False,
        language="en",
        available_qualities=["720p"],
        formats=[{
            "formatId": "22", "ext": "mp4", "resolution": "720p",
            "height": 720, "width": 1280, "fps": 30.0,
            "vcodec": "h264", "acodec": "aac", "filesize": 100,
            "tbr": 1000.0, "note": None, "type": "video",
        }],
        thumbnail="https://video.example/thumb.jpg",
        uploader="uploader",
    )

    def fake_probe(url, **kwargs):
        captured.update(kwargs)
        return core_result

    monkeypatch.setattr(downloader_ytdlp.downloader, "probe_video", fake_probe)
    session = downloader_ytdlp.factory.create_session(
        _config(), FakeContext(task_dir=tmp_path, purpose="download")
    )
    result = session.run(ProbeRequest(
        "https://video.example/watch?v=1",
        force_refresh=True,
        cookies_from_browser="Chrome",
    ))

    assert isinstance(result, ProbeResult)
    assert result.available_qualities == ("720p",)
    assert result.formats == (ProbeFormat(
        formatId="22", ext="mp4", resolution="720p", height=720, width=1280,
        fps=30.0, vcodec="h264", acodec="aac", filesize=100, tbr=1000.0,
        note=None, type="video",
    ),)
    assert captured["cookies_from_browser"] == "Chrome"
    assert captured["force_refresh"] is True
    assert captured["format_selector"] == "bv*[height<=720]+ba/b[height<=720]"
    session.close()


def test_config_rejects_credentials_embedded_in_proxy_or_invalid_cookie_ref(tmp_path):
    context = FakeContext(task_dir=tmp_path, purpose="download")
    bad_proxy = downloader_ytdlp.factory.create_session(
        _config(proxy="https://user:password@proxy.example"), context
    )
    with pytest.raises(PluginError, match="embedded credentials"):
        bad_proxy.validate(_download_request(tmp_path))

    bad_ref = downloader_ytdlp.factory.create_session(
        _config(cookies_ref="translation_engine:secret:api_key"), context
    )
    with pytest.raises(PluginError, match="cookie reference"):
        bad_ref.validate(_download_request(tmp_path))
