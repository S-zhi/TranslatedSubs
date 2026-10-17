"""Structural validation for immutable instance-backed task plans.

Reading a stored plan never consults live instance configuration or requires its
implementation to still be registered. The manager verifies it at execution.
"""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

PURPOSE_STAGES = {
    "download": "download",
    "extract.asr": "audio_extract", "extract.dubbing": "audio_extract",
    "separate.asr": "vocal_separate", "separate.dubbing": "vocal_separate",
    "transcribe": "transcribe", "translate": "translate", "tts": "tts",
    "render.subtitle": "render", "render.dub_mux": "render",
}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_MODEL_CREDENTIAL = re.compile(r"^model_card:(llm|whisper|tts):[A-Za-z0-9_.-]{1,160}:api_key$")
_SECRET_FIELD = re.compile(r"(?i)(api_?key|password|secret|token|authorization|cookie)")


def _reference(value: Any, purpose: str, config: dict[str, Any]) -> None:
    if value is None:
        return
    if purpose == "transcribe" and value == "settings:transcriber_api_key":
        return
    if purpose == "translate" and value == f"translation_engine:{config.get('engine_id')}:api_key":
        return
    if isinstance(value, str) and _MODEL_CREDENTIAL.fullmatch(value):
        kind = value.split(":")[1]
        if {"transcribe": "whisper", "translate": "llm", "tts": "tts"}.get(purpose) == kind:
            return
    raise ValueError("invalid credential reference in plugin plan")


def validate_instance_plan(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "config_revision", "slots"}:
        raise ValueError("invalid instance plugin plan fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 2:
        raise ValueError("unsupported instance plugin plan version")
    if type(value["config_revision"]) is not int or value["config_revision"] < 0:
        raise ValueError("invalid plugin plan configuration revision")
    if not isinstance(value["slots"], dict):
        raise ValueError("plugin plan slots must be an object")
    slots = {}
    for purpose, entry in value["slots"].items():
        if purpose not in PURPOSE_STAGES or not isinstance(entry, dict):
            raise ValueError("invalid plugin plan purpose")
        required = {"plugin_id", "implementation_version", "instance_id", "config_schema_version", "config"}
        if not required.issubset(entry) or set(entry) - required - {"model_ref", "engine_ref"}:
            raise ValueError("invalid instance plugin slot fields")
        for key in ("plugin_id", "implementation_version", "instance_id"):
            if not isinstance(entry[key], str) or not _IDENTIFIER.fullmatch(entry[key]):
                raise ValueError(f"invalid plugin plan identity: {key}")
        if type(entry["config_schema_version"]) is not int or entry["config_schema_version"] < 1:
            raise ValueError("invalid plugin configuration version")
        config = entry["config"]
        if not isinstance(config, dict) or len(config) > 64:
            raise ValueError("invalid plugin configuration")
        for key, item in config.items():
            if not isinstance(key, str) or not _IDENTIFIER.fullmatch(key):
                raise ValueError("invalid plugin configuration field")
            if _SECRET_FIELD.search(key) and key != "cookies_ref":
                raise ValueError("plugin plans cannot contain credential values")
            if item is not None and not isinstance(item, (str, bool, int, float)):
                raise ValueError("plugin configuration must use JSON scalar values")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("plugin configuration numbers must be finite")
            if isinstance(item, str) and len(item) > 8192:
                raise ValueError("plugin configuration value is too long")
            if isinstance(item, str) and (key.endswith("url") or key == "proxy") and item:
                parsed = urlsplit(item)
                if parsed.username is not None or parsed.password is not None or parsed.fragment or any(
                    _SECRET_FIELD.search(query_key) or query_key.lower() == "key"
                    for query_key, _ in parse_qsl(parsed.query)
                ):
                    raise ValueError("plugin plan URL contains credentials")
        if "credential_ref" in config:
            _reference(config["credential_ref"], purpose, config)
        if config.get("cookies_ref") not in {None, "settings:cookies_file"}:
            raise ValueError("invalid cookie reference in plugin plan")
        copied = {**entry, "config": dict(config)}
        model_ref = entry.get("model_ref")
        engine_ref = entry.get("engine_ref")
        if model_ref is not None:
            if not isinstance(model_ref, dict) or set(model_ref) != {"kind", "card_id"}:
                raise ValueError("invalid model reference in plugin plan")
            if model_ref["kind"] != {"transcribe": "whisper", "translate": "llm", "tts": "tts"}.get(purpose):
                raise ValueError("model reference does not match plugin purpose")
            if not isinstance(model_ref["card_id"], str) or not _IDENTIFIER.fullmatch(model_ref["card_id"]):
                raise ValueError("invalid model card identity")
            copied["model_ref"] = dict(model_ref)
        if engine_ref is not None:
            if purpose != "translate" or model_ref is not None or not isinstance(engine_ref, dict) or set(engine_ref) != {"id"}:
                raise ValueError("invalid engine reference in plugin plan")
            if not isinstance(engine_ref["id"], str) or not _IDENTIFIER.fullmatch(engine_ref["id"]):
                raise ValueError("invalid engine identity")
            copied["engine_ref"] = dict(engine_ref)
        slots[purpose] = copied
    return {"schema_version": 2, "config_revision": value["config_revision"], "slots": slots}
