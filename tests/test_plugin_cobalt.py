from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from src.plugin_system import (
    DownloadRequest,
    DownloadResult,
    FakeContext,
    PluginCancelled,
    PluginError,
    ProbeFormat,
    ProbeRequest,
    ProbeResult,
)
from src.plugins.builtin import downloader_cobalt
from src.plugins.builtin.cobalt_client import CobaltError, CobaltMedia
from src.plugins.builtin.cobalt_configuration import YTDLP_CONFIG_KEYS


_URL = "https://video.example/watch?v=1"


def _config(**overrides):
    config = {
        "quality": "720p", "format_selector": "bv*[height<=720]+ba/b[height<=720]",
        "proxy": None, "socket_timeout": 31, "cookies_ref": None,
        "merge_output_format": "mkv", "retries": 5, "concurrent_fragments": 3,
        "endpoint": "https://cobalt.example/", "access_mode": "anonymous",
        "fallback_policy": "none", "filename_style": "basic", "audio_format": "best",
    }
    config.update(overrides)
    return config


def _request(task_dir, quality="720p"):
    return DownloadRequest("download", "download", "download", _URL, quality, task_dir)


def _session(tmp_path, **config):
    context = FakeContext(task_dir=tmp_path, purpose="download")
    return downloader_cobalt.factory.create_session(_config(**config), context), context


class _Client:
    def __init__(self, **options):
        self.options = options
        self.resolve_calls = []
        self.download_calls = []
        self.close_calls = 0
        self.resolve_error = None
        self.download_error = None
        self.close_error = None
        self.after_resolve = None
        self.after_download = None
        self.on_close = None
        self.media = CobaltMedia("https://cdn.example/file?signature=secret", "Example title.mp4")

    def resolve(self, url, **options):
        self.resolve_calls.append((url, options))
        options["check_cancelled"]()
        if self.resolve_error:
            raise self.resolve_error
        if self.after_resolve:
            self.after_resolve()
        return self.media

    def download(self, media, **options):
        self.download_calls.append((media, options))
        options["check_cancelled"]()
        if self.download_error:
            raise self.download_error
        options["on_progress"](None)
        options["on_progress"](42.5)
        target = options["target_dir"] / f"{options['source_stem']}.mp4"
        target.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32)
        options["on_progress"](100)
        if self.after_download:
            self.after_download()
        return target

    def close(self):
        self.close_calls += 1
        if self.on_close:
            self.on_close()
        if self.close_error:
            raise self.close_error


@pytest.fixture
def client_factory(monkeypatch):
    clients = []
    customize = {}

    def create(**options):
        client = _Client(**options)
        for name, value in customize.items():
            setattr(client, name, value)
        clients.append(client)
        return client

    monkeypatch.setattr(downloader_cobalt, "CobaltClient", create)
    return clients, customize


class _Child:
    def __init__(self, result):
        self.result = result
        self.validate_calls = []
        self.run_calls = []
        self.close_calls = 0
        self.validate_error = None
        self.run_error = None
        self.close_error = None
        self.on_close = None

    def validate(self, request):
        self.validate_calls.append(request)
        if self.validate_error:
            raise self.validate_error

    def run(self, request):
        self.run_calls.append(request)
        if self.run_error:
            raise self.run_error
        return self.result

    def close(self):
        self.close_calls += 1
        if self.on_close:
            self.on_close()
        if self.close_error:
            raise self.close_error


def _child_factory(monkeypatch, child, *, on_create=None):
    created = []

    class Factory:
        def create_session(self, config, context):
            if on_create:
                on_create()
            created.append((config, context))
            return child

    monkeypatch.setattr(downloader_cobalt, "YtDlpFactory", Factory)
    return created


def test_factory_metadata_and_config_are_lightweight(tmp_path, client_factory, monkeypatch):
    factory = downloader_cobalt.factory
    assert factory.stage == "download"
    assert factory.manifest.plugin_id == "builtin.cobalt"
    assert factory.manifest.protocol_version == 1
    assert factory.manifest.implementation_version == "1"
    assert factory.manifest.operations == frozenset({"download"})
    assert factory.manifest.capabilities == frozenset({"probe"})
    description = factory.describe()
    assert description["bindings"] == []
    assert description["config_schema_version"] == 1
    assert description["config_schema"]["access_mode"]["enum"] == ["anonymous", "api_key"]
    description["config_schema"]["access_mode"]["enum"].append("bad")
    assert factory.describe()["config_schema"]["access_mode"]["enum"] == ["anonymous", "api_key"]
    validated = factory.validate_config(_config(), purpose="download")
    assert validated["credential_ref"] is None
    captured = {}

    def preflight(config, **kwargs):
        captured.update(config=config, **kwargs)

    monkeypatch.setattr(downloader_cobalt, "preflight_cobalt", preflight)
    factory.preflight(validated, operation="download", purpose="download")
    assert captured["config"] == validated
    assert captured["operation"] == "download"
    session = factory.create_session(validated, FakeContext(task_dir=tmp_path, purpose="download"))
    session.close()
    session.close()
    assert client_factory[0] == []


