"""Validation for model cards and translation-engine records.

These helpers deliberately report field names and constraints only. Invalid
values can contain credentials, so validation errors must never echo them.
"""
from __future__ import annotations

import math
from typing import Any


def _fail(path: str, reason: str) -> None:
    raise ValueError(f"invalid configuration field {path}: {reason}")


def _string(record: dict[str, Any], key: str, path: str, *, required=False,
            nullable=False, max_length=4096) -> None:
    if key not in record:
        if required:
            _fail(f"{path}.{key}", "required")
        return
    value = record[key]
    if value is None and nullable:
        return
    if not isinstance(value, str) or len(value) > max_length:
        _fail(f"{path}.{key}", "must be a string")


def _number(record: dict[str, Any], key: str, path: str, *, minimum: float,
            maximum: float, integer=False) -> None:
    if key not in record:
        return
    value = record[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{path}.{key}", "must be numeric")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        _fail(f"{path}.{key}", "outside the supported range")
    if integer and not isinstance(value, int):
        _fail(f"{path}.{key}", "must be an integer")


def _boolean(record: dict[str, Any], key: str, path: str) -> None:
    if key in record and not isinstance(record[key], bool):
        _fail(f"{path}.{key}", "must be a boolean")


def _validate_card(card: Any, path: str) -> None:
    if not isinstance(card, dict):
        _fail(path, "must be an object")
    _string(card, "id", path, required=True, max_length=128)
    if not card.get("id", "").strip():
        _fail(f"{path}.id", "must not be empty")
    for key in ("name", "provider", "model", "baseUrl", "language", "device", "voice", "format"):
        _string(card, key, path)
    for key in ("apiKey", "api_key"):
        _string(card, key, path, nullable=True, max_length=2048)
    for key in ("enabled", "isCurrent", "wordTimestamps"):
        _boolean(card, key, path)
    _number(card, "temperature", path, minimum=0, maximum=2)
    _number(card, "speed", path, minimum=0.1, maximum=4)
    _number(card, "maxTokens", path, minimum=1, maximum=1_000_000, integer=True)
    _number(card, "timeout", path, minimum=1, maximum=86_400, integer=True)
    _number(card, "concurrency", path, minimum=1, maximum=256, integer=True)
    _number(card, "threads", path, minimum=1, maximum=512, integer=True)


def validate_models(models: Any) -> None:
    """Validate `{kind: settings}` model configuration and card identities."""
    if not isinstance(models, dict):
        _fail("models", "must be an object")
    for kind, config in models.items():
        if kind not in {"llm", "whisper", "tts"}:
            _fail(f"models.{kind}", "unsupported model category")
        path = f"models.{kind}"
        if not isinstance(config, dict):
            _fail(path, "must be an object")
        _boolean(config, "enabled", path)
        _boolean(config, "_runtimeManaged", path)
        for key in ("provider", "model", "baseUrl", "language", "device", "voice", "format"):
            _string(config, key, path)
        for key in ("apiKey", "api_key"):
            _string(config, key, path, nullable=True, max_length=2048)
        _number(config, "temperature", path, minimum=0, maximum=2)
        _number(config, "speed", path, minimum=0.1, maximum=4)
        _number(config, "maxTokens", path, minimum=1, maximum=1_000_000, integer=True)
        _number(config, "timeout", path, minimum=1, maximum=86_400, integer=True)
        _number(config, "concurrency", path, minimum=1, maximum=256, integer=True)
        _number(config, "threads", path, minimum=1, maximum=512, integer=True)
        if "wordTimestamps" in config and not isinstance(config["wordTimestamps"], bool):
            _fail(f"{path}.wordTimestamps", "must be a boolean")
        cards = config.get("models", [])
        if not isinstance(cards, list):
            _fail(f"{path}.models", "must be an array")
        ids: set[str] = set()
        current_count = 0
        for index, card in enumerate(cards):
            card_path = f"{path}.models[{index}]"
            _validate_card(card, card_path)
            card_id = card["id"]
            if card_id in ids:
                _fail(f"{card_path}.id", "must be unique within the category")
            ids.add(card_id)
            current_count += int(card.get("isCurrent", False))
        if current_count > 1:
            _fail(f"{path}.models", "at most one card may be current")


def validate_engines(by_id: Any) -> None:
    """Validate Redis engine map, including map-key/record-ID consistency."""
    if not isinstance(by_id, dict):
        _fail("engines.byId", "must be an object")
    allowed_types = {"openai_compatible", "anthropic_compatible", "local_ct2"}
    allowed_availability = {"UNCONFIGURED", "UNKNOWN", "CHECKING", "AVAILABLE", "UNAVAILABLE"}
    for engine_id, record in by_id.items():
        path = f"engines.byId[{engine_id}]"
        if not isinstance(engine_id, str) or not engine_id.strip() or len(engine_id) > 128:
            _fail("engines.byId", "keys must be non-empty strings")
        if not isinstance(record, dict):
            _fail(path, "must be an object")
        _string(record, "id", path, required=True, max_length=128)
        if record.get("id") != engine_id:
            _fail(f"{path}.id", "must match its map key")
        for key in ("name", "base_url", "model"):
            _string(record, key, path, required=True, max_length=2048)
        _string(record, "api_type", path, required=True, max_length=64)
        if record["api_type"] not in allowed_types:
            _fail(f"{path}.api_type", "unsupported engine type")
        _string(record, "api_key", path, nullable=True, max_length=2048)
        enabled = record.get("enabled", 1)
        if isinstance(enabled, bool):
            pass
        elif not isinstance(enabled, int) or enabled not in (0, 1):
            _fail(f"{path}.enabled", "must be a boolean")
        availability = record.get("availability", "UNCONFIGURED")
        if availability not in allowed_availability:
            _fail(f"{path}.availability", "unsupported availability")
        for key in ("last_checked_at", "api_key_rotated_at", "created_at", "updated_at"):
            value = record.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                _fail(f"{path}.{key}", "must be a nonnegative integer or null")
        _string(record, "last_error", path, nullable=True, max_length=4096)
