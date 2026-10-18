"""Persisted plugin-instance structure and non-sensitive binding validation."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlsplit

PLUGIN_SCHEMA_VERSION = 1
DEFAULT_PLUGIN_IDS = {
    "download": "builtin.ytdlp",
    "audio_extract": "builtin.ffmpeg_audio",
    "vocal_separate": "builtin.vocal_demucs",
    "transcribe": "builtin.whisper",
    "translate": "builtin.translation_engine",
    "tts": "builtin.kokoro",
    "render": "builtin.ffmpeg_render",
}
PLUGIN_STAGES = frozenset(DEFAULT_PLUGIN_IDS)
_MODEL_KINDS = {"transcribe": "whisper", "translate": "llm", "tts": "tts"}
_INSTANCE_FIELDS = {
    "id", "name", "stage", "plugin_id", "implementation_version",
    "config_schema_version", "enabled", "config", "model_ref", "engine_ref",
}
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SECRET_FIELD = re.compile(
    r"api[_-]?key|password|passwd|secret|credential|cookie|authorization|"
    r"(?:^|[_-])(?:token|auth|bearer)(?:$|[_-])",
    re.IGNORECASE,
)


def default_plugin_section() -> dict[str, Any]:
    """Return compatibility instances without freezing legacy runtime settings."""
    instances = {}
    defaults = {}
    for stage, plugin_id in DEFAULT_PLUGIN_IDS.items():
        instance_id = f"default_{stage}"
        instances[instance_id] = {
            "id": instance_id,
            "name": plugin_id,
            "stage": stage,
            "plugin_id": plugin_id,
            "implementation_version": "1",
            "config_schema_version": PLUGIN_SCHEMA_VERSION,
            "enabled": True,
            "config": {},
            "model_ref": None,
            "engine_ref": None,
        }
        defaults[stage] = instance_id
    return {
        "schema_version": PLUGIN_SCHEMA_VERSION,
        "byId": instances,
        "defaults": defaults,
    }


def _fail(path: str, reason: str) -> None:
    # Never echo a rejected value, which may contain credentials.
    raise ValueError(f"invalid configuration field {path}: {reason}")


def _identifier(value: Any, path: str) -> None:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        _fail(path, "must be a non-empty identifier of at most 128 characters")


def _text(value: Any, path: str, *, maximum: int = 256) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail(path, "must be a non-empty string")


def _sensitive_field(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return bool(_SECRET_FIELD.search(key)) or normalized in {
        "key", "token", "accesstoken", "refreshtoken", "authtoken", "bearertoken",
    }


def _safe_url(value: str, path: str) -> None:
    try:
        parsed = urlsplit(value)
        parsed.port  # Reject malformed or out-of-range ports as well.
        sensitive = (
            parsed.username is not None or parsed.password is not None
            or bool(parsed.fragment)
            or any(
                re.search(r"token|key|password|passwd|auth|cookie|secret|credential|signature", key, re.IGNORECASE)
                for key, _ in parse_qsl(parsed.query)
            )
        )
        valid = (
            parsed.scheme.lower() in {"http", "https", "socks5", "socks5h"}
            and bool(parsed.hostname)
            and not any(character.isspace() for character in value)
        )
    except ValueError:
        _fail(path, "must be a valid URL without credentials")
    if sensitive or not valid:
        _fail(path, "must be a valid URL without credentials or sensitive components")


def _validate_config(config: Any, path: str) -> None:
    if not isinstance(config, dict):
        _fail(path, "must be an object")
    for key, value in config.items():
        if not isinstance(key, str) or not key or len(key) > 128 or _sensitive_field(key):
            _fail(path, "contains an unsupported or sensitive field")
        field_path = f"{path}.{key}"
        if value is not None and type(value) not in {str, int, float, bool}:
            _fail(field_path, "must be a JSON scalar")
        if isinstance(value, float) and not math.isfinite(value):
            _fail(field_path, "must be finite")
        if isinstance(value, str):
            if len(value) > 4096:
                _fail(field_path, "is too long")
            url_field = key.lower().endswith(("url", "uri", "proxy", "endpoint"))
            if value and (url_field or "://" in value):
                _safe_url(value, field_path)


def _validate_bindings(
    record: dict[str, Any], path: str, document: Mapping[str, Any] | None,
) -> None:
    model_ref = record["model_ref"]
    engine_ref = record["engine_ref"]
    if model_ref is not None and engine_ref is not None:
        _fail(path, "model_ref and engine_ref are mutually exclusive")
    if model_ref is not None:
        ref_path = f"{path}.model_ref"
        if not isinstance(model_ref, dict) or set(model_ref) != {"kind", "card_id"}:
            _fail(ref_path, "must contain only kind and card_id")
        kind = model_ref["kind"]
        if not isinstance(kind, str) or kind != _MODEL_KINDS.get(record["stage"]):
            _fail(f"{ref_path}.kind", "does not match the plugin stage")
        _text(model_ref["card_id"], f"{ref_path}.card_id", maximum=128)
        if document is not None:
            cards = document.get("models", {}).get(kind, {}).get("models", [])
            if not any(isinstance(card, dict) and card.get("id") == model_ref["card_id"] for card in cards):
                _fail(f"{ref_path}.card_id", "model card does not exist")
    if engine_ref is not None:
        ref_path = f"{path}.engine_ref"
        if not isinstance(engine_ref, dict) or set(engine_ref) != {"id"}:
            _fail(ref_path, "must contain only id")
        if record["stage"] != "translate":
            _fail(ref_path, "only translation plugins may bind an engine")
        _identifier(engine_ref["id"], f"{ref_path}.id")
        if document is not None and engine_ref["id"] not in document.get("engines", {}).get("byId", {}):
            _fail(f"{ref_path}.id", "translation engine does not exist")


def validate_plugin_section(
    section: Any, *, document: Mapping[str, Any] | None = None,
) -> None:
    """Validate structure, complete defaults, and references in one aggregate.

    Factory-owned editable fields are checked by PluginInstanceStore, which has
    access to the lightweight plugin catalog. This layer has no runtime/plugin
    imports so configuration initialization remains independent of plugins.
    """
    if not isinstance(section, dict) or set(section) != {"schema_version", "byId", "defaults"}:
        _fail("plugins", "must contain only schema_version, byId and defaults")
    if type(section["schema_version"]) is not int or section["schema_version"] != PLUGIN_SCHEMA_VERSION:
        _fail("plugins.schema_version", "unsupported schema version")
    instances = section["byId"]
    defaults = section["defaults"]
    if not isinstance(instances, dict):
        _fail("plugins.byId", "must be an object")
    if not isinstance(defaults, dict) or set(defaults) != PLUGIN_STAGES:
        _fail("plugins.defaults", "must specify exactly one instance for every stage")
    for instance_id, record in instances.items():
        _identifier(instance_id, "plugins.byId")
        path = f"plugins.byId[{instance_id}]"
        if not isinstance(record, dict) or set(record) != _INSTANCE_FIELDS:
            _fail(path, "must contain exactly the plugin-instance fields")
        if record["id"] != instance_id:
            _fail(f"{path}.id", "must match its map key")
        _text(record["name"], f"{path}.name")
        _text(record["plugin_id"], f"{path}.plugin_id", maximum=128)
        _text(record["implementation_version"], f"{path}.implementation_version", maximum=128)
        if not isinstance(record["stage"], str) or record["stage"] not in PLUGIN_STAGES:
            _fail(f"{path}.stage", "unsupported plugin stage")
        if type(record["config_schema_version"]) is not int or record["config_schema_version"] != PLUGIN_SCHEMA_VERSION:
            _fail(f"{path}.config_schema_version", "unsupported configuration schema version")
        if not isinstance(record["enabled"], bool):
            _fail(f"{path}.enabled", "must be a boolean")
        _validate_config(record["config"], f"{path}.config")
        _validate_bindings(record, path, document)
    for stage, instance_id in defaults.items():
        _identifier(instance_id, f"plugins.defaults.{stage}")
        selected = instances.get(instance_id)
        if selected is None or selected["stage"] != stage or not selected["enabled"]:
            _fail(f"plugins.defaults.{stage}", "must reference an enabled instance of the same stage")