def test_download_uses_frozen_config_key_progress_and_safe_title(tmp_path, client_factory, caplog):
    clients, customize = client_factory
    customize["media"] = CobaltMedia("https://cdn.example/?signature=opaque", r"..\private\Example<>\x00.mp4".replace("\\x00", "\x00"))
    original = _config(access_mode="api_key", proxy="http://proxy.example:8080")
    context = FakeContext(
        task_dir=tmp_path, purpose="download",
        credentials={"settings:cobalt_api_key": "runtime-secret", "settings:cookies_file": "/private/cookie"},
    )
    resolved = []
    resolver = context.resolve_credential
    context.resolve_credential = lambda ref: resolved.append(ref) or resolver(ref)
    session = downloader_cobalt.factory.create_session(original, context)
    original.update(endpoint="https://changed.example/", quality="1080p", fallback_policy="ytdlp")
    with caplog.at_level(logging.INFO, logger=downloader_cobalt.__name__):
        result = session.run(_request(tmp_path))
    session.close()

    assert result == DownloadResult(tmp_path / "source.mp4", "Example__", None)
    assert context.progress == [None, 42.5, 100]
    assert resolved == ["settings:cobalt_api_key"]
    client = clients[0]
    assert client.options == {
        "endpoint": "https://cobalt.example/", "api_key": "runtime-secret",
        "proxy": "http://proxy.example:8080", "socket_timeout": 31,
    }
    url, options = client.resolve_calls[0]
    assert url == _URL
    assert options["video_quality"] == "720"
    assert options["download_mode"] == "auto"
    assert options["filename_style"] == "basic"
    assert options["audio_format"] == "best"
    assert client.download_calls[0][1]["source_stem"] == "source"
    assert client.close_calls == 1
    assert "provider=cobalt" in caplog.text
    assert "runtime-secret" not in caplog.text and "signature=" not in caplog.text
    assert "/private" not in caplog.text


@pytest.mark.parametrize("quality, expected_quality, expected_mode", [
    ("best", "max", "auto"), ("BEST", "max", "auto"),
    ("audio_only", "max", "audio"), ("AUDIO_ONLY", "max", "audio"),
    *((quality, height, "auto") for height in ("4320", "2160", "1440", "1080", "720", "480", "360", "240", "144")
      for quality in (height, f"{height}p", f"{height}P")),
])
def test_quality_mapping_only_uses_documented_options(tmp_path, client_factory, quality, expected_quality, expected_mode):
    session, _ = _session(tmp_path, quality=quality)
    result = session.run(ProbeRequest(_URL))
    options = client_factory[0][0].resolve_calls[0][1]
    assert result.ok
    assert options["video_quality"] == expected_quality
    assert options["download_mode"] == expected_mode
    assert client_factory[0][0].close_calls == 1
    session.close()


@pytest.mark.parametrize("quality", ["900p", "721", "max", "auto", "720pp", "0720p", " 720p", "720p ", "", 720, None, True])
def test_unsupported_quality_never_starts_network_or_fallback(tmp_path, client_factory, monkeypatch, quality):
    created = _child_factory(monkeypatch, _Child(ProbeResult(ok=True)))
    session, _ = _session(tmp_path, quality=quality, fallback_policy="ytdlp")
    with pytest.raises(PluginError) as error:
        session.run(ProbeRequest(_URL))
    assert error.value.code == "invalid_input"
    assert client_factory[0] == [] and created == []
    session.close()


def test_probe_has_only_known_metadata_and_never_downloads(tmp_path, client_factory):
    session, context = _session(tmp_path)
    result = session.run(ProbeRequest(_URL, force_refresh=True))
    assert result == ProbeResult(ok=True, title="Example title", extractor="cobalt", webpage_url=_URL)
    assert result.formats_count == 0 and result.formats == () and result.available_qualities == ()
    assert result.duration is None and result.thumbnail is None and result.uploader is None
    assert context.progress == []
    assert client_factory[0][0].download_calls == []
    assert client_factory[0][0].close_calls == 1
    assert list(tmp_path.iterdir()) == []
    session.close()


