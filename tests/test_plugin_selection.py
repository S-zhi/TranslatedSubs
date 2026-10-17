import copy
import json

import pytest
from test_plugin_preflight import creation_plugins

from src.config.config_repository import MemoryConfigProvider
from src.config.plugin_schema import default_plugin_section
from src.config.redis_store import ConfigSnapshot
from src.plugins import get_plugin_manager
from src.service.plugin_plan import parse_plugin_plan, resolve_plugin_plan
from src.service.plugin_selection import (
    build_instance_plan, prepare_instance_selection, resolve_model_credential,
)
from src.service.plugin_snapshot import validate_instance_plan


def _snapshot():
    provider = MemoryConfigProvider()
    return provider.read()


def test_instance_parameters_are_frozen_and_v1_remains_readable():
    snapshot = _snapshot()
    snapshot.values["plugins"] = default_plugin_section()
    snapshot.values["plugins"]["byId"]["default_download"]["config"] = {"socket_timeout": 45}
    selected = prepare_instance_selection(snapshot, None, request_model="local:tiny", engine="deepseek",
                                          tts_voice="auto", need_subtitle=False)
    v1 = resolve_plugin_plan(source_type="url", source_lang="en", target_lang="zh-CN", mode="mono",
                             burn="hard", model="tiny", engine="deepseek", need_subtitle=False,
                             quality="720p", tts_enabled=False, tts_voice="auto", original_voice_mode="keep",
                             plugin_defaults=selected.plugin_defaults)
    assert parse_plugin_plan(v1)["schema_version"] == 1
    encoded = build_instance_plan(v1, selected)
    plan = parse_plugin_plan(encoded)
    assert plan["schema_version"] == 2
    assert plan["slots"]["download"]["instance_id"] == "default_download"
    assert plan["slots"]["download"]["config"]["socket_timeout"] == 45
    snapshot.values["plugins"]["byId"]["default_download"]["config"]["socket_timeout"] = 9
    assert parse_plugin_plan(encoded)["slots"]["download"]["config"]["socket_timeout"] == 45


def test_http_instance_overrides_legacy_local_model_without_editing_current_card():
    snapshot = _snapshot()
    snapshot.values["plugins"] = default_plugin_section()
    snapshot.values["plugins"]["byId"]["default_transcribe"]["config"] = {
        "backend": "http", "service_url": "https://asr.example.test/v1",
    }
    before = copy.deepcopy(snapshot.values)
    selected = prepare_instance_selection(snapshot, {}, request_model="local:tiny", engine="deepseek",
                                          tts_voice="auto")
    assert selected.request_model == "tiny"
    assert selected.config_snapshot.values["business"]["transcriber_backend"] == "http"
    assert snapshot.values == before


def test_fixed_model_binding_never_switches_to_current_card_and_key_rotation_is_used():
    snapshot = _snapshot()
    snapshot.values["plugins"] = default_plugin_section()
    snapshot.values["models"]["llm"] = {
        "enabled": True, "models": [
            {"id": "fixed", "enabled": True, "model": "fixed-model", "provider": "openai", "apiKey": "old", "isCurrent": False},
            {"id": "current", "enabled": True, "model": "other-model", "provider": "openai", "apiKey": "different", "isCurrent": True},
        ],
    }
    snapshot.values["plugins"]["byId"]["default_translate"]["model_ref"] = {"kind": "llm", "card_id": "fixed"}
    selected = prepare_instance_selection(snapshot, None, request_model="tiny", engine="another", tts_voice="auto")
    assert selected.engine == "deepseek"
    assert selected.config_snapshot.values["models"]["llm"]["models"][0]["id"] == "fixed"
    assert snapshot.values["models"]["llm"]["models"][1]["isCurrent"]
    ref = "model_card:llm:fixed:api_key"
    snapshot.values["models"]["llm"]["models"][0]["apiKey"] = "rotated"
    assert resolve_model_credential(ref, snapshot) == "rotated"
    snapshot.values["models"]["llm"]["models"] = snapshot.values["models"]["llm"]["models"][1:]
    with pytest.raises(ValueError, match="删除"):
        resolve_model_credential(ref, snapshot)


def test_unused_stage_missing_implementation_does_not_block_download_only():
    snapshot = _snapshot()
    snapshot.values["plugins"] = default_plugin_section()
    snapshot.values["plugins"]["byId"]["default_tts"]["plugin_id"] = "missing.tts"
    prepare_instance_selection(snapshot, None, request_model="tiny", engine="deepseek", tts_voice="auto",
                               need_subtitle=False)


