"""Resolve and persist the versioned plugin plan for a task."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from types import MappingProxyType
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from src.config import settings as default_settings
from src.core.downloader import QUALITY_PRESETS

PLUGIN_PLAN_SCHEMA_VERSION = 1

DEFAULT_PLUGIN_IDS = {
    "download": "builtin.ytdlp",
    "audio_extract": "builtin.ffmpeg_audio",
    "vocal_separate": "builtin.vocal_demucs",
    "transcribe": "builtin.whisper",
    "translate": "builtin.translation_engine",
    "tts": "builtin.kokoro",
    "render": "builtin.ffmpeg_render",
}
IMPLEMENTATION_VERSION = "1"
_PLUGIN_STAGES = frozenset(DEFAULT_PLUGIN_IDS)

_SLOT_CONFIG_KEYS = {
    "download": {
        "quality", "format_selector", "proxy", "socket_timeout", "cookies_ref",
        "merge_output_format", "retries", "concurrent_fragments",
    },
    "extract.asr": {"sample_rate", "channels"},
    "extract.dubbing": {"sample_rate", "channels"},
    "separate.asr": {"backend", "model", "threads", "command", "timeout_seconds"},
    "separate.dubbing": {"backend", "model", "threads", "command", "timeout_seconds"},
    "transcribe": {
        "backend", "model", "service_url", "credential_ref", "timeout_seconds",
        "device", "compute_type", "download_root", "beam_size",
    },
    "translate": {
        "engine_id", "api_type", "base_url", "model", "credential_ref",
        "batch_size", "local_batch_size", "timeout_seconds",
    },
    "tts": {"voice", "target_lang", "bilingual", "speed", "model_id", "output_format"},
    "render.subtitle": {"mode"},
    "render.dub_mux": {"original_voice_mode"},
}
_SENSITIVE_QUERY_KEYS = re.compile(r"(?i)(token|api[_-]?key|key|password|auth|authorization|cookie)")


def _setting(settings: Any, name: str, default: Any = None) -> Any:
    return getattr(settings, name, default) if settings is not None else default


def _url_has_credentials_or_secret_query(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        return True
    for pair in parsed.query.split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0]
        if _SENSITIVE_QUERY_KEYS.search(key):
            return True
    return bool(parsed.fragment)


def _validate_non_sensitive_url(value: Any, field_name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a URL string")
    if _url_has_credentials_or_secret_query(value):
        raise ValueError(f"{field_name} contains credentials or a sensitive URL component")
    return value


def validate_plugin_plan(value: object) -> dict[str, Any]:
    """Read original plans unchanged and dispatch instance-backed v2 plans."""
    if isinstance(value, dict) and value.get("schema_version") == 2:
        from src.service.plugin_snapshot import validate_instance_plan
        return validate_instance_plan(value)
    if not isinstance(value, dict) or set(value) != {"schema_version", "slots"}:
        raise ValueError("plugin plan must contain only schema_version and slots")
    version = value["schema_version"]
    if type(version) is not int or version != PLUGIN_PLAN_SCHEMA_VERSION:
        raise ValueError(f"unsupported plugin plan schema version: {version}")
    slots = value["slots"]
    if not isinstance(slots, dict):
        raise ValueError("plugin plan slots must be an object")

    copied_slots: dict[str, dict[str, Any]] = {}
    for purpose, entry in slots.items():
        if purpose not in _SLOT_CONFIG_KEYS:
            raise ValueError(f"unsupported plugin plan slot: {purpose}")
        if not isinstance(entry, dict) or set(entry) != {
            "plugin_id", "implementation_version", "config"
        }:
            raise ValueError(f"invalid plugin plan entry: {purpose}")
        plugin_id = entry["plugin_id"]
        implementation_version = entry["implementation_version"]
        config = entry["config"]
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise ValueError(f"invalid plugin id in slot: {purpose}")
        if not isinstance(implementation_version, str) or not implementation_version.strip():
            raise ValueError(f"invalid implementation version in slot: {purpose}")
        if not isinstance(config, dict) or set(config) - _SLOT_CONFIG_KEYS[purpose]:
            raise ValueError(f"invalid configuration keys in slot: {purpose}")
        for key, item in config.items():
            if item is not None and not isinstance(item, (str, int, float, bool)):
                raise ValueError(f"plugin configuration must use JSON scalar values: {purpose}.{key}")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError(f"plugin configuration must use finite numbers: {purpose}.{key}")
        if "credential_ref" in config:
            ref = config["credential_ref"]
            engine_id = config.get("engine_id")
            if purpose == "transcribe":
                allowed_refs = {None, "settings:transcriber_api_key"}
            elif purpose == "translate":
                allowed_refs = {
                    None,
                    f"translation_engine:{engine_id}:api_key" if engine_id else None,
                }
            else:
                allowed_refs = {None}
            if ref is not None and ref not in allowed_refs:
                raise ValueError("invalid credential reference in plugin plan")
        if "cookies_ref" in config and config["cookies_ref"] not in {None, "settings:cookies_file"}:
            raise ValueError("invalid cookie reference in plugin plan")
        for key in ("base_url", "service_url", "proxy"):
            if key in config and _url_has_credentials_or_secret_query(config[key]):
                raise ValueError(f"plugin plan contains a sensitive {key} value")
        copied_slots[purpose] = {
            "plugin_id": plugin_id,
            "implementation_version": implementation_version,
            "config": dict(config),
        }
    return {"schema_version": PLUGIN_PLAN_SCHEMA_VERSION, "slots": copied_slots}


def serialize_plugin_plan(plan: Mapping[str, Any]) -> str:
    validated = validate_plugin_plan(dict(plan))
    return json.dumps(validated, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_plugin_plan(plugin_plan_json: str) -> dict[str, Any]:
    try:
        value = json.loads(plugin_plan_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("stored plugin plan is not valid JSON") from exc
    return validate_plugin_plan(value)


def resolve_plugin_defaults(
    plugin_defaults: Mapping[str, Any] | None = None,
) -> Mapping[str, Mapping[str, str]]:
    """Read and validate defaults once, returning an immutable stage mapping."""
    if plugin_defaults is None:
        raw = os.environ.get("SUBTRANS_PIPELINE_PLUGINS")
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("SUBTRANS_PIPELINE_PLUGINS must be valid JSON") from exc
            if not isinstance(parsed, dict):
                raise ValueError("SUBTRANS_PIPELINE_PLUGINS must be a JSON object")
            supplied: Mapping[str, Any] = parsed
        else:
            supplied = {}
    else:
        supplied = plugin_defaults

    unknown_stages = set(supplied) - _PLUGIN_STAGES
    if unknown_stages:
        raise ValueError(f"unsupported plugin default stages: {sorted(unknown_stages)}")
    resolved = {
        stage: {
            "plugin_id": plugin_id,
            "implementation_version": IMPLEMENTATION_VERSION,
        }
        for stage, plugin_id in DEFAULT_PLUGIN_IDS.items()
    }
    for stage, identity in supplied.items():
        if not isinstance(identity, Mapping) or set(identity) != {
            "plugin_id", "implementation_version"
        }:
            raise ValueError(f"invalid plugin default identity for stage: {stage}")
        plugin_id = identity["plugin_id"]
        implementation_version = identity["implementation_version"]
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise ValueError(f"plugin default id must be a non-empty string: {stage}")
        if not isinstance(implementation_version, str) or not implementation_version.strip():
            raise ValueError(f"plugin implementation version must be a non-empty string: {stage}")
        resolved[stage] = {
            "plugin_id": plugin_id,
            "implementation_version": implementation_version,
        }
    return MappingProxyType({
        stage: MappingProxyType(dict(identity))
        for stage, identity in resolved.items()
    })


def _slot(
    stage: str,
    config: dict[str, Any],
    plugin_defaults: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    identity = plugin_defaults[stage]
    return {
        "plugin_id": identity["plugin_id"],
        "implementation_version": identity["implementation_version"],
        "config": config,
    }


def _effective_transcriber_model(model: str, backend: Any) -> tuple[str, str]:
    """Match the legacy orchestrator's model-prefix backend selection."""
    selected_backend = str(backend or "local_whisper").strip().lower()
    selected_model = str(model)
    if ":" in selected_model:
        prefix, suffix = selected_model.split(":", 1)
        if prefix.strip().lower() in {"local", "local_whisper", "whisper", "faster_whisper"}:
            selected_backend = "local_whisper"
            selected_model = suffix.strip()
    elif selected_backend in {"local", "whisper", "faster_whisper"}:
        selected_backend = "local_whisper"
    return selected_backend, selected_model