@pytest.mark.parametrize("cookies_from_browser", ["Chrome", "firefox", "", 12])
def test_browser_cookie_mode_is_explicitly_rejected(tmp_path, client_factory, monkeypatch, cookies_from_browser):
    created = _child_factory(monkeypatch, _Child(ProbeResult(ok=True)))
    session, _ = _session(tmp_path, fallback_policy="ytdlp")
    with pytest.raises(PluginError, match="browser-cookie") as error:
        session.run(ProbeRequest(_URL, cookies_from_browser=cookies_from_browser))
    assert error.value.code == "invalid_input"
    assert not client_factory[0] and not created
    session.close()


@pytest.mark.parametrize("changes", [
    {"stage": "transcribe"}, {"operation": "audio_extract"}, {"purpose": "extract.asr"},
    {"source_stem": "../other"}, {"quality": "1080p"}, {"target_dir": Path("relative")},
    {"target_dir": Path("/tmp/other-task")}, {"url": ""}, {"url": "file:///tmp/video"},
    {"url": "https://user:secret@video.example/a"}, {"url": "https://video.example:bad/a"},
])
def test_request_identity_and_paths_are_validated(tmp_path, client_factory, changes):
    session, _ = _session(tmp_path)
    with pytest.raises(PluginError) as error:
        session.run(replace(_request(tmp_path), **changes))
    assert error.value.code == "invalid_input"
    assert client_factory[0] == []
    session.close()


def test_invalid_context_wrong_request_and_closed_session(tmp_path, client_factory):
    session = downloader_cobalt.factory.create_session(_config(), FakeContext(task_dir=tmp_path, purpose="translate"))
    with pytest.raises(PluginError, match="purpose"):
        session.run(ProbeRequest(_URL))
    session.close()
    session, _ = _session(tmp_path)
    with pytest.raises(PluginError, match="expected a"):
        session.run(object())
    session.close()
    with pytest.raises(PluginError, match="closed"):
        session.run(ProbeRequest(_URL))
    assert client_factory[0] == []


@pytest.mark.parametrize("credentials", [{}, {"settings:cobalt_api_key": ""}, {"settings:cobalt_api_key": " \t"}])
def test_missing_api_key_remains_explicit_error(tmp_path, client_factory, monkeypatch, credentials):
    context = FakeContext(task_dir=tmp_path, purpose="download", credentials=credentials)
    created = _child_factory(monkeypatch, _Child(ProbeResult(ok=True)))
    session = downloader_cobalt.factory.create_session(_config(access_mode="api_key", fallback_policy="ytdlp"), context)
    with pytest.raises(PluginError) as error:
        session.run(ProbeRequest(_URL))
    assert error.value.code == "credential_missing"
    assert "runtime settings" in error.value.message
    assert client_factory[0] == [] and created == []
    session.close()


def test_anonymous_mode_does_not_resolve_api_key_or_legacy_cookies(tmp_path, client_factory):
    session, context = _session(tmp_path, cookies_ref="settings:cookies_file")

    def no_resolver(ref):
        pytest.fail("Cobalt must not resolve cookies or an anonymous API key")

    context.resolve_credential = no_resolver
    assert session.run(ProbeRequest(_URL)).ok
    assert client_factory[0][0].options["api_key"] is None
    session.close()


@pytest.mark.parametrize("override", [
    {"endpoint": "https://user:secret@cobalt.example/"},
    {"access_mode": "api_key", "credential_ref": None},
    {"access_mode": "anonymous", "credential_ref": "settings:cobalt_api_key"},
    {"use_api_key": True}, {"unexpected": "field"},
])
def test_invalid_configuration_is_not_a_probe_failure_or_fallback(tmp_path, client_factory, monkeypatch, override):
    created = _child_factory(monkeypatch, _Child(ProbeResult(ok=True)))
    session, _ = _session(tmp_path, fallback_policy="ytdlp", **override)
    with pytest.raises(PluginError) as error:
        session.run(ProbeRequest(_URL))
    assert error.value.code == "invalid_input"
    assert "secret@" not in str(error.value)
    assert client_factory[0] == [] and created == []
    session.close()


