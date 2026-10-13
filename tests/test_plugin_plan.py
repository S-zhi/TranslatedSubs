"""Tests for immutable pipeline plugin-plan resolution."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.service.plugin_plan import (
    parse_plugin_plan,
    resolve_plugin_defaults,
    resolve_plugin_plan,
    serialize_plugin_plan,
    validate_plugin_plan,
)


class EngineStore:
    def __init__(self, record):
        self.record = record

    def get(self, engine_id):
        return self.record if self.record and self.record.id == engine_id else None


def _settings(**overrides):
    values = {
        "download_format": "custom-fallback",
        "download_proxy": None,
        "download_socket_timeout": 30,
        "download_retries": 5,
        "download_concurrent_fragments": 4,
        "merge_output_format": "mkv",
        "cookies_file": None,
        "audio_sample_rate": 16000,
        "audio_channels": 2,
        "vocal_separation_enabled": False,
        "vocal_separation_backend": "demucs",
        "vocal_separation_model": "htdemucs",
        "vocal_separation_threads": 2,
        "vocal_separation_command": "python -m demucs.separate",
        "vocal_separation_timeout": 90,
        "transcriber_backend": "local_whisper",
        "transcriber_url": None,
        "transcriber_api_key": None,
        "transcriber_timeout": 123,
        "local_whisper_beam_size": 7,
        "translate_batch_size": 20,
        "local_translate_batch_size": 8,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _resolve(**kwargs):
    defaults = dict(
        source_type="url",
        source_lang="en",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="local:tiny",
        engine="cloud",
        need_subtitle=True,
        quality="720p",
        tts_enabled=False,
        tts_voice="auto",
        original_voice_mode="keep",
        settings=_settings(),
        engines=EngineStore(
            SimpleNamespace(
                id="cloud",
                api_type="openai_compatible",
                base_url="https://example.test/v1",
                model="model-a",
                api_key="secret-api-key",
            )
        ),
    )
    defaults.update(kwargs)
    return parse_plugin_plan(resolve_plugin_plan(**defaults))


def test_resolver_snapshots_effective_defaults_and_never_persists_secrets(monkeypatch):
    monkeypatch.setenv(
        "SUBTRANS_PIPELINE_PLUGINS",
        json.dumps({"translate": {"plugin_id": "env.translator", "implementation_version": "2"}}),
    )
    settings = _settings(
        vocal_separation_enabled=True,
        transcriber_backend="http",
        transcriber_url="https://asr.example.test/v1",
        transcriber_api_key="secret-asr-key",
    )
    plan = _resolve(settings=settings, model="tiny")

    assert plan["slots"]["download"]["config"] == {
        "quality": "720p",
        "format_selector": "bv*[height<=720]+ba/b[height<=720]",
        "proxy": None,
        "socket_timeout": 30,
        "cookies_ref": None,
        "merge_output_format": "mkv",
        "retries": 5,
        "concurrent_fragments": 4,
    }
    assert plan["slots"]["extract.asr"]["config"] == {"sample_rate": 48000, "channels": 2}
    assert plan["slots"]["separate.asr"]["config"]["timeout_seconds"] == 90
    assert plan["slots"]["transcribe"]["config"] == {
        "backend": "http",
        "model": "tiny",
        "service_url": "https://asr.example.test/v1",
        "credential_ref": "settings:transcriber_api_key",
        "timeout_seconds": 123,
        "device": "cpu",
        "compute_type": "int8",
        "download_root": None,
        "beam_size": 7,
    }
    assert plan["slots"]["translate"]["plugin_id"] == "env.translator"
    serialized = json.dumps(plan)
    assert "secret-api-key" not in serialized
    assert "secret-asr-key" not in serialized


def test_explicit_empty_plugin_defaults_suppresses_environment(monkeypatch):
    monkeypatch.setenv(
        "SUBTRANS_PIPELINE_PLUGINS",
        json.dumps({"download": {"plugin_id": "env.downloader", "implementation_version": "9"}}),
    )
    plan = _resolve(need_subtitle=False, plugin_defaults={})
    assert plan["slots"]["download"]["plugin_id"] == "builtin.ytdlp"
    assert plan["slots"]["download"]["implementation_version"] == "1"


def test_environment_plugin_defaults_are_snapshotted_once_for_slots(monkeypatch):
    monkeypatch.setenv(
        "SUBTRANS_PIPELINE_PLUGINS",
        json.dumps({"audio_extract": {"plugin_id": "custom.extractor", "implementation_version": "3"}}),
    )
    plan = _resolve(
        tts_enabled=True,
        original_voice_mode="lower",
        plugin_defaults=None,
    )
    assert plan["slots"]["extract.asr"]["plugin_id"] == "custom.extractor"
    assert plan["slots"]["extract.dubbing"]["plugin_id"] == "custom.extractor"
    assert plan["slots"]["extract.dubbing"]["implementation_version"] == "3"


def test_public_plugin_defaults_helper_returns_an_immutable_mapping(monkeypatch):
    monkeypatch.setenv(
        "SUBTRANS_PIPELINE_PLUGINS",
        json.dumps({"download": {"plugin_id": "custom.downloader", "implementation_version": "4"}}),
    )
    defaults = resolve_plugin_defaults()
    assert defaults["download"]["plugin_id"] == "custom.downloader"
    with pytest.raises(TypeError):
        defaults["download"]["plugin_id"] = "mutated"


def test_upload_plan_omits_download_slot():
    plan = _resolve(source_type="upload")
    assert "download" not in plan["slots"]
    assert "extract.asr" in plan["slots"]


def test_local_model_prefix_preserves_legacy_backend_override():
    settings = _settings(transcriber_backend="http", transcriber_url="https://asr.example.test")
    plan = _resolve(settings=settings, model="local:tiny")
    config = plan["slots"]["transcribe"]["config"]
    assert config["backend"] == "local_whisper"
    assert config["model"] == "tiny"
    assert config["service_url"] is None


@pytest.mark.parametrize(
    "mapping",
    [
        {"not-a-stage": {"plugin_id": "x", "implementation_version": "1"}},
        {"download": {"plugin_id": "x"}},
        {"download": {"plugin_id": "", "implementation_version": "1"}},
        {"download": {"plugin_id": "x", "implementation_version": 1}},
    ],
)
def test_plugin_default_mapping_rejects_unknown_or_invalid_entries(mapping):
    with pytest.raises(ValueError):
        _resolve(need_subtitle=False, plugin_defaults=mapping)


def test_plan_validation_rejects_secret_urls_and_unknown_config():
    with pytest.raises(ValueError):
        validate_plugin_plan(
            {
                "schema_version": 1,
                "slots": {
                    "download": {
                        "plugin_id": "builtin.ytdlp",
                        "implementation_version": "1",
                        "config": {"proxy": "https://user:secret@example.test"},
                    }
                },
            }
        )
    with pytest.raises(ValueError):
        validate_plugin_plan(
            {
                "schema_version": 1,
                "slots": {
                    "translate": {
                        "plugin_id": "builtin.translation_engine",
                        "implementation_version": "1",
                        "config": {
                            "engine_id": "cloud",
                            "credential_ref": "settings:transcriber_api_key",
                        },
                    }
                },
            }
        )
    with pytest.raises(ValueError):
        validate_plugin_plan(
            {
                "schema_version": 1,
                "slots": {
                    "translate": {
                        "plugin_id": "builtin.translation_engine",
                        "implementation_version": "1",
                        "config": {"api_key": "secret"},
                    }
                },
            }
        )


def test_plan_serialization_is_stable_and_rejects_unsupported_version():
    data = {"schema_version": 1, "slots": {}}
    assert serialize_plugin_plan(data) == '{"schema_version":1,"slots":{}}'
    with pytest.raises(ValueError):
        validate_plugin_plan({"schema_version": 2, "slots": {}})
    with pytest.raises(ValueError):
        validate_plugin_plan({"schema_version": True, "slots": {}})


def test_custom_translation_plugin_owns_local_ct2_language_validation():
    engine = SimpleNamespace(
        id="cloud", api_type="local_ct2", base_url="", model="local-model", api_key=None
    )
    plan = _resolve(
        engines=EngineStore(engine),
        source_lang="ja",
        target_lang="en",
        plugin_defaults={
            "translate": {"plugin_id": "custom.translator", "implementation_version": "2"}
        },
    )
    assert plan["slots"]["translate"]["plugin_id"] == "custom.translator"
    assert plan["slots"]["translate"]["config"]["api_type"] == "local_ct2"
