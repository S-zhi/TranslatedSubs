"""Cobalt configuration freezes safe options without changing legacy plans."""
from __future__ import annotations

import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.handler import deps, plugin_settings, tasks
from src.service.task import task_lifecycle
from src.store import TaskStore
from src.config.config_repository import get_config_provider
from src.config.plugin_schema import _sensitive_field, default_plugin_section
from src.plugin_system import PluginError
from src.plugins import get_plugin_manager
from src.plugins.builtin import cobalt_configuration as cobalt
from src.plugins.builtin.downloader_ytdlp import YtDlpFactory
from src.service.plugin_plan import parse_plugin_plan, resolve_plugin_plan
from src.service.plugin_selection import build_instance_plan, prepare_instance_selection
from src.store.plugin_instance_store import PluginInstanceStore


def _legacy(**overrides):
    return {
        "quality": "720p", "format_selector": "bv*[height<=720]+ba/b[height<=720]",
        "proxy": None, "socket_timeout": 30, "cookies_ref": None,
        "merge_output_format": "mp4", "retries": 3, "concurrent_fragments": 4,
        **overrides,
    }


def _config(**overrides):
    return {**_legacy(), "endpoint": "https://cobalt.example/api", **overrides}


def test_editable_schema_is_secret_free_and_endpoint_is_normalized():
    assert set(cobalt.COBALT_CONFIG_SCHEMA) == {
        "endpoint", "socket_timeout", "access_mode", "fallback_policy", "filename_style", "audio_format",
    }
    assert not any(_sensitive_field(key) for key in cobalt.COBALT_CONFIG_SCHEMA)
    assert cobalt.validate_cobalt_config({"endpoint": "http://127.0.0.1:9000"}) == {
        "endpoint": "http://127.0.0.1:9000/",
    }
    assert cobalt.validate_cobalt_config({"endpoint": "https://cobalt.example", "socket_timeout": 1}) == {
        "endpoint": "https://cobalt.example/", "socket_timeout": 1,
    }
    for partial in (False, True):
        for config in ({}, {"socket_timeout": 1}):
            with pytest.raises(ValueError, match="endpoint"):
                cobalt.validate_cobalt_config(config, partial=partial)
    assert cobalt.validate_cobalt_config({"endpoint": "https://cobalt.example/api///"}, partial=False) == {
        "endpoint": "https://cobalt.example/api/", "socket_timeout": 30,
        "access_mode": "anonymous", "fallback_policy": "none",
        "filename_style": "basic", "audio_format": "best",
    }


def test_download_config_preserves_legacy_fields_and_freezes_defaults_and_reference():
    original = _config(cookies_ref="settings:cookies_file", retries=0)
    before = copy.deepcopy(original)
    anonymous = cobalt.validate_cobalt_config(original, purpose="download", partial=True)
    assert original == before
    assert {key: anonymous[key] for key in cobalt.YTDLP_CONFIG_KEYS} == _legacy(
        cookies_ref="settings:cookies_file", retries=0,
    )
    assert anonymous["endpoint"] == "https://cobalt.example/api/"
    assert anonymous["access_mode"] == "anonymous"
    assert anonymous["credential_ref"] is None
    assert anonymous["fallback_policy"] == "none"
    assert anonymous["filename_style"] == "basic"
    assert anonymous["audio_format"] == "best"
    authenticated = cobalt.validate_cobalt_config(_config(access_mode="api_key"), purpose="download")
    assert authenticated["credential_ref"] == "settings:cobalt_api_key"
    assert cobalt.validate_cobalt_config(authenticated, purpose="download") == authenticated