@pytest.mark.parametrize("code, allowed", [
    ("service_unavailable", True), ("rate_limited", True), ("unsupported_source", True),
    ("unsupported_response", True), ("access_denied", False), ("invalid_result", False),
    ("execution_error", False),
])
def test_default_probe_failure_is_safe_and_never_falls_back(tmp_path, client_factory, monkeypatch, code, allowed):
    client_factory[1]["resolve_error"] = CobaltError(code, "Safe Cobalt failure", fallback_allowed=allowed)
    created = _child_factory(monkeypatch, _Child(ProbeResult(ok=True)))
    session, _ = _session(tmp_path)
    result = session.run(ProbeRequest(_URL))
    assert result == ProbeResult(ok=False, extractor="cobalt", webpage_url=_URL, reason=code, detail="Safe Cobalt failure")
    assert client_factory[0][0].close_calls == 1
    assert created == []
    session.close()


@pytest.mark.parametrize("code", ["invalid_input", "credential_missing", "dependency_missing"])
def test_explicit_client_errors_never_become_failed_probe_or_fallback(tmp_path, client_factory, monkeypatch, code):
    failure = CobaltError(code, "Explicit failure", fallback_allowed=True)
    client_factory[1]["resolve_error"] = failure
    created = _child_factory(monkeypatch, _Child(ProbeResult(ok=True)))
    session, _ = _session(tmp_path, fallback_policy="ytdlp")
    with pytest.raises(CobaltError) as error:
        session.run(ProbeRequest(_URL))
    assert error.value is failure
    assert client_factory[0][0].close_calls == 1 and created == []
    session.close()


@pytest.mark.parametrize("failure", [
    CobaltError("access_denied", "Protected source"),
    CobaltError("invalid_result", "Malformed response"),
    ValueError("programming error"), PluginCancelled("cancelled"),
])
def test_noneligible_errors_and_bugs_never_trigger_fallback(tmp_path, client_factory, monkeypatch, failure):
    client_factory[1]["resolve_error"] = failure
    created = _child_factory(monkeypatch, _Child(DownloadResult(tmp_path / "source.mp4")))
    session, _ = _session(tmp_path, fallback_policy="ytdlp")
    with pytest.raises(type(failure)) as error:
        session.run(_request(tmp_path))
    assert error.value is failure
    assert client_factory[0][0].close_calls == 1 and created == []
    session.close()


def test_opt_in_fallback_runs_adapter_after_releasing_limiter(tmp_path, client_factory, monkeypatch, caplog):
    failure = CobaltError("service_unavailable", "Cobalt temporarily unavailable", fallback_allowed=True)
    client_factory[1]["download_error"] = failure
    active = 0

    class Limiter:
        @contextmanager
        def slot(self):
            nonlocal active
            active += 1
            try:
                yield 0
            finally:
                active -= 1

    def assert_released():
        assert active == 0
        assert client_factory[0][0].close_calls == 1

    monkeypatch.setattr(downloader_cobalt.downloader, "_download_limiter", Limiter())
    result = DownloadResult(tmp_path / "source.mkv", "yt-dlp title", 23.0)
    child = _Child(result)
    created = _child_factory(monkeypatch, child, on_create=assert_released)
    config = _config(fallback_policy="ytdlp", cookies_ref="settings:cookies_file")
    context = FakeContext(task_dir=tmp_path, purpose="download")
    session = downloader_cobalt.factory.create_session(config, context)
    with caplog.at_level(logging.INFO, logger=downloader_cobalt.__name__):
        actual = session.run(_request(tmp_path))
    assert actual is result
    assert created == [({key: config[key] for key in YTDLP_CONFIG_KEYS}, context)]
    assert child.validate_calls == [_request(tmp_path)] and child.run_calls == [_request(tmp_path)]
    assert child.close_calls == 1 and active == 0
    assert "provider=cobalt -> yt-dlp" in caplog.text
    assert "provider=yt-dlp (Cobalt fallback)" in caplog.text
    assert "signature=" not in caplog.text and _URL not in caplog.text
    session.close()


def test_successful_fallback_probe_preserves_metadata_and_marks_provider(tmp_path, client_factory, monkeypatch):
    client_factory[1]["resolve_error"] = CobaltError("unsupported_source", "Unsupported by Cobalt", fallback_allowed=True)
    format_entry = ProbeFormat("22", "mp4", "720p", 720, 1280, 30, "h264", "aac", 1024, None, None, "video")
    result = ProbeResult(ok=True, title="yt-dlp title", extractor="Example", duration=23, formats_count=1,
                         webpage_url=_URL, available_qualities=("720p",), formats=(format_entry,))
    child = _Child(result)
    _child_factory(monkeypatch, child)
    session, _ = _session(tmp_path, fallback_policy="ytdlp")
    request = ProbeRequest(_URL, force_refresh=True)
    actual = session.run(request)
    assert actual.extractor == "yt-dlp (Cobalt fallback; Example)"
    assert replace(actual, extractor=result.extractor) == result
    assert child.run_calls == [request] and child.close_calls == 1
    session.close()