def _effective_download_format(quality: str, settings: Any) -> str:
    normalized = str(quality or "").strip().lower()
    if normalized in QUALITY_PRESETS:
        return QUALITY_PRESETS[normalized]
    if normalized:
        height = normalized.rstrip("p")
        if height.isdigit():
            return f"bv*[height<={height}]+ba/b[height<={height}]"
    return str(_setting(settings, "download_format", "bestvideo+bestaudio/best"))


def resolve_plugin_plan(
    *,
    source_type: str,
    source_lang: str,
    target_lang: str,
    mode: str,
    burn: str,
    model: str,
    engine: str,
    need_subtitle: bool,
    quality: str | None,
    tts_enabled: bool,
    tts_voice: str,
    original_voice_mode: str,
    settings: Any = None,
    engines: Any = None,
    plugin_defaults: Mapping[str, Any] | None = None,
) -> str:
    """Resolve one task plan from persisted request choices and backend defaults."""
    settings = settings or default_settings
    resolved_plugin_defaults = resolve_plugin_defaults(plugin_defaults)
    if source_type not in {"url", "upload"}:
        raise ValueError("source_type must be url or upload")
    slots: dict[str, dict[str, Any]] = {}
    if source_type == "url":
        proxy = _validate_non_sensitive_url(_setting(settings, "download_proxy"), "proxy")
        cookies_ref = "settings:cookies_file" if _setting(settings, "cookies_file") else None
        effective_quality = quality or "480p"
        format_selector = _effective_download_format(effective_quality, settings)
        slots["download"] = _slot(
            "download", {
                "quality": effective_quality,
                "format_selector": format_selector,
                "proxy": proxy,
                "socket_timeout": int(_setting(settings, "download_socket_timeout", 30)),
                "cookies_ref": cookies_ref,
                "merge_output_format": str(_setting(settings, "merge_output_format", "mp4")),
                "retries": int(_setting(settings, "download_retries", 5)),
                "concurrent_fragments": int(_setting(settings, "download_concurrent_fragments", 4)),
            }, resolved_plugin_defaults,
        )

    if need_subtitle:
        separation_enabled = bool(_setting(settings, "vocal_separation_enabled", False))
        sample_rate = 48000 if separation_enabled else int(_setting(settings, "audio_sample_rate", 16000))
        channels = 2 if separation_enabled else int(_setting(settings, "audio_channels", 1))
        slots["extract.asr"] = _slot(
            "audio_extract", {"sample_rate": sample_rate, "channels": channels}, resolved_plugin_defaults
        )
        separation_config = {
            "backend": _setting(settings, "vocal_separation_backend", "demucs"),
            "model": _setting(settings, "vocal_separation_model", "htdemucs"),
            "threads": int(_setting(settings, "vocal_separation_threads", 1)),
            "command": _setting(settings, "vocal_separation_command", "python -m demucs.separate"),
            "timeout_seconds": int(_setting(settings, "vocal_separation_timeout", 60)),
        }
        if separation_enabled:
            slots["separate.asr"] = _slot(
                "vocal_separate", dict(separation_config), resolved_plugin_defaults
            )

        transcriber_backend, effective_model = _effective_transcriber_model(
            model, _setting(settings, "transcriber_backend", "local_whisper")
        )
        service_url = None
        credential_ref = None
        timeout_seconds = int(_setting(settings, "transcriber_timeout", 1800))
        if str(transcriber_backend).lower() in {"http", "custom", "custom_http"}:
            service_url = _validate_non_sensitive_url(
                _setting(settings, "transcriber_url"), "service_url"
            )
            credential_ref = (
                "settings:transcriber_api_key"
                if _setting(settings, "transcriber_api_key")
                else None
            )
        else:
            transcriber_backend = "local_whisper"
        slots["transcribe"] = _slot(
            "transcribe", {
                "backend": transcriber_backend,
                "model": effective_model,
                "service_url": service_url,
                "credential_ref": credential_ref,
                "timeout_seconds": timeout_seconds,
                "device": "cpu",
                "compute_type": "int8",
                "download_root": None,
                "beam_size": int(_setting(settings, "local_whisper_beam_size", 5)),
            }, resolved_plugin_defaults,
        )

        builtin_translation = resolved_plugin_defaults["translate"]["plugin_id"] == "builtin.translation_engine"
        if engines is None and builtin_translation:
            raise ValueError("translation engine store is required for subtitle tasks")
        engine_record = engines.get(engine) if engines is not None else None
        if engine_record is None and builtin_translation:
            raise ValueError("translation engine configuration does not exist")
        if engine_record is not None and engine_record.id != engine:
            raise ValueError("translation engine store returned a mismatched configuration")
        if engine_record is None:
            engine_record = SimpleNamespace(id=engine, api_type="openai_compatible", base_url="", model="")
        api_type = engine_record.api_type
        if api_type not in {"local_ct2", "openai_compatible", "anthropic_compatible"}:
            raise ValueError("unsupported translation engine type")
        if (
            resolved_plugin_defaults["translate"]["plugin_id"] == "builtin.translation_engine"
            and api_type == "local_ct2"
            and not (
                source_lang == "en" and target_lang.lower() in {"zh", "zh-cn"}
            )
        ):
            raise ValueError("local_ct2 only supports en to zh-CN or zh")
        base_url = _validate_non_sensitive_url(engine_record.base_url, "base_url") or ""
        credential_ref = (
            f"translation_engine:{engine_record.id}:api_key"
            if builtin_translation and api_type != "local_ct2" else None
        )
        slots["translate"] = _slot(
            "translate", {
                "engine_id": engine_record.id,
                "api_type": api_type,
                "base_url": base_url,
                "model": engine_record.model,
                "credential_ref": credential_ref,
                "batch_size": int(_setting(settings, "translate_batch_size", 20)),
                "local_batch_size": int(_setting(settings, "local_translate_batch_size", 8)),
                "timeout_seconds": 60,
            }, resolved_plugin_defaults,
        )
        slots["render.subtitle"] = _slot(
            "render", {"mode": burn}, resolved_plugin_defaults
        )

        if tts_enabled:
            slots["extract.dubbing"] = _slot(
                "audio_extract", {"sample_rate": 48000, "channels": 2}, resolved_plugin_defaults
            )
            if original_voice_mode != "keep":
                slots["separate.dubbing"] = _slot(
                    "vocal_separate", dict(separation_config), resolved_plugin_defaults
                )
            slots["tts"] = _slot(
                "tts", {
                    "voice": tts_voice,
                    "target_lang": target_lang,
                    "bilingual": mode == "bilingual",
                }, resolved_plugin_defaults,
            )
            slots["render.dub_mux"] = _slot(
                "render", {"original_voice_mode": original_voice_mode}, resolved_plugin_defaults
            )

    return serialize_plugin_plan({"schema_version": PLUGIN_PLAN_SCHEMA_VERSION, "slots": slots})