@pytest.mark.parametrize("endpoint", [
    "", "ftp://cobalt.example", "https://", "https://user:private@cobalt.example",
    "https://cobalt.example?token=private", "https://cobalt.example?", "https://cobalt.example#",
    "https://cobalt.example#private", "https://cobalt.example:bad", "https://cobalt.example:99999",
    "https://cobalt.example:0", "https://cobalt.example/private\\payload", "https://cobalt.example/\x7fprivate",
    " https://cobalt.example", "https://cobalt.example/\nprivate", "https://[invalid",
])
def test_endpoint_rejects_credentials_and_invalid_addresses_without_echoing(endpoint):
    with pytest.raises(ValueError) as failure:
        cobalt.validate_cobalt_config({"endpoint": endpoint})
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("config", [
    {"endpoint": None}, {"socket_timeout": True}, {"socket_timeout": 0},
    {"socket_timeout": 301}, {"socket_timeout": 1.5}, {"access_mode": "bearer"},
    {"use_api_key": True}, {"api_key": "private"}, {"credential_ref": "settings:cobalt_api_key"},
    {"fallback_policy": "auto"}, {"filename_style": "../private"}, {"audio_format": "flac"},
    {"quality": "720p"}, {"unknown": "private"},
])
def test_instance_overrides_reject_unknown_sensitive_or_wrong_typed_fields(config):
    with pytest.raises(ValueError) as failure:
        cobalt.validate_cobalt_config({"endpoint": "https://cobalt.example", **config})
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("overrides", [
    {"quality": "999p"}, {"quality": "bestp"}, {"quality": "480p "}, {"quality": 480},
    {"proxy": "https://user:private@proxy.example"}, {"proxy": "https://proxy.example?auth=private"},
    {"proxy": "https://proxy.example:99999"}, {"format_selector": []},
    {"cookies_ref": "settings:cobalt_api_key"}, {"cookies_ref": []}, {"retries": True},
    {"retries": -1}, {"retries": 101}, {"concurrent_fragments": 0}, {"concurrent_fragments": 33},
    {"merge_output_format": " "}, {"socket_timeout": 301},
    {"credential_ref": "settings:transcriber_api_key"},
    {"credential_ref": "settings:cobalt_api_key"}, {"access_mode": "api_key", "credential_ref": None},
])
def test_frozen_config_validates_legacy_values_and_exact_credential_reference(overrides):
    with pytest.raises(ValueError) as failure:
        cobalt.validate_cobalt_config(_config(**overrides), purpose="download")
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("quality", ["best", "audio_only", "4320p", "2160", "1440P", "1080p", "720", "480p", "360p", "240p", "144"])
def test_only_supported_cobalt_qualities_are_accepted_and_preserved(quality):
    config = cobalt.validate_cobalt_config(_config(quality=quality), purpose="download")
    assert config["quality"] == quality


def test_download_purpose_requires_endpoint_all_legacy_fields_and_correct_purpose():
    for missing in ("endpoint", *cobalt.YTDLP_CONFIG_KEYS):
        config = _config()
        del config[missing]
        with pytest.raises(ValueError):
            cobalt.validate_cobalt_config(config, purpose="download")
    with pytest.raises(ValueError):
        cobalt.validate_cobalt_config(_config(), purpose="translate")


def test_preflight_is_local_and_only_checks_ytdlp_when_fallback_is_enabled(monkeypatch):
    looked_up = []
    fallbacks = []
    monkeypatch.setattr(cobalt.importlib.util, "find_spec", lambda name: looked_up.append(name) or object())
    monkeypatch.setattr(YtDlpFactory, "preflight", lambda self, config, **options: fallbacks.append((config, options)))
    cobalt.preflight_cobalt(_config(), operation="download", purpose="download")
    assert looked_up == ["httpx"]
    assert fallbacks == []
    cobalt.preflight_cobalt(
        _config(fallback_policy="ytdlp"), operation="download", purpose="download",
        source_lang="en", target_lang="zh-CN",
    )
    assert fallbacks == [(_legacy(), {
        "operation": "download", "purpose": "download", "source_lang": "en", "target_lang": "zh-CN",
    })]