@pytest.mark.parametrize("primary", [PluginCancelled("cancelled"), CobaltError("access_denied", "Safe primary")])
def test_cobalt_close_failure_does_not_mask_primary_or_cancel(tmp_path, client_factory, primary, caplog):
    client_factory[1].update(resolve_error=primary, close_error=ValueError("secret-close-url"))
    session, _ = _session(tmp_path)
    with caplog.at_level(logging.WARNING, logger=downloader_cobalt.__name__):
        with pytest.raises(type(primary)) as error:
            session.run(_request(tmp_path))
    assert error.value is primary
    assert isinstance(error.value.cleanup_error, ValueError)
    assert client_factory[0][0].close_calls == 1
    assert "secret-close-url" not in caplog.text
    session.close()


@pytest.mark.parametrize("failure_phase", ["validate", "run"])
@pytest.mark.parametrize("primary", [
    PluginCancelled("cancelled"),
    PluginError("invalid_input", "download", "builtin.ytdlp", "private-cookie-path"),
])
def test_fallback_close_preserves_primary_and_closes_on_validation_error(tmp_path, client_factory, monkeypatch, failure_phase, primary, caplog):
    cobalt_failure = CobaltError("rate_limited", "Cobalt limit reached", fallback_allowed=True)
    client_factory[1]["resolve_error"] = cobalt_failure
    child = _Child(DownloadResult(tmp_path / "source.mkv"))
    setattr(child, f"{failure_phase}_error", primary)
    child.close_error = RuntimeError("private-close-details")
    _child_factory(monkeypatch, child)
    session, _ = _session(tmp_path, fallback_policy="ytdlp")
    with caplog.at_level(logging.WARNING, logger=downloader_cobalt.__name__):
        with pytest.raises(type(primary)) as error:
            session.run(_request(tmp_path))
    if isinstance(primary, PluginCancelled):
        assert error.value is primary
    else:
        assert error.value.code == primary.code
        assert error.value.cause is primary
        assert error.value.cobalt_error is cobalt_failure
        assert "Cobalt limit reached" in error.value.message
        assert "yt-dlp fallback failed" in error.value.message
    assert error.value.cleanup_error is child.close_error
    assert child.close_calls == 1 and client_factory[0][0].close_calls == 1
    assert "private-close-details" not in caplog.text and "private-cookie-path" not in caplog.text
    session.close()


@pytest.mark.parametrize("phase", ["before_client", "resolve", "after_resolve", "progress", "client_close", "child_close"])
def test_cancellation_at_boundaries_is_preserved_and_resources_close(tmp_path, client_factory, monkeypatch, phase):
    session, context = _session(tmp_path, fallback_policy="ytdlp")
    child = _Child(DownloadResult(tmp_path / "source.mkv"))
    created = _child_factory(monkeypatch, child)

    def cancel():
        context.cancelled = True

    if phase == "before_client":
        cancel()
    elif phase == "resolve":
        client_factory[1]["resolve_error"] = PluginCancelled("cancelled during resolve")
    elif phase == "after_resolve":
        client_factory[1]["after_resolve"] = cancel
    elif phase == "progress":
        context.report_progress = lambda percent: (cancel(), context.check_cancelled())
    elif phase == "client_close":
        client_factory[1]["on_close"] = cancel
    else:
        client_factory[1]["resolve_error"] = CobaltError("rate_limited", "Rate limited", fallback_allowed=True)
        child.on_close = cancel
    with pytest.raises(PluginCancelled):
        session.run(_request(tmp_path))
    assert len(client_factory[0]) == (0 if phase == "before_client" else 1)
    assert all(client.close_calls == 1 for client in client_factory[0])
    assert len(created) == (1 if phase == "child_close" else 0)
    assert child.close_calls == (1 if phase == "child_close" else 0)
    session.close()


def test_cancellation_while_waiting_for_limiter_is_checked_before_resolution(tmp_path, client_factory, monkeypatch):
    session, context = _session(tmp_path)

    class Limiter:
        @contextmanager
        def slot(self):
            context.cancelled = True
            yield 0

    monkeypatch.setattr(downloader_cobalt.downloader, "_download_limiter", Limiter())
    with pytest.raises(PluginCancelled):
        session.run(_request(tmp_path))
    assert client_factory[0][0].resolve_calls == []
    assert client_factory[0][0].close_calls == 1
    session.close()