@pytest.mark.parametrize("backend,canonical", [
    ("local", "local_whisper"), ("whisper", "local_whisper"),
    ("faster_whisper", "local_whisper"), ("custom", "http"), ("custom_http", "http"),
])
def test_backend_aliases_are_canonical_in_runtime_snapshot(backend, canonical):
    snapshot = _snapshot()
    snapshot.values["plugins"]["byId"]["default_transcribe"]["config"] = {"backend": backend}
    if canonical == "http":
        snapshot.values["plugins"]["byId"]["default_transcribe"]["config"]["service_url"] = "https://asr.test/v1"
    selected = prepare_instance_selection(snapshot, None, request_model="local:tiny", engine="deepseek", tts_voice="auto")
    assert selected.config_snapshot.values["business"]["transcriber_backend"] == canonical
    from src.config.schema import validate_business_value
    validate_business_value("transcriber_backend", canonical)


@pytest.mark.parametrize("instance_id", ["disabled-tts", "deleted-tts"])
def test_unused_explicit_selection_does_not_block_download(instance_id):
    snapshot = _snapshot()
    snapshot.values["plugins"]["byId"]["disabled-tts"] = {
        **snapshot.values["plugins"]["byId"]["default_tts"], "id": "disabled-tts", "enabled": False,
    }
    selected = prepare_instance_selection(snapshot, {"tts": instance_id}, request_model="tiny",
                                          engine="deepseek", tts_voice="auto", need_subtitle=False)
    assert selected.instances["tts"]["id"] == "default_tts"


def test_explicit_deepseek_engine_binding_bypasses_current_llm(creation_plugins):
    from src.config.config_repository import get_config_provider
    from src.service.model_runtime import resolve_runtime
    from src.service.task.task_lifecycle import _resolve_creation_plan
    from src.store import TranslationEngineStore
    provider = get_config_provider()
    engines = TranslationEngineStore()
    engines.ensure_default_deepseek(api_key="engine-secret")
    engines.update("deepseek", api_key="engine-secret", availability="AVAILABLE", model="engine-model", base_url="https://engine.test/v1")
    provider.mutate(lambda values: values["models"].update({"llm": {
        "enabled": True, "_runtimeManaged": True, "models": [{
            "id": "other", "name": "Other", "model": "wrong-model", "provider": "openai",
            "enabled": True, "isCurrent": True, "apiKey": "wrong-secret", "baseUrl": "https://wrong.test/v1",
        }],
    }}))
    provider.mutate(lambda values: values["plugins"]["byId"]["default_translate"].update({"engine_ref": {"id": "deepseek"}}))
    selected = prepare_instance_selection(provider.read(), None, request_model="tiny", engine="deepseek", tts_voice="auto")
    pinned_engines = TranslationEngineStore(snapshot=selected.config_snapshot)
    runtime = resolve_runtime(engine=selected.engine, need_subtitle=True, request_model=selected.request_model,
                              source_lang="en", target_lang="zh-CN", tts_enabled=False, tts_voice="auto",
                              engines=pinned_engines, config_snapshot=selected.config_snapshot,
                              plugin_defaults=selected.plugin_defaults, ensure_local_model_ready=lambda _: None)
    assert runtime.snapshot["engine"]["source"] == "translation_store"
    assert runtime.snapshot["engine"]["model"] == "engine-model"
    from src.config import settings
    encoded = _resolve_creation_plan(source_type="url", source_lang="en", target_lang="zh-CN", mode="mono",
                                     burn="soft", model="tiny", engine="deepseek", need_subtitle=True,
                                     quality="480p", tts_enabled=False, tts_voice="auto", original_voice_mode="keep",
                                     settings=settings, engines=pinned_engines, ensure_supported_translation_languages=lambda *_: None,
                                     ensure_local_model_ready=lambda *_: None, ensure_translation_engine=lambda *_: None,
                                     runtime_snapshot=runtime.snapshot, plugin_defaults=selected.plugin_defaults,
                                     config_snapshot=selected.config_snapshot, instance_selection=selected)
    slot = parse_plugin_plan(encoded)["slots"]["translate"]
    assert slot["engine_ref"] == {"id": "deepseek"}
    assert slot["model_ref"] is None
    assert slot["config"]["model"] == "engine-model"
    assert slot["config"]["credential_ref"] == "translation_engine:deepseek:api_key"


@pytest.mark.parametrize("service_url", ["https://asr.test/v1", ""])
def test_local_whisper_http_fields_fail_before_media_execution(service_url):
    with pytest.raises(Exception, match="本地 Whisper"):
        get_plugin_manager().preflight("builtin.whisper", "1", operation="transcribe", purpose="transcribe",
                                       config={"backend": "local_whisper", "model": "tiny", "service_url": service_url})


@pytest.mark.parametrize("field,value", [("api_key", "secret"), ("base_url", "https://x.test/?token=secret"),
                                          ("credential_ref", "settings:unrelated_key"), ("batch_size", float("nan"))])
def test_v2_plan_rejects_secrets_bad_references_and_nonfinite_numbers(field, value):
    plan = {"schema_version": 2, "config_revision": 3, "slots": {
        "translate": {"plugin_id": "test.translator", "implementation_version": "1",
                      "instance_id": "instance", "config_schema_version": 1, "config": {field: value}},
    }}
    with pytest.raises(ValueError):
        validate_instance_plan(plan)
