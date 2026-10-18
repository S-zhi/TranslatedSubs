"""Cobalt secrets stay live and must be authorized by the frozen download slot."""
from __future__ import annotations

import copy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import settings
from src.config.config_repository import get_config_provider
from src.handler import business_settings, tasks
from src.plugin_system import PluginError, ProbeResult
from src.plugins import get_plugin_manager
from src.plugins.builtin.cobalt_configuration import validate_cobalt_config
from src.service import runner
from src.service.orchestrator import PipelineEvent
from src.service.plugin_plan import parse_plugin_plan
from src.service.plugin_snapshot import validate_instance_plan
from src.store import TaskStore
from src.store.plugin_instance_store import PluginInstanceStore

REF = "settings:cobalt_api_key"
SECRET = "cobalt-private-test-value"


def _config(**overrides):
    return validate_cobalt_config({
        "quality": "480p", "format_selector": "bv*[height<=480]+ba/b[height<=480]",
        "proxy": None, "socket_timeout": 30, "cookies_ref": None,
        "merge_output_format": "mp4", "retries": 3, "concurrent_fragments": 4,
        "endpoint": "https://cobalt.example", "access_mode": "api_key", **overrides,
    }, purpose="download")


def _plan(*, plugin_id="builtin.cobalt", purpose="download", config=None):
    return {
        "schema_version": 2, "config_revision": 1,
        "slots": {purpose: {
            "plugin_id": plugin_id, "implementation_version": "1", "instance_id": "test-instance",
            "config_schema_version": 1, "config": config if config is not None else _config(),
        }},
    }


def _instance(*, access_mode="api_key"):
    provider = get_config_provider()
    instance, _ = PluginInstanceStore(provider=provider).create({
        "name": "Cobalt", "stage": "download", "plugin_id": "builtin.cobalt",
        "implementation_version": "1", "config": {
            "endpoint": "https://cobalt.example", "access_mode": access_mode,
        },
    }, expected_revision=provider.read().revision)
    return instance


def _run_with_resolver(tmp_path, monkeypatch, plan, action):
    store = TaskStore(tmp_path / "credentials.db")
    record = store.create(
        url="https://video.example/test", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="soft", model="local:tiny", engine="deepseek",
        need_subtitle=False, plugin_plan_json=json.dumps(plan),
    )
    called = []

    def pipeline(_params, on_event, *, credential_resolver, **_kwargs):
        called.append(True)
        action(credential_resolver)
        on_event(PipelineEvent("SUCCESS", 100, None, outputs={}))

    monkeypatch.setattr(runner, "_store", store)
    monkeypatch.setattr(runner, "run_pipeline", pipeline)
    runner._run(record.id)
    assert called, store.get(record.id).error
    return store.get(record.id)


def test_runtime_settings_api_masks_rotates_and_resets_cobalt_key(memory_config_provider):
    app = FastAPI()
    app.include_router(business_settings.router)
    with TestClient(app) as client:
        initial = client.get("/api/settings/runtime")
        assert initial.json()["hasSecrets"]["cobalt_api_key"] is False
        for secret in (SECRET, "rotated-cobalt-private-test-value"):
            response = client.patch("/api/settings/runtime", headers={
                "If-Match": str(memory_config_provider.read().revision),
            }, json={"cobalt_api_key": secret})
            assert response.status_code == 200
            assert "cobalt_api_key" not in response.json()["values"]
            assert response.json()["hasSecrets"]["cobalt_api_key"] is True
            assert secret not in response.text
            public = client.get("/api/settings/runtime")
            assert secret not in public.text
            assert "cobalt_api_key" not in public.json()["values"]
            assert settings.cobalt_api_key == secret
            assert "cobalt_api_key" not in settings.capture_business_snapshot()
            assert secret not in json.dumps(settings.capture_business_snapshot())
        invalid = client.patch("/api/settings/runtime", json={"cobalt_api_key": 123})
        assert invalid.status_code == 422
        reset = client.patch("/api/settings/runtime", json={"cobalt_api_key": None})
        assert reset.status_code == 200
        assert reset.json()["hasSecrets"]["cobalt_api_key"] is False
        assert settings.cobalt_api_key is None


