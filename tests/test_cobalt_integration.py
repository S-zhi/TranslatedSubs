"""Offline vertical coverage for configured cobalt probes and frozen downloads."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Follow the public facade's initialization order before importing route modules.
from src.handler import tasks
from src.config import storage
from src.config.config import Settings
from src.config.config_repository import MemoryConfigProvider
from src.handler import business_settings, deps, plugin_settings
from src.plugins import get_plugin_manager
from src.plugins.builtin.configuration import BuiltinFactory
from src.service import runner
from src.service.asset_resolver import AssetResolver
from src.service.plugin_plan import parse_plugin_plan
from src.service.task import task_lifecycle
from src.store import ProbeStore, TaskStore, TranslationEngineStore
from src.store.plugin_instance_store import PluginInstanceStore


_ENDPOINT = "https://cobalt.example.test/api/"
_SOURCE_URL = "https://video.example.test/watch?v=offline-cobalt"
_MEDIA_URL = _ENDPOINT + "tunnel/media?ticket=signed-media-ticket"
_API_KEY = "cobalt-private-key-at-creation"
_ROTATED_KEY = "cobalt-private-key-after-rotation"
_COOKIE = "private-api-response-cookie"
_RAW_ERROR = "private-upstream-body-with-credentials"
_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"media" * 100
_WAV = b"RIFF\x28\x00\x00\x00WAVEfmt " + b"audio" * 100
_WEBM = b"\x1a\x45\xdf\xa3" + b"webm" * 100
_PLUGIN_BASE = "/api/settings/plugins"


@dataclass
class _Workbench:
    client: TestClient
    provider: MemoryConfigProvider
    store: TaskStore
    probes: ProbeStore
    instances: PluginInstanceStore
    queued: list[str]
    root: Path

    def instance(self, **config):
        item, _ = self.instances.create(
            {
                "name": "Offline cobalt", "stage": "download",
                "plugin_id": "builtin.cobalt", "implementation_version": "1",
                "config": {"endpoint": _ENDPOINT, **config},
            },
            expected_revision=self.provider.read().revision,
        )
        return item

    def task(self, *, instance_id=None, url=_SOURCE_URL, quality="720p"):
        payload = {"url": url, "quality": quality, "needSubtitle": False}
        if instance_id is not None:
            payload["pluginInstances"] = {"download": instance_id}
        response = self.client.post("/api/tasks", json=payload)
        assert response.status_code == 201, response.text
        task = self.store.get(response.json()["id"])
        assert task is not None
        assert task.id in self.queued
        return task

    def run(self, task_id):
        # Consume the queued task synchronously; keep the actual runner,
        # orchestrator, plugin manager, adapter and artifact publication intact.
        runner._run(task_id)
        response = self.client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def runtime(self, **values):
        response = self.client.patch(
            "/api/settings/runtime", json=values,
            headers={"If-Match": str(self.provider.read().revision)},
        )
        assert response.status_code == 200, response.text
        return response


@pytest.fixture
def workbench(tmp_path, monkeypatch, memory_config_provider):
    """Use real stores and routes without application startup or worker threads."""
    from src.core import downloader

    def unexpected_ytdlp(*_args, **_kwargs):
        pytest.fail("unexpected yt-dlp network access in an offline cobalt scenario")

    monkeypatch.setattr(downloader, "YoutubeDL", unexpected_ytdlp)
    monkeypatch.delenv("SUBTRANS_DESKTOP", raising=False)
    monkeypatch.delenv("SUBTRANS_PIPELINE_PLUGINS", raising=False)
    root = tmp_path / "artifacts"
    monkeypatch.setattr(storage, "settings", Settings(data_dir=root))
    # Only yt-dlp's external-tool readiness is irrelevant to instance selection.
    monkeypatch.setattr(BuiltinFactory, "_binary", lambda *_: None)
    get_plugin_manager.cache_clear()
    store = TaskStore(tmp_path / "tasks.sqlite3")
    probes = ProbeStore(tmp_path / "probes.sqlite3")
    engines = TranslationEngineStore(provider=memory_config_provider)
    monkeypatch.setattr(runner, "_store", store)
    monkeypatch.setattr(runner, "_engine_store", engines)
    queued = []
    monkeypatch.setattr(tasks, "enqueue_pipeline", queued.append)
    app = FastAPI()
    app.include_router(task_lifecycle.router)
    app.include_router(plugin_settings.router)
    app.include_router(business_settings.router)
    app.dependency_overrides[deps.get_store] = lambda: store
    app.dependency_overrides[deps.get_probe_store] = lambda: probes
    app.dependency_overrides[deps.get_translation_engine_store] = lambda: engines
    app.dependency_overrides[deps.require_api_token] = lambda: None
    with TestClient(app) as client:
        yield _Workbench(
            client, memory_config_provider, store, probes,
            PluginInstanceStore(provider=memory_config_provider), queued, root,
        )
    get_plugin_manager.cache_clear()


@dataclass
class _CobaltHTTP:
    requests: list[httpx.Request]
    api_response: Callable[[httpx.Request], httpx.Response]
    media_response: Callable[[httpx.Request], httpx.Response]

    def handle(self, request):
        self.requests.append(request)
        if request.method == "POST":
            return self.api_response(request)
        if request.method == "GET":
            return self.media_response(request)
        pytest.fail(f"unexpected HTTP method: {request.method}")

    @property
    def posts(self):
        return [request for request in self.requests if request.method == "POST"]

    @property
    def gets(self):
        return [request for request in self.requests if request.method == "GET"]


def _resolved_media(request):
    audio = json.loads(request.content)["downloadMode"] == "audio"
    return httpx.Response(
        200, json={
            "status": "tunnel", "url": _MEDIA_URL + ("&audio=1" if audio else ""),
            "filename": "Offline clip.wav" if audio else "Offline clip.mp4",
        },
        # Same-origin media is deliberate: sharing the API client's cookie jar
        # would leak this cookie even without a cross-origin request.
        headers={"Set-Cookie": f"session={_COOKIE}; Path=/; Secure"},
    )


def _media_bytes(request):
    audio = request.url.params.get("audio") == "1"
    return httpx.Response(
        200, content=_WAV if audio else _MP4,
        headers={"Content-Type": "audio/wav" if audio else "video/mp4"},
    )


@pytest.fixture
def cobalt_http(monkeypatch):
    from src.plugins.builtin import cobalt_client

    boundary = _CobaltHTTP([], _resolved_media, _media_bytes)
    real_client = httpx.Client

    def offline_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(boundary.handle)
        return real_client(*args, **kwargs)

    def unexpected_live_http(*_args, **_kwargs):
        pytest.fail("HTTPX bypassed MockTransport in an offline cobalt scenario")

    monkeypatch.setattr(cobalt_client.httpx, "Client", offline_client)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected_live_http)
    return boundary


def _assert_no_publication(workbench, task_id):
    directory = workbench.root / task_id
    assert not list(directory.glob("source.*"))
    assert not list(directory.glob("*.part"))
    assert not AssetResolver.plugin_meta_path(task_id, "download").exists()
    assert workbench.store.get(task_id).output_video is None


@pytest.mark.parametrize("selection,quality,mode,video_quality,extension,media", [
    ("explicit", "720p", "auto", "720", "mp4", _MP4),
    ("default", "best", "auto", "max", "mp4", _MP4),
    ("explicit", "audio_only", "audio", "max", "wav", _WAV),
])
def test_probe_and_frozen_task_use_same_selected_cobalt_after_instance_changes(
    workbench, cobalt_http, selection, quality, mode, video_quality, extension, media,
):
    instance = workbench.instance(
        endpoint=_ENDPOINT.rstrip("/"), socket_timeout=7,
        filename_style="pretty", audio_format="wav",
    )
    if selection == "default":
        workbench.instances.set_defaults(
            {"download": instance["id"]},
            expected_revision=workbench.provider.read().revision,
        )
    chosen = {"download": instance["id"]} if selection == "explicit" else None
    probe = workbench.client.post("/api/tasks/probe", json={
        "url": _SOURCE_URL, "quality": quality, "pluginInstances": chosen,
    })
    assert probe.status_code == 200, probe.text
    assert probe.json()["ok"] is True
    assert probe.json()["extractor"] == "cobalt"
    assert probe.json()["duration"] is None
    assert probe.json()["formatsCount"] == 0
    assert probe.json()["availableQualities"] == probe.json()["formats"] == []
    assert cobalt_http.gets == []  # Probing must not download media.
    assert len(workbench.probes.list()) == 1
    task = workbench.task(
        instance_id=instance["id"] if chosen else None, quality=quality,
    )
    saved_plan = task.plugin_plan_json
    plan = parse_plugin_plan(saved_plan)
    slot = plan["slots"]["download"]
    assert plan["schema_version"] == 2
    assert slot["instance_id"] == instance["id"]
    assert slot["plugin_id"] == "builtin.cobalt"
    assert slot["config"]["endpoint"] == _ENDPOINT
    assert slot["config"]["credential_ref"] is None
    assert slot["config"]["fallback_policy"] == "none"

    workbench.instances.update(
        instance["id"], {"config": {
            "endpoint": "https://replacement.example.test/", "socket_timeout": 19,
            "filename_style": "nerdy", "audio_format": "mp3", "access_mode": "api_key",
        }}, expected_revision=workbench.provider.read().revision,
    )
    workbench.runtime(download_quality="360p", download_socket_timeout=23)
    result = workbench.run(task.id)
    assert result["status"] == "SUCCESS", result
    assert result["outputs"]["video"] == f"/api/tasks/{task.id}/download"
    assert workbench.store.get(task.id).plugin_plan_json == saved_plan
    assert workbench.store.get(task.id).output_video == f"source.{extension}"
    assert AssetResolver.require_source(task.id).read_bytes() == media
    assert len(cobalt_http.posts) == 2
    expected = {
        "url": _SOURCE_URL, "videoQuality": video_quality, "downloadMode": mode,
        "filenameStyle": "pretty", "audioFormat": "wav", "localProcessing": "disabled",
    }
    for request in cobalt_http.posts:
        assert str(request.url) == _ENDPOINT
        assert json.loads(request.content) == expected
        assert request.extensions["timeout"]["read"] == 7
    assert len(cobalt_http.gets) == 1
    assert all("authorization" not in req.headers and "cookie" not in req.headers
               for req in cobalt_http.requests)


def test_registering_cobalt_preserves_ytdlp_default_and_existing_v1_task(
    workbench, cobalt_http,
):
    workbench.instance()
    defaults = workbench.client.get(f"{_PLUGIN_BASE}/instances").json()
    assert len(defaults["defaults"]) == 7
    assert defaults["defaults"]["download"] == "default_download"
    task = workbench.task()
    slot = parse_plugin_plan(task.plugin_plan_json)["slots"]["download"]
    assert slot["plugin_id"] == "builtin.ytdlp"
    assert "endpoint" not in slot["config"]
    assert cobalt_http.requests == []

    # A literal historical schema, not a v2 plan transformed by new code.
    legacy_plan = {"schema_version": 1, "slots": {"download": {
        "plugin_id": "builtin.ytdlp", "implementation_version": "1", "config": {
            "quality": "720p", "format_selector": "bv*+ba/b", "proxy": None,
            "socket_timeout": 30, "cookies_ref": None, "merge_output_format": "mp4",
            "retries": 5, "concurrent_fragments": 4,
        },
    }}}
    encoded = json.dumps(legacy_plan)
    old = workbench.store.create(
        url="https://video.example.test/legacy", source_lang="en", target_lang="zh-CN",
        mode="mono", burn="soft", model="local:tiny", engine="deepseek",
        need_subtitle=False, plugin_plan_json=encoded,
    )
    directory = workbench.root / old.id
    directory.mkdir(parents=True)
    (directory / "source.mp4").write_bytes(_MP4)
    metadata = AssetResolver.plugin_metadata(
        task_id=old.id, stage="download", operation="download", purpose="download",
        plugin_id="builtin.ytdlp", implementation_version="1",
        config=legacy_plan["slots"]["download"]["config"],
        inputs={"url": old.url}, output_names=("source.mp4",),
    )
    AssetResolver.write_plugin_cache_metadata(old.id, metadata)
    assert parse_plugin_plan(encoded) == legacy_plan
    result = workbench.run(old.id)
    assert result["status"] == "SUCCESS", result
    assert workbench.store.get(old.id).plugin_plan_json == encoded
    assert workbench.store.get(old.id).output_video == "source.mp4"
    assert cobalt_http.requests == []


@pytest.mark.parametrize("access_mode", ["anonymous", "api_key"])
def test_access_mode_gates_current_key_without_leaking_into_task_or_media(
    workbench, cobalt_http, tmp_path, caplog, access_mode,
):
    caplog.set_level(logging.INFO)
    cookies = tmp_path / "browser-cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    saved = workbench.runtime(cobalt_api_key=_API_KEY, cookies_file=str(cookies))
    assert "cobalt_api_key" not in saved.json()["values"]
    assert saved.json()["hasSecrets"]["cobalt_api_key"] is True
    instance = workbench.instance(access_mode=access_mode)
    payload = {"url": _SOURCE_URL, "quality": "720p",
               "pluginInstances": {"download": instance["id"]}}
    probe = workbench.client.post("/api/tasks/probe", json=payload)
    assert probe.status_code == 200, probe.text
    assert probe.json()["ok"] is True
    task = workbench.task(instance_id=instance["id"])
    slot = parse_plugin_plan(task.plugin_plan_json)["slots"]["download"]
    assert slot["config"]["access_mode"] == access_mode
    assert slot["config"]["credential_ref"] == (
        "settings:cobalt_api_key" if access_mode == "api_key" else None
    )
    rotated = workbench.runtime(cobalt_api_key=_ROTATED_KEY)
    result = workbench.run(task.id)
    assert result["status"] == "SUCCESS", result
    assert [req.headers.get("authorization") for req in cobalt_http.posts] == (
        [f"Api-Key {_API_KEY}", f"Api-Key {_ROTATED_KEY}"]
        if access_mode == "api_key" else [None, None]
    )
    assert len(cobalt_http.gets) == 1
    assert "authorization" not in cobalt_http.gets[0].headers
    assert all("cookie" not in req.headers for req in cobalt_http.requests)
    cache = AssetResolver.plugin_meta_path(task.id, "download").read_text(encoding="utf-8")
    exposures = [
        saved.text, rotated.text, probe.text, json.dumps(result),
        workbench.client.get("/api/settings/runtime").text,
        workbench.client.get(f"{_PLUGIN_BASE}/instances").text,
        repr(workbench.store.get(task.id)), repr(workbench.probes.list()),
        task.plugin_plan_json, task.runtime_config_json, cache, caplog.text,
    ]
    for exposed in exposures:
        assert all(secret not in exposed for secret in (
            _API_KEY, _ROTATED_KEY, _COOKIE, "signed-media-ticket",
        ))


def test_missing_api_key_is_actionable_before_network_and_never_publishes(
    workbench, cobalt_http,
):
    instance = workbench.instance(access_mode="api_key", fallback_policy="ytdlp")
    probe = workbench.client.post("/api/tasks/probe", json={
        "url": _SOURCE_URL, "pluginInstances": {"download": instance["id"]},
    })
    assert probe.status_code == 422, probe.text
    assert probe.json()["detail"]["code"] == "credential_missing"
    assert workbench.probes.list() == []
    task = workbench.task(instance_id=instance["id"])
    result = workbench.run(task.id)
    assert result["status"] == "FAILED"
    assert result["errorCode"] == "credential_missing"
    assert result["error"]
    assert result["outputs"] is None
    assert cobalt_http.requests == []
    _assert_no_publication(workbench, task.id)


@pytest.mark.parametrize("damage", ["unapproved_ref", "different_plugin", "anonymous_ref"])
def test_persisted_credentials_require_the_selected_plugin_and_access_mode(
    workbench, cobalt_http, damage,
):
    workbench.runtime(cobalt_api_key=_API_KEY, transcriber_api_key="unrelated-asr-secret")
    instance = workbench.instance(access_mode="api_key")
    task = workbench.task(instance_id=instance["id"])
    plan = json.loads(task.plugin_plan_json)
    slot = plan["slots"]["download"]
    if damage == "unapproved_ref":
        slot["config"]["credential_ref"] = "settings:transcriber_api_key"
    elif damage == "different_plugin":
        slot["plugin_id"] = "builtin.ytdlp"
    else:
        slot["config"]["access_mode"] = "anonymous"
    workbench.store.update(task.id, plugin_plan_json=json.dumps(plan))
    result = workbench.run(task.id)
    assert result["status"] == "FAILED"
    assert result["errorCode"] == (
        "invalid_input" if damage == "anonymous_ref" else "plugin_plan_invalid"
    )
    assert _API_KEY not in json.dumps(result)
    assert "unrelated-asr-secret" not in json.dumps(result)
    assert cobalt_http.requests == []
    _assert_no_publication(workbench, task.id)


@pytest.mark.parametrize("body,error_code", [
    ({"status": "picker", "picker": [{"type": "video", "url": _MEDIA_URL}]},
     "unsupported_response"),
    ({"status": "local-processing", "type": "merge", "tunnel": [_MEDIA_URL],
      "output": {"filename": "clip.mp4"}}, "unsupported_response"),
    ({"status": "unexpected", "error": {"context": _RAW_ERROR}}, "invalid_result"),
])
def test_unsupported_or_invalid_response_is_safe_and_creates_no_artifact(
    workbench, cobalt_http, caplog, body, error_code,
):
    caplog.set_level(logging.INFO)
    cobalt_http.api_response = lambda _request: httpx.Response(200, json=body)
    instance = workbench.instance()
    probe = workbench.client.post("/api/tasks/probe", json={
        "url": _SOURCE_URL, "pluginInstances": {"download": instance["id"]},
    })
    assert probe.status_code == 200, probe.text
    assert probe.json()["ok"] is False
    assert probe.json()["reason"] == error_code
    task = workbench.task(instance_id=instance["id"])
    result = workbench.run(task.id)
    assert result["status"] == "FAILED"
    assert result["errorCode"] == error_code
    assert result["error"]
    assert result["outputs"] is None
    assert cobalt_http.gets == []
    assert _RAW_ERROR not in probe.text + json.dumps(result) + caplog.text
    _assert_no_publication(workbench, task.id)


class _InterruptedMedia(httpx.SyncByteStream):
    def __init__(self, after_first):
        self.after_first = after_first

    def __iter__(self):
        # Enough data to flush a client chunk before interruption/cancellation.
        yield _MP4 + b"\x00" * (256 * 1024)
        self.after_first()
        yield b"\x00" * (256 * 1024)


@pytest.mark.parametrize("cancelled", [False, True])
def test_interrupted_media_does_not_publish_or_remove_unrelated_files(
    workbench, cobalt_http, caplog, cancelled,
):
    caplog.set_level(logging.INFO)
    instance = workbench.instance()
    task = workbench.task(instance_id=instance["id"])
    directory = workbench.root / task.id
    directory.mkdir(parents=True)
    unrelated = directory / "keep.txt"
    unrelated.write_text("unrelated file", encoding="utf-8")

    def interrupt():
        if cancelled:
            runner.set_cancelled_signal(task.id)
        else:
            raise httpx.ReadError(_RAW_ERROR)

    cobalt_http.media_response = lambda _request: httpx.Response(
        200, headers={"Content-Type": "video/mp4"},
        stream=_InterruptedMedia(interrupt),
    )
    result = workbench.run(task.id)
    assert result["status"] == ("CANCELLED" if cancelled else "FAILED"), result
    assert result["outputs"] is None
    assert result["errorCode"]
    assert len(cobalt_http.gets) == 1
    assert unrelated.read_text(encoding="utf-8") == "unrelated file"
    assert _RAW_ERROR not in json.dumps(result) + caplog.text
    _assert_no_publication(workbench, task.id)


def test_replacing_stale_source_extension_publishes_and_reuses_new_artifact(
    workbench, cobalt_http,
):
    instance = workbench.instance()
    task = workbench.task(instance_id=instance["id"])
    directory = workbench.root / task.id
    directory.mkdir(parents=True)
    (directory / "source.mp4").write_bytes(_MP4)
    cobalt_http.api_response = lambda _request: httpx.Response(200, json={
        "status": "redirect", "url": _MEDIA_URL, "filename": "replacement.webm",
    })
    cobalt_http.media_response = lambda _request: httpx.Response(
        200, content=_WEBM, headers={"Content-Type": "video/webm"},
    )
    result = workbench.run(task.id)
    assert result["status"] == "SUCCESS", result
    assert workbench.store.get(task.id).output_video == "source.webm"
    assert AssetResolver.require_source(task.id).read_bytes() == _WEBM
    assert not (directory / "source.mp4").exists()
    metadata = json.loads(AssetResolver.plugin_meta_path(task.id, "download").read_text())
    assert metadata["output_names"] == ["source.webm"]
    requests_before_retry = len(cobalt_http.requests)
    workbench.store.update(task.id, status="FAILED", error="interrupted after download")
    retry = workbench.client.post(f"/api/tasks/{task.id}/retry")
    assert retry.status_code == 200, retry.text
    result = workbench.run(task.id)
    assert result["status"] == "SUCCESS", result
    assert workbench.store.get(task.id).output_video == "source.webm"
    assert len(cobalt_http.requests) == requests_before_retry


def test_explicit_fallback_uses_real_ytdlp_adapter_with_frozen_download_options(
    workbench, cobalt_http, monkeypatch, tmp_path, caplog,
):
    from src.core import downloader

    caplog.set_level(logging.INFO)
    cookies = tmp_path / "fallback-cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    workbench.runtime(
        cookies_file=str(cookies), download_retries=2, download_concurrent_fragments=3,
        download_proxy=None, merge_output_format="mp4", probe_cache_ttl_sec=0,
    )
    instance = workbench.instance(fallback_policy="ytdlp", socket_timeout=7)
    # External SDK boundary only: YtDlpFactory, YtDlpSession and download_video
    # remain real and must successfully validate/delegate the frozen config.
    sdk_calls = []

    class OfflineYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def extract_info(self, url, download=False):
            sdk_calls.append((url, download, dict(self.options)))
            info = {
                "title": "yt-dlp fallback clip", "duration": 4.0,
                "url": "https://media.example.test/fallback.mp4", "extractor": "Generic",
            }
            if download:
                output = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
                output.write_bytes(_MP4)
                for hook in self.options.get("progress_hooks", []):
                    hook({"status": "finished", "downloaded_bytes": len(_MP4),
                          "total_bytes": len(_MP4)})
                info["requested_downloads"] = [{"filepath": str(output)}]
            return info

    monkeypatch.setattr(downloader, "YoutubeDL", OfflineYoutubeDL)
    cobalt_http.api_response = lambda _request: httpx.Response(400, json={
        "status": "error", "error": {
            "code": "error.api.service.unsupported", "context": {"service": _RAW_ERROR},
        },
    })
    probe = workbench.client.post("/api/tasks/probe", json={
        "url": _SOURCE_URL, "quality": "720p",
        "pluginInstances": {"download": instance["id"]},
    })
    assert probe.status_code == 200, probe.text
    assert probe.json()["ok"] is True
    assert "yt-dlp" in probe.json()["extractor"]
    assert "fallback" in probe.json()["extractor"].lower()
    task = workbench.task(instance_id=instance["id"])
    saved_plan = task.plugin_plan_json
    workbench.instances.update(
        instance["id"], {"config": {"endpoint": _ENDPOINT, "fallback_policy": "none"}},
        expected_revision=workbench.provider.read().revision,
    )
    workbench.runtime(
        download_retries=9, download_concurrent_fragments=8, download_socket_timeout=23,
        download_proxy="http://replacement-proxy.example.test:8080", merge_output_format="mkv",
    )
    result = workbench.run(task.id)
    assert result["status"] == "SUCCESS", result
    assert result["title"] == "yt-dlp fallback clip"
    assert AssetResolver.require_source(task.id).read_bytes() == _MP4
    assert workbench.store.get(task.id).plugin_plan_json == saved_plan
    assert len(sdk_calls) == 2
    assert [download for _url, download, _options in sdk_calls] == [False, True]
    for url, _download, options in sdk_calls:
        assert url == _SOURCE_URL
        assert options["format"] == "bv*[height<=720]+ba/b[height<=720]"
        assert options["socket_timeout"] == 7
        assert options["cookiefile"] == str(cookies)
        assert options.get("proxy") is None
    download_options = sdk_calls[-1][2]
    assert download_options["retries"] == 2
    assert download_options["concurrent_fragment_downloads"] == 3
    assert download_options["merge_output_format"] == "mp4"
    assert len(cobalt_http.posts) == 2
    assert cobalt_http.gets == []
    assert any(
        "cobalt" in record.getMessage().lower()
        and "yt-dlp" in record.getMessage().lower()
        and "fallback" in record.getMessage().lower()
        for record in caplog.records
    )
    assert _RAW_ERROR not in probe.text + json.dumps(result) + caplog.text