def test_preflight_reports_missing_dependency_and_does_not_fallback_from_invalid_config(monkeypatch):
    monkeypatch.setattr(cobalt.importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(PluginError) as failure:
        cobalt.preflight_cobalt(_config(), operation="download", purpose="download")
    assert failure.value.code == "dependency_missing"
    monkeypatch.setattr(YtDlpFactory, "preflight", lambda *_args, **_kwargs: pytest.fail("invalid config reached fallback"))
    for changes in ({"endpoint": "https://user:private@cobalt.example"}, {"proxy": "socks5://proxy.example"}, {"proxy": "socks4://proxy.example"}):
        with pytest.raises(PluginError) as failure:
            cobalt.preflight_cobalt(_config(fallback_policy="ytdlp", **changes), operation="download", purpose="download")
        assert failure.value.code == "invalid_input"
        assert "private" not in str(failure.value)


def test_preflight_rejects_zero_retries_only_for_enabled_ytdlp_fallback(monkeypatch):
    monkeypatch.setattr(cobalt.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(YtDlpFactory, "preflight", lambda *_args, **_kwargs: pytest.fail("zero retries reached fallback"))
    cobalt.preflight_cobalt(_config(retries=0), operation="download", purpose="download")
    with pytest.raises(PluginError) as failure:
        cobalt.preflight_cobalt(_config(retries=0, fallback_policy="ytdlp"), operation="download", purpose="download")
    assert failure.value.code == "invalid_input"
    assert "download_retries" in failure.value.message


def test_catalog_adds_cobalt_without_creating_default_instance_or_changing_ytdlp_defaults():
    manager = get_plugin_manager()
    catalog = {entry["plugin_id"]: entry for entry in manager.catalog()}
    assert len(catalog) == 8
    assert catalog[cobalt.PLUGIN_ID]["stage"] == "download"
    assert catalog[cobalt.PLUGIN_ID]["capabilities"] == ["probe"]
    assert catalog[cobalt.PLUGIN_ID]["config_schema"] == cobalt.COBALT_CONFIG_SCHEMA
    defaults = default_plugin_section()
    assert len(defaults["defaults"]) == len(defaults["byId"]) == 7
    assert defaults["byId"]["default_download"]["plugin_id"] == "builtin.ytdlp"
    assert manager.validate_config("builtin.ytdlp", "1", _legacy(), purpose="download") == _legacy()


def test_instance_plan_freezes_cobalt_defaults_without_changing_raw_plan():
    from src.config import settings

    provider = get_config_provider()
    store = PluginInstanceStore(provider=provider)
    instance, saved = store.create({
        "name": "Cobalt", "stage": "download", "plugin_id": cobalt.PLUGIN_ID,
        "implementation_version": "1", "config": {"endpoint": "https://cobalt.example", "access_mode": "api_key"},
    }, expected_revision=provider.read().revision)
    selected = prepare_instance_selection(
        saved, {"download": instance["id"]}, request_model="local:tiny", engine="deepseek",
        tts_voice="auto", need_subtitle=False,
    )
    raw = resolve_plugin_plan(
        source_type="url", source_lang="auto", target_lang="zh-CN", mode="mono", burn="hard",
        model="local:tiny", engine="deepseek", need_subtitle=False, quality="480p", tts_enabled=False,
        tts_voice="auto", original_voice_mode="keep", settings=settings, plugin_defaults=selected.plugin_defaults,
    )
    legacy = parse_plugin_plan(raw)["slots"]["download"]["config"]
    assert set(legacy) == cobalt.YTDLP_CONFIG_KEYS
    frozen = parse_plugin_plan(build_instance_plan(raw, selected))
    config = frozen["slots"]["download"]["config"]
    assert {key: config[key] for key in cobalt.YTDLP_CONFIG_KEYS} == legacy
    assert config["endpoint"] == "https://cobalt.example/"
    assert config["credential_ref"] == "settings:cobalt_api_key"
    assert config["fallback_policy"] == "none"
    store.update(instance["id"], {"config": {"endpoint": "https://changed.example"}}, expected_revision=saved.revision)
    assert parse_plugin_plan(build_instance_plan(raw, selected)) == frozen


@pytest.fixture
def config_routes(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "cobalt-config.db")
    queued = []
    monkeypatch.setattr(tasks, "enqueue_pipeline", queued.append)
    app = FastAPI()
    app.include_router(plugin_settings.router)
    app.include_router(task_lifecycle.router)
    app.dependency_overrides[deps.get_store] = lambda: store
    app.dependency_overrides[deps.require_api_token] = lambda: None
    with TestClient(app) as client:
        yield client, get_config_provider(), store, queued


def _instance_payload(config):
    return {
        "id": "cobalt-config", "name": "Cobalt", "stage": "download", "plugin_id": cobalt.PLUGIN_ID,
        "implementation_version": "1", "config": config,
    }


@pytest.mark.parametrize("config", [{}, {"socket_timeout": 1}, {"fallback_policy": "ytdlp"}])
def test_routes_cannot_create_update_or_default_cobalt_without_endpoint(config_routes, config):
    client, provider, _store, queued = config_routes
    base = "/api/settings/plugins"
    before = provider.read()
    response = client.post(f"{base}/instances", headers={"If-Match": str(before.revision)}, json=_instance_payload(config))
    assert response.status_code == 422
    assert "endpoint" in response.text
    assert provider.read() == before

    response = client.post(f"{base}/instances", headers={"If-Match": str(before.revision)}, json=_instance_payload({
        "endpoint": "https://cobalt.example",
    }))
    assert response.status_code == 201
    saved = provider.read()
    response = client.put(f"{base}/instances/cobalt-config", headers={"If-Match": str(saved.revision)}, json={"config": config})
    assert response.status_code == 422
    assert "endpoint" in response.text
    assert provider.read() == saved

    # Simulate a previously saved incomplete instance to exercise default changes.
    provider.mutate(lambda doc: doc["plugins"]["byId"]["cobalt-config"].update({"config": config}))
    before = provider.read()
    response = client.put(f"{base}/defaults", headers={"If-Match": str(before.revision)}, json={
        "defaults": {"download": "cobalt-config"},
    })
    assert response.status_code == 422
    assert "endpoint" in response.text
    assert provider.read() == before
    assert queued == []


@pytest.mark.parametrize("key,value", [
    ("endpoint", "https://cobalt.example:0/private"),
    ("endpoint", "https://cobalt.example/private\\payload"),
    ("endpoint", "https://cobalt.example/\x7fprivate"),
    ("proxy", "http://proxy.example:0/private"),
    ("proxy", "http://proxy.example/private\\payload"),
    ("proxy", "http://proxy.example/\x7fprivate"),
    ("proxy", "http://proxy.example?route=private"),
])
def test_invalid_url_boundaries_fail_preflight_without_fallback_or_value_echo(monkeypatch, key, value):
    monkeypatch.setattr(cobalt.importlib.util, "find_spec", lambda _name: pytest.fail("invalid config reached dependency check"))
    monkeypatch.setattr(YtDlpFactory, "preflight", lambda *_args, **_kwargs: pytest.fail("invalid config reached fallback"))
    with pytest.raises(PluginError) as failure:
        cobalt.preflight_cobalt(_config(fallback_policy="ytdlp", **{key: value}), operation="download", purpose="download")
    assert failure.value.code == "invalid_input"
    assert "private" not in str(failure.value)
    assert value not in str(failure.value)


@pytest.mark.parametrize("key,value", [
    ("endpoint", None),
    ("endpoint", "https://cobalt.example:0/private"),
    ("endpoint", "https://cobalt.example/private\\payload"),
    ("endpoint", "https://cobalt.example/\x7fprivate"),
    ("proxy", "http://proxy.example:0/private"),
    ("proxy", "http://proxy.example/private\\payload"),
    ("proxy", "http://proxy.example/\x7fprivate"),
    ("proxy", "http://proxy.example?route=private"),
])
def test_invalid_endpoint_or_proxy_cannot_persist_or_enqueue_task(config_routes, key, value):
    client, provider, store, queued = config_routes
    instance, _ = PluginInstanceStore(provider=provider).create(
        _instance_payload({"endpoint": "https://cobalt.example"}), expected_revision=provider.read().revision,
    )
    if key == "endpoint":
        config = {} if value is None else {"endpoint": value}
        # Previously accepted instances still receive validation on task selection.
        provider.mutate(lambda doc: doc["plugins"]["byId"][instance["id"]].update({"config": config}))
    else:
        provider.mutate(lambda doc: doc["business"].update({"download_proxy": value}))
    response = client.post("/api/tasks", json={
        "url": "https://video.example/test", "needSubtitle": False,
        "pluginInstances": {"download": instance["id"]},
    })
    assert response.status_code == 422
    assert "private" not in response.text
    assert store.list() == []
    assert queued == []