@pytest.mark.parametrize("child_result", [
    ProbeResult(ok=False, reason="raw URL https://secret.example/?session=opaque", detail="private details"),
    PluginError("execution_error", "download", "builtin.ytdlp", "private details"),
])
def test_both_probe_failures_return_safe_actionable_detail(tmp_path, client_factory, monkeypatch, child_result, caplog):
    client_factory[1]["resolve_error"] = CobaltError("service_unavailable", "Cobalt temporarily unavailable", fallback_allowed=True)
    child = _Child(child_result)
    if isinstance(child_result, Exception):
        child.run_error = child_result
    _child_factory(monkeypatch, child)
    session, _ = _session(tmp_path, fallback_policy="ytdlp")
    with caplog.at_level(logging.WARNING, logger=downloader_cobalt.__name__):
        result = session.run(ProbeRequest(_URL))
    assert not result.ok and result.extractor == "yt-dlp (Cobalt fallback)"
    assert "Cobalt temporarily unavailable" in result.detail
    assert "yt-dlp" in result.detail
    assert "cookies" in result.detail
    assert "private details" not in str(result) + caplog.text
    assert "secret.example" not in str(result) + caplog.text
    assert child.close_calls == 1
    session.close()


def test_missing_fallback_cookie_credential_is_explicit_and_safe(tmp_path, client_factory, monkeypatch):
    client_factory[1]["resolve_error"] = CobaltError("service_unavailable", "Cobalt unavailable", fallback_allowed=True)
    child = _Child(ProbeResult(ok=True))
    child.run_error = PluginError("credential_missing", "download", "builtin.ytdlp", "missing private cookie")
    _child_factory(monkeypatch, child)
    session, _ = _session(tmp_path, fallback_policy="ytdlp")
    with pytest.raises(PluginError) as error:
        session.run(ProbeRequest(_URL))
    assert error.value.code == "credential_missing"
    assert error.value.cause is child.run_error
    assert "private cookie" not in str(error.value)
    assert child.close_calls == 1
    session.close()


def test_successful_child_with_cleanup_failure_is_not_success(tmp_path, client_factory, monkeypatch):
    client_factory[1]["resolve_error"] = CobaltError("unsupported_source", "Cobalt unsupported", fallback_allowed=True)
    child = _Child(DownloadResult(tmp_path / "source.mkv"))
    child.close_error = ValueError("private cleanup details")
    _child_factory(monkeypatch, child)
    session, _ = _session(tmp_path, fallback_policy="ytdlp")
    with pytest.raises(PluginError) as error:
        session.run(_request(tmp_path))
    assert error.value.code == "cleanup_error"
    assert "private cleanup details" not in str(error.value)
    assert child.close_calls == 1
    session.close()


@pytest.mark.parametrize("cleanup_error", [PluginCancelled("cancelled during close"), RuntimeError("close failed")])
def test_recoverable_primary_with_failed_cleanup_never_falls_back(tmp_path, client_factory, monkeypatch, cleanup_error):
    primary = CobaltError("service_unavailable", "Cobalt unavailable", fallback_allowed=True)
    client_factory[1].update(resolve_error=primary, close_error=cleanup_error)
    created = _child_factory(monkeypatch, _Child(DownloadResult(tmp_path / "source.mkv")))
    session, _ = _session(tmp_path, fallback_policy="ytdlp")
    expected = cleanup_error if isinstance(cleanup_error, PluginCancelled) else primary
    with pytest.raises(type(expected)) as error:
        session.run(_request(tmp_path))
    assert error.value is expected
    assert primary.cleanup_error is cleanup_error
    assert created == [] and client_factory[0][0].close_calls == 1
    session.close()


def test_existing_zero_retry_fallback_validation_error_remains_visible(tmp_path, client_factory):
    client_factory[1]["resolve_error"] = CobaltError("rate_limited", "Cobalt rate limited", fallback_allowed=True)
    session, _ = _session(tmp_path, fallback_policy="ytdlp", retries=0)
    with pytest.raises(PluginError) as error:
        session.run(_request(tmp_path))
    assert error.value.code == "invalid_input"
    assert "yt-dlp fallback failed" in str(error.value)
    assert "retries" in str(error.value.cause)
    assert client_factory[0][0].close_calls == 1
    session.close()