def test_snapshot_allows_only_cobalt_download_reference_and_rejects_inline_secret():
    plan = _plan()
    assert validate_instance_plan(plan) == plan
    assert REF in json.dumps(plan)
    assert SECRET not in json.dumps(plan)
    for candidate in (
        _plan(plugin_id="builtin.ytdlp"), _plan(plugin_id="custom.downloader"),
        _plan(purpose="transcribe"), _plan(purpose="translate"),
        _plan(config={**_config(), "credential_ref": "settings:api_token"}),
        _plan(config={**_config(), "api_key": SECRET}),
        _plan(config={**_config(), "endpoint": f"https://user:{SECRET}@cobalt.example"}),
        _plan(config={**_config(), "endpoint": f"https://cobalt.example?token={SECRET}"}),
    ):
        with pytest.raises(ValueError) as failure:
            validate_instance_plan(candidate)
        assert SECRET not in str(failure.value)


@pytest.mark.parametrize("purpose,plugin_id,config", [
    ("transcribe", "builtin.whisper", {"credential_ref": "settings:transcriber_api_key"}),
    ("translate", "builtin.translation_engine", {"engine_id": "deepseek", "credential_ref": "translation_engine:deepseek:api_key"}),
    ("transcribe", "builtin.whisper", {"credential_ref": "model_card:whisper:whisper-card:api_key"}),
    ("translate", "builtin.translation_engine", {"credential_ref": "model_card:llm:llm-card:api_key"}),
    ("tts", "builtin.kokoro", {"credential_ref": "model_card:tts:tts-card:api_key"}),
    ("download", "builtin.ytdlp", {"cookies_ref": "settings:cookies_file"}),
])
def test_existing_credential_reference_scopes_are_unchanged(purpose, plugin_id, config):
    plan = _plan(purpose=purpose, plugin_id=plugin_id, config=config)
    assert validate_instance_plan(plan) == plan


@pytest.mark.parametrize("access_mode,requested_ref,authorized", [
    ("api_key", REF, True), ("anonymous", REF, False),
    ("api_key", "settings:transcriber_api_key", False), ("api_key", "settings:cookies_file", False),
    ("api_key", "settings:api_token", False), ("api_key", "settings:cobalt_api_key:other", False),
])
def test_probe_resolver_requires_exact_frozen_reference(monkeypatch, access_mode, requested_ref, authorized):
    provider = get_config_provider()
    provider.mutate(lambda doc: doc["business"].update({"cobalt_api_key": SECRET, "transcriber_api_key": "other-secret"}))
    instance = _instance(access_mode=access_mode)
    captured = []

    def probe(_manager, plugin_id, _version, _request, config, context):
        captured.append((plugin_id, copy.deepcopy(config)))
        if authorized:
            assert context.resolve_credential(requested_ref) == SECRET
        else:
            with pytest.raises(PluginError) as failure:
                context.resolve_credential(requested_ref)
            assert failure.value.code == "credential_missing"
            assert SECRET not in str(failure.value)
        return ProbeResult(ok=True)

    monkeypatch.setattr(type(get_plugin_manager()), "run_probe", probe)
    assert tasks.probe_video("https://video.example/test", plugin_instances={"download": instance["id"]}).ok
    assert captured[0][0] == "builtin.cobalt"
    assert SECRET not in json.dumps(captured)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_probe_missing_cobalt_key_raises_safe_error(monkeypatch, value):
    get_config_provider().mutate(lambda doc: doc["business"].update({"cobalt_api_key": value}))
    instance = _instance()

    def probe(_manager, _plugin_id, _version, _request, _config, context):
        context.resolve_credential(REF)
        pytest.fail("missing key unexpectedly resolved")

    monkeypatch.setattr(type(get_plugin_manager()), "run_probe", probe)
    with pytest.raises(PluginError) as failure:
        tasks.probe_video("https://video.example/test", plugin_instances={"download": instance["id"]})
    assert failure.value.code == "credential_missing"
    assert failure.value.message == "credential is unavailable"


def test_probe_resolver_uses_current_key_and_keeps_cookie_reference_compatible(monkeypatch, tmp_path):
    provider = get_config_provider()
    cookie_path = tmp_path / "cookies.txt"
    provider.mutate(lambda doc: doc["business"].update({"cobalt_api_key": SECRET, "cookies_file": str(cookie_path)}))
    instance = _instance()

    def probe(_manager, _plugin_id, _version, _request, config, context):
        provider.mutate(lambda doc: doc["business"].update({"cobalt_api_key": "rotated-value"}))
        assert context.resolve_credential(REF) == "rotated-value"
        assert context.resolve_credential("settings:cookies_file") == str(cookie_path)
        assert config["cookies_ref"] == "settings:cookies_file"
        assert SECRET not in json.dumps(config)
        return ProbeResult(ok=True)

    monkeypatch.setattr(type(get_plugin_manager()), "run_probe", probe)
    assert tasks.probe_video("https://video.example/test", plugin_instances={"download": instance["id"]}).ok