def ensure_plan(
    record: Any,
    store: Any,
    engines: Any,
    settings: Any = None,
    runtime_snapshot: dict[str, Any] | None = None,
) -> Any:
    """Return a record with its existing plan or atomically materialize a legacy plan.

    This is the runner/recovery entry point. NULL plans are resolved once using
    the historical task fields and current defaults, then compare-and-set in the
    task store so competing workers all consume the persisted winner.
    """
    current_json = getattr(record, "plugin_plan_json", None)
    if current_json:
        parse_plugin_plan(current_json)
        return record
    plan_engines = engines
    engine_snapshot = (runtime_snapshot or {}).get("engine")
    if isinstance(engine_snapshot, dict) and engine_snapshot.get("source") != "disabled":
        engine_id = str(engine_snapshot.get("id") or record.engine)
        provider = str(engine_snapshot.get("provider") or "").strip().lower()
        api_type = (
            "anthropic_compatible"
            if provider in {"anthropic", "anthropic_compatible"}
            else "local_ct2" if provider == "local" else "openai_compatible"
        )
        snapshot_engine = SimpleNamespace(
            id=engine_id,
            api_type=api_type,
            base_url=str(engine_snapshot.get("baseUrl") or ""),
            model=str(engine_snapshot.get("model") or ""),
        )

        class _SnapshotEngineStore:
            def get(self, requested_id: str) -> Any:
                if requested_id == engine_id:
                    return snapshot_engine
                return engines.get(requested_id) if engines is not None else None

        plan_engines = _SnapshotEngineStore()

    plan_json = resolve_plugin_plan(
        source_type=record.source_type,
        source_lang=record.source_lang,
        target_lang=record.target_lang,
        mode=record.mode,
        burn=record.burn,
        model=record.model,
        engine=record.engine,
        need_subtitle=bool(record.need_subtitle),
        quality=record.quality,
        tts_enabled=bool(record.tts_enabled),
        tts_voice=record.tts_voice,
        original_voice_mode=record.original_voice_mode,
        settings=settings,
        engines=plan_engines,
    )
    if isinstance(runtime_snapshot, dict):
        decoded = json.loads(plan_json)
        slots = decoded["slots"]

        engine = runtime_snapshot.get("engine") or {}
        translate = slots.get("translate")
        if engine.get("source") == "disabled":
            slots.pop("translate", None)
            slots.pop("render.subtitle", None)
        elif translate:
            provider = str(engine.get("provider") or "").strip().lower()
            translate["config"].update({
                "engine_id": str(engine.get("id") or record.engine),
                "api_type": (
                    "anthropic_compatible"
                    if provider in {"anthropic", "anthropic_compatible"}
                    else "local_ct2" if provider == "local" else "openai_compatible"
                ),
                "base_url": str(engine.get("baseUrl") or ""),
                "model": str(engine.get("model") or ""),
            })
            if translate["config"]["api_type"] == "local_ct2":
                translate["config"]["credential_ref"] = None
            else:
                translate["config"]["credential_ref"] = (
                    f"translation_engine:{translate['config']['engine_id']}:api_key"
                )

        whisper = runtime_snapshot.get("whisper") or {}
        transcribe = slots.get("transcribe")
        if whisper.get("source") == "disabled":
            slots.pop("transcribe", None)
        elif transcribe:
            backend, model = _effective_transcriber_model(
                str(whisper.get("model") or record.model), whisper.get("provider")
            )
            transcribe["config"]["backend"] = backend
            transcribe["config"]["model"] = model

        tts = runtime_snapshot.get("tts") or {}
        tts_slot = slots.get("tts")
        if not tts.get("enabled"):
            for purpose in (
                "tts", "extract.dubbing", "separate.dubbing", "render.dub_mux"
            ):
                slots.pop(purpose, None)
        elif tts_slot:
            tts_config = tts_slot["config"]
            if tts.get("model") is not None:
                tts_config["model_id"] = tts["model"]
            if tts.get("voice") is not None:
                tts_config["voice"] = tts["voice"]
            if tts.get("speed") is not None:
                tts_config["speed"] = tts["speed"]
            if tts.get("format") is not None:
                tts_config["output_format"] = tts["format"]
        plan_json = serialize_plugin_plan(decoded)
    updated = store.ensure_plugin_plan(record.id, plan_json)
    if updated is None:
        raise LookupError("task disappeared while materializing its plugin plan")
    parse_plugin_plan(updated.plugin_plan_json)
    return updated
