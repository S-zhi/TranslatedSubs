"""Explicit one-time import of settings from a legacy dotenv file.

Nothing in application startup reads this file. Run with
``python -m src.config.migrate_legacy --legacy-env-file PATH`` after Redis has
been initialized. Existing Redis values win over imported values.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from .config_repository import get_config_provider, RedisConfigProvider
from .redis_store import ConfigUnavailable
from .schema import BUSINESS_DEFAULTS, SECRET_KEYS


ENV_TO_SETTING = {
    "SUBTRANS_API_TOKEN": "api_token",
    "SUBTRANS_WORKERS": "pipeline_workers",
    "SUBTRANS_DOWNLOAD_WORKERS": "download_workers",
    "SUBTRANS_DL_CONCURRENT_FRAGMENTS": "download_concurrent_fragments",
    "SUBTRANS_STREAM_TIMEOUT_SEC": "stream_timeout_sec",
    "SUBTRANS_READINESS_TTL_SEC": "readiness_ttl_sec",
    "SUBTRANS_PROBE_CACHE_TTL_SEC": "probe_cache_ttl_sec",
    "SUBTRANS_CORS_ORIGINS": "cors_allow_origins",
    "SUBTRANS_DL_QUALITY": "download_quality",
    "SUBTRANS_DL_FORMAT": "download_format",
    "SUBTRANS_DL_SOCKET_TIMEOUT": "download_socket_timeout",
    "SUBTRANS_PROXY": "download_proxy",
    "SUBTRANS_COOKIES": "cookies_file",
    "SUBTRANS_DEBUG": "debug",
    "SUBTRANS_DL_CONTAINER": "merge_output_format",
    "SUBTRANS_ALLOW_COOKIES_FROM_BROWSER": "allow_cookies_from_browser",
    "SUBTRANS_STARTUP_PROBE_ENABLED": "startup_probe_enabled",
    "SUBTRANS_DL_RETRIES": "download_retries",
    "SUBTRANS_MAX_UPLOAD_MB": "max_upload_mb",
    "SUBTRANS_MAX_VIDEO_MINUTES": "max_video_minutes",
    "SUBTRANS_FFMPEG": "ffmpeg_bin",
    "SUBTRANS_FFPROBE": "ffprobe_bin",
    "SUBTRANS_AUDIO_SR": "audio_sample_rate",
    "SUBTRANS_AUDIO_CH": "audio_channels",
    "SUBTRANS_VOCAL_SEPARATION": "vocal_separation_enabled",
    "SUBTRANS_VOCAL_SEPARATION_BACKEND": "vocal_separation_backend",
    "SUBTRANS_VOCAL_SEPARATION_MODEL": "vocal_separation_model",
    "SUBTRANS_VOCAL_SEPARATION_THREADS": "vocal_separation_threads",
    "SUBTRANS_VOCAL_SEPARATION_COMMAND": "vocal_separation_command",
    "SUBTRANS_VOCAL_SEPARATION_TIMEOUT": "vocal_separation_timeout",
    "SUBTRANS_TRANSCRIBER_BACKEND": "transcriber_backend",
    "SUBTRANS_TRANSCRIBER_URL": "transcriber_url",
    "SUBTRANS_TRANSCRIBER_API_KEY": "transcriber_api_key",
    "SUBTRANS_TRANSCRIBER_TIMEOUT": "transcriber_timeout",
    "SUBTRANS_LOCAL_WHISPER_MODEL": "local_whisper_model",
    "SUBTRANS_LOCAL_WHISPER_DEVICE": "local_whisper_device",
    "SUBTRANS_LOCAL_WHISPER_COMPUTE_TYPE": "local_whisper_compute_type",
    "SUBTRANS_LOCAL_WHISPER_DOWNLOAD_ROOT": "local_whisper_download_root",
    "SUBTRANS_LOCAL_WHISPER_BEAM_SIZE": "local_whisper_beam_size",
    "SUBTRANS_TRANSLATE_BATCH": "translate_batch_size",
    "SUBTRANS_LOCAL_TRANSLATE_BATCH": "local_translate_batch_size",
    "SUBTRANS_TRANSLATE_TIMEOUT": "translate_timeout",
    "SUBTRANS_TARGET_LANGUAGES": "target_languages",
    "SUBTRANS_LANG_NAMES": "lang_names",
    "SUBTRANS_DRIVE_API_BASE_URL": "drive_api_base_url",
    "SUBTRANS_DRIVE_TIMEOUT": "drive_timeout",
    "SUBTRANS_DRIVE_TRANSFER_TIMEOUT": "drive_transfer_timeout",
    "SUBTRANS_LOCAL_CT2_INTER_THREADS": "local_ct2_inter_threads",
    "SUBTRANS_LOCAL_CT2_INTRA_THREADS": "local_ct2_intra_threads",
}


def _parse_dotenv(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        parsed[key.strip()] = value.strip().strip("\"'")
    return parsed


def _convert(key: str, value: str):
    default = BUSINESS_DEFAULTS[key]
    if key in SECRET_KEYS or default is None:
        return value or None
    if isinstance(default, bool):
        normalized = value.strip().lower()
        if normalized not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
            raise ValueError(f"invalid boolean value for {key}")
        return normalized in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        return int(value)
    if isinstance(default, list):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(default, dict):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError(f"invalid mapping for {key}")
        merged = dict(default)
        merged.update({str(k): str(v) for k, v in parsed.items()})
        return merged
    return value


def _read_models(db_path: Path | None) -> dict[str, dict[str, Any]]:
    if db_path is None or not db_path.is_file():
        return {}
    with sqlite3.connect(db_path) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "model_settings" not in tables:
            return {}
        return {
            str(kind): json.loads(config_json)
            for kind, config_json in connection.execute(
                "SELECT kind, config_json FROM model_settings"
            )
        }


def _read_engines(db_path: Path | None) -> dict[str, dict[str, Any]]:
    if db_path is None or not db_path.is_file():
        return {}
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "translation_engines" not in tables:
            return {}
        return {
            str(row["id"]): dict(row)
            for row in connection.execute("SELECT * FROM translation_engines")
        }


def _read_json_object(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"legacy JSON file must contain an object: {path.name}")
    return value


def import_legacy_sources(
    *,
    env_file: Path | None = None,
    db_path: Path | None = None,
    runtime_file: Path | None = None,
    retention_file: Path | None = None,
    merge_defaults: bool = False,
    provider=None,
) -> dict[str, int]:
    for label, source_path in (
        ("legacy env file", env_file), ("legacy database", db_path),
        ("legacy runtime file", runtime_file), ("legacy retention file", retention_file),
    ):
        if source_path is not None and not source_path.is_file():
            raise FileNotFoundError(f"{label} does not exist: {source_path}")

    provider = provider or get_config_provider()
    initialize = getattr(provider, "initialize", None)
    was_empty = False
    if initialize:
        try:
            provider.read()
        except ConfigUnavailable:
            was_empty = True
        initialize()

    business_by_source: dict[str, dict[str, Any]] = {}
    legacy = {}
    if env_file is not None:
        legacy = _parse_dotenv(env_file)
        if "SUBTRANS_PROXY" not in legacy:
            for proxy_key in ("HTTPS_PROXY", "HTTP_PROXY"):
                if legacy.get(proxy_key):
                    legacy["SUBTRANS_PROXY"] = legacy[proxy_key]
                    break
        business_by_source["env"] = {
            ENV_TO_SETTING[env_key]: _convert(ENV_TO_SETTING[env_key], value)
            for env_key, value in legacy.items()
            if env_key in ENV_TO_SETTING
        }

    runtime = _read_json_object(runtime_file)
    runtime_values = runtime.get("business", runtime.get("values", runtime))
    if isinstance(runtime_values, dict) and isinstance(runtime_values.get("business"), dict):
        runtime_models = runtime_values.get("models", {})
        runtime_engines = runtime_values.get("engines", {}).get("byId", {})
        runtime_values = runtime_values["business"]
    else:
        runtime_models = runtime.get("models", {})
        runtime_engines = runtime.get("engines", {}).get("byId", {}) if isinstance(
            runtime.get("engines"), dict
        ) else {}
    if isinstance(runtime_values, dict):
        business_by_source["runtime_file"] = {
            key: value for key, value in runtime_values.items() if key in BUSINESS_DEFAULTS
        }

    retention = _read_json_object(retention_file)
    business_by_source["retention_file"] = {}
    if "days" in retention:
        business_by_source["retention_file"]["retention_days"] = retention["days"]
    if "lastRunAt" in retention:
        business_by_source["retention_file"]["retention_last_run_at"] = retention["lastRunAt"]

    # The previous audio bridge stored this key under the same namespaced
    # prefix. Only the explicit migration command probes it.
    if isinstance(provider, RedisConfigProvider):
        old_audio = provider.store.get("runtime:audio")
        if old_audio and isinstance(old_audio.values, dict):
            business_by_source["legacy_audio"] = {}
            for key, value in old_audio.values.items():
                if key in BUSINESS_DEFAULTS:
                    business_by_source["legacy_audio"][key] = value

    models = _read_models(db_path)
    llm = models.get("llm")
    if isinstance(llm, dict) and llm.get("apiKey"):
        cards = llm.get("models") if isinstance(llm.get("models"), list) else []
        current_card = next((card for card in cards if card.get("isCurrent")), None)
        if current_card is None and cards:
            current_card = cards[0]
        if current_card is not None and not current_card.get("apiKey"):
            current_card["apiKey"] = llm["apiKey"]
    for kind, value in runtime_models.items() if isinstance(runtime_models, dict) else ():
        models.setdefault(kind, value)
    engines = _read_engines(db_path)
    if isinstance(runtime_engines, dict):
        for engine_id, value in runtime_engines.items():
            engines.setdefault(engine_id, value)
    if env_file is not None:
        deepseek = {
            "id": "deepseek",
            "name": "DeepSeek",
            "api_type": "openai_compatible",
            "base_url": legacy.get("SUBTRANS_DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/"),
            "model": legacy.get("SUBTRANS_DEEPSEEK_MODEL", "deepseek-chat").strip(),
            "api_key": (legacy.get("SUBTRANS_DEEPSEEK_API_KEY") or legacy.get("DEEPSEEK_API_KEY") or "").strip() or None,
            "enabled": 1,
            "availability": "UNKNOWN" if legacy.get("SUBTRANS_DEEPSEEK_API_KEY") or legacy.get("DEEPSEEK_API_KEY") else "UNCONFIGURED",
            "last_checked_at": None,
            "last_error": None,
            "api_key_rotated_at": None,
            "created_at": 0,
            "updated_at": 0,
        }
        if any((deepseek["api_key"], "SUBTRANS_DEEPSEEK_BASE_URL" in legacy,
                "SUBTRANS_DEEPSEEK_MODEL" in legacy)):
            engines.setdefault("deepseek", deepseek)
    counts = {"business": 0, "models": 0, "engines": 0}
    current = provider.read()
    existing_sources = set(
        current.values.get("meta", {}).get("legacy_sources_imported", [])
    )
    # Apply low-to-high precedence deterministically. Redis values are checked
    # separately below and always win; among legacy inputs the old audio key
    # wins over runtime JSON, which wins over dotenv. Retention is independent
    # state and is applied last for its retention fields.
    source_order = ("env", "runtime_file", "legacy_audio", "retention_file")
    new_sources = [
        source for source in source_order
        if source in business_by_source and source not in existing_sources
    ]
    if was_empty:
        merge_defaults = True
    sources_to_mark = set(new_sources)
    if not merge_defaults:
        for source in new_sources:
            if any(
                key != "retention_last_run_at"
                and key in current.values.get("business", {})
                and current.values["business"][key] == BUSINESS_DEFAULTS.get(key)
                for key in business_by_source[source]
            ):
                # Leave an ambiguous default pending so a later explicit
                # --merge-defaults run can still import it.
                sources_to_mark.discard(source)
    existing_models = current.values.get("models", {})
    existing_engines = current.values.get("engines", {}).get("byId", {})
    pending_models = {key: value for key, value in models.items() if key not in existing_models}
    pending_engines = {key: value for key, value in engines.items() if key not in existing_engines}
    business_work = any(
        key not in current.values.get("business", {})
        or (merge_defaults and current.values["business"].get(key) == BUSINESS_DEFAULTS.get(key))
        for source in new_sources for key in business_by_source[source]
    )
    if not sources_to_mark and not business_work and not pending_models and not pending_engines:
        return {"business": 0, "models": 0, "engines": 0, "revision": current.revision}

    def apply(document):
        business = document.setdefault("business", {})
        # Resolve legacy collisions before mutating the document, so a lower
        # priority source cannot prevent a later, higher priority source from
        # supplying the value.
        imported_business = {}
        for source in new_sources:
            imported_business.update(business_by_source[source])
        for key, value in imported_business.items():
            if key == "retention_last_run_at":
                can_import = key not in business
            else:
                can_import = key not in business or (
                    merge_defaults and business[key] == BUSINESS_DEFAULTS.get(key)
                )
            if can_import:
                business[key] = value
                counts["business"] += 1
        meta = document.setdefault("meta", {})
        imported_sources = set(meta.get("legacy_sources_imported", []))
        meta["legacy_sources_imported"] = sorted(imported_sources | sources_to_mark)
        target_models = document.setdefault("models", {})
        for kind, value in pending_models.items():
            if kind not in target_models:
                target_models[kind] = value
                counts["models"] += 1
        by_id = document.setdefault("engines", {}).setdefault("byId", {})
        for engine_id, value in pending_engines.items():
            if engine_id not in by_id:
                if value.get("availability") == "CHECKING":
                    value["availability"] = "UNKNOWN" if value.get("api_key") else "UNCONFIGURED"
                by_id[engine_id] = value
                counts["engines"] += 1
        return document

    snapshot = provider.mutate(apply, expected_revision=current.revision)
    counts["revision"] = snapshot.revision
    return counts


def import_legacy_env(path: Path) -> tuple[int, int]:
    counts = import_legacy_sources(env_file=path, merge_defaults=True)
    return counts["business"], len({
        ENV_TO_SETTING[env_key]: _convert(ENV_TO_SETTING[env_key], value)
        for env_key, value in _parse_dotenv(path).items()
        if env_key in ENV_TO_SETTING
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="Import legacy .env values into Redis once")
    parser.add_argument("--legacy-env-file", type=Path)
    parser.add_argument("--legacy-db", type=Path)
    parser.add_argument("--legacy-runtime-file", type=Path)
    parser.add_argument("--legacy-retention-file", type=Path)
    parser.add_argument(
        "--merge-defaults", action="store_true",
        help="on an initialized Redis, allow imports to fill fields still at schema defaults; custom Redis values always win",
    )
    args = parser.parse_args()
    if not any((args.legacy_env_file, args.legacy_db, args.legacy_runtime_file,
                args.legacy_retention_file)):
        parser.error("provide at least one --legacy-* source")
    counts = import_legacy_sources(
        env_file=args.legacy_env_file, db_path=args.legacy_db,
        runtime_file=args.legacy_runtime_file, retention_file=args.legacy_retention_file,
        merge_defaults=args.merge_defaults,
    )
    print("Imported counts: " + ", ".join(
        f"{key}={counts[key]}" for key in ("business", "models", "engines")
    ) + f"; revision={counts['revision']}. Existing non-default Redis values and IDs were preserved.")


if __name__ == "__main__":
    main()