def test_probe_resolver_rechecks_plugin_identity_even_for_forged_slot(monkeypatch):
    get_config_provider().mutate(lambda doc: doc["business"].update({"cobalt_api_key": SECRET}))
    forged = _plan(plugin_id="custom.downloader")
    monkeypatch.setattr(tasks, "parse_plugin_plan", lambda _value: forged)

    def probe(_manager, _plugin_id, _version, _request, _config, context):
        with pytest.raises(PluginError) as failure:
            context.resolve_credential(REF)
        assert failure.value.code == "credential_missing"
        return ProbeResult(ok=True)

    monkeypatch.setattr(type(get_plugin_manager()), "run_probe", probe)
    assert tasks.probe_video("https://video.example/test").ok


def test_runner_reads_rotated_key_and_persists_only_reference(tmp_path, monkeypatch, caplog):
    provider = get_config_provider()
    provider.mutate(lambda doc: doc["business"].update({"cobalt_api_key": SECRET}))
    frozen = _plan()
    seen = []

    def resolve(resolver):
        provider.mutate(lambda doc: doc["business"].update({"cobalt_api_key": "rotated-value"}))
        seen.append(resolver(REF))

    record = _run_with_resolver(tmp_path, monkeypatch, frozen, resolve)
    assert record.status == "SUCCESS"
    assert seen == ["rotated-value"]
    assert parse_plugin_plan(record.plugin_plan_json) == frozen
    for value in (record.plugin_plan_json, record.runtime_config_json, json.dumps(record.to_dict()), caplog.text):
        assert SECRET not in value
        assert "rotated-value" not in value
    assert "cobalt_api_key" not in json.loads(record.runtime_config_json)["business"]


@pytest.mark.parametrize("value", [None, "", "   "])
def test_runner_missing_cobalt_key_fails_with_safe_error(tmp_path, monkeypatch, value):
    get_config_provider().mutate(lambda doc: doc["business"].update({"cobalt_api_key": value}))
    record = _run_with_resolver(tmp_path, monkeypatch, _plan(), lambda resolver: resolver(REF))
    assert record.status == "FAILED"
    assert record.error_code == "credential_missing"
    assert record.error == "credential is unavailable"


@pytest.mark.parametrize("access_mode,requested_ref", [
    ("anonymous", REF), ("api_key", "settings:api_token"),
    ("api_key", "settings:transcriber_api_key"), ("api_key", "settings:cookies_file"),
    ("api_key", "settings:cobalt_api_key:other"),
])
def test_runner_resolver_rejects_unfrozen_references(tmp_path, monkeypatch, access_mode, requested_ref):
    get_config_provider().mutate(lambda doc: doc["business"].update({"cobalt_api_key": SECRET}))
    record = _run_with_resolver(
        tmp_path, monkeypatch, _plan(config=_config(access_mode=access_mode)),
        lambda resolver: resolver(requested_ref),
    )
    assert record.status == "FAILED"
    assert record.error_code == "credential_missing"
    assert SECRET not in record.error


def test_runner_resolver_rechecks_plugin_identity_even_for_forged_slot(tmp_path, monkeypatch):
    get_config_provider().mutate(lambda doc: doc["business"].update({"cobalt_api_key": SECRET}))
    forged = _plan(plugin_id="custom.downloader")
    monkeypatch.setattr(runner, "parse_plugin_plan", lambda _value: forged)
    monkeypatch.setattr(runner, "ensure_plan", lambda record, *_args: record)
    record = _run_with_resolver(tmp_path, monkeypatch, forged, lambda resolver: resolver(REF))
    assert record.status == "FAILED"
    assert record.error_code == "credential_missing"
    assert record.error == "credential is unavailable"


def test_runner_keeps_legacy_cookie_reference_resolution(tmp_path, monkeypatch):
    cookie_path = tmp_path / "cookies.txt"
    get_config_provider().mutate(lambda doc: doc["business"].update({"cookies_file": str(cookie_path)}))
    plan = {"schema_version": 1, "slots": {"download": {
        "plugin_id": "builtin.ytdlp", "implementation_version": "1",
        "config": {"quality": "480p", "cookies_ref": "settings:cookies_file"},
    }}}
    seen = []
    record = _run_with_resolver(tmp_path, monkeypatch, plan, lambda resolver: seen.append(resolver("settings:cookies_file")))
    assert record.status == "SUCCESS"
    assert seen == [str(cookie_path)]
