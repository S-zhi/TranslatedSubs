"""Shared Redis-backed settings and machine-local bootstrap behavior."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import pytest

from src.config import config
from src.config.bootstrap import DEFAULT_BOOTSTRAP
from src.config.config_repository import MemoryConfigProvider, set_config_provider
from src.config.schema import default_document


def test_settings_use_provider_and_keep_explicit_dataclass_overrides(memory_config_provider):
    values = default_document()
    values["business"].update({"pipeline_workers": 5, "debug": True})
    set_config_provider(MemoryConfigProvider(values))
    settings = config.Settings()

    assert settings.pipeline_workers == 5
    assert settings.debug is True
    replaced = dataclasses.replace(settings, _debug=False, _pipeline_workers_override=2)
    assert replaced.debug is False
    assert replaced.pipeline_workers == 2


def test_capture_snapshot_excludes_secrets(memory_config_provider):
    values = default_document()
    values["business"].update({"api_token": "api-secret", "transcriber_api_key": "stt-secret"})
    set_config_provider(MemoryConfigProvider(values))

    snapshot = config.Settings().capture_business_snapshot()
    assert snapshot["pipeline_workers"] == 8
    assert "api_token" not in snapshot
    assert "transcriber_api_key" not in snapshot


def test_effective_settings_preserve_quality_mapping_and_disabled_languages(
    memory_config_provider,
):
    memory_config_provider.mutate(lambda document: document["business"].update({
        "download_quality": "720p",
        "target_languages": ["bn", "ur", "ja"],
    }))
    assert config.settings.download_format == "bv*[height<=720]+ba/b[height<=720]"
    assert config.settings.target_languages == ("ja",)
    captured = config.settings.capture_business_snapshot()
    assert captured["download_format"] == config.settings.download_format
    assert captured["target_languages"] == ["ja"]

    memory_config_provider.mutate(lambda document: document["business"].update({
        "download_format": "custom-selector",
    }))
    assert config.settings.download_format == "custom-selector"


def test_internal_retention_state_is_not_in_task_business_snapshot(memory_config_provider):
    memory_config_provider.mutate(
        lambda document: document["business"].update({"retention_last_run_at": 1234})
    )
    snapshot = config.settings.capture_business_snapshot()
    assert "retention_last_run_at" not in snapshot


def test_update_runtime_settings_persists_shared_business_values(memory_config_provider):
    result = config.update_runtime_settings({"retention_days": 14})
    assert result["retention_days"] == 14
    assert config.get_runtime_settings()["retention_days"] == 14


def test_machine_paths_come_from_local_bootstrap(monkeypatch, tmp_path):
    bootstrap = {key: dict(value) for key, value in DEFAULT_BOOTSTRAP.items()}
    bootstrap["paths"].update({"dataDir": str(tmp_path / "data"), "dbPath": str(tmp_path / "db.sqlite")})
    monkeypatch.setattr(config, "load_bootstrap", lambda: bootstrap)

    settings = config.Settings(backend_dir=tmp_path)
    assert settings.data_dir == tmp_path / "data"
    assert settings.db_path == tmp_path / "db.sqlite"


def test_legacy_env_import_preserves_existing_redis_values(tmp_path, memory_config_provider):
    from src.config.migrate_legacy import import_legacy_env

    legacy = tmp_path / "legacy.env"
    legacy.write_text(
        "SUBTRANS_WORKERS=3\nSUBTRANS_DL_QUALITY=720p\n"
        "SUBTRANS_DEEPSEEK_API_KEY=do-not-print\n"
    )
    memory_config_provider.mutate(
        lambda document: document["business"].update({"download_quality": "1080p"})
    )

    applied, found = import_legacy_env(legacy)
    snapshot = memory_config_provider.read()
    assert (applied, found) == (1, 2)
    assert snapshot.values["business"]["pipeline_workers"] == 3
    assert snapshot.values["business"]["download_quality"] == "1080p"
    assert snapshot.values["engines"]["byId"]["deepseek"]["api_key"] == "do-not-print"


def test_legacy_sources_import_models_engines_and_retention_idempotently(
    tmp_path, memory_config_provider,
):
    from src.config.migrate_legacy import import_legacy_sources

    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE model_settings (kind TEXT PRIMARY KEY, config_json TEXT NOT NULL, updated_at INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO model_settings VALUES (?, ?, ?)",
            ("llm", json.dumps({
                "model": "legacy-llm", "apiKey": "secret-value",
                "models": [{"id": "card-1", "isCurrent": True, "model": "legacy-llm"}],
            }), 100),
        )
        connection.execute(
            """CREATE TABLE translation_engines (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, api_type TEXT NOT NULL,
                base_url TEXT NOT NULL, model TEXT NOT NULL, api_key TEXT,
                enabled INTEGER NOT NULL, availability TEXT NOT NULL,
                last_checked_at INTEGER, last_error TEXT, api_key_rotated_at INTEGER,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            )"""
        )
        connection.execute(
            """INSERT INTO translation_engines VALUES
                ('legacy-id', 'Legacy', 'openai_compatible', 'https://example.test',
                 'model-a', 'engine-secret', 1, 'UNKNOWN', NULL, NULL, NULL, 10, 11)"""
        )

    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(json.dumps({"pipeline_workers": 4}))
    retention_path = tmp_path / "retention.json"
    retention_path.write_text(json.dumps({"days": 21, "lastRunAt": 1234}))
    memory_config_provider.mutate(
        lambda document: document["engines"]["byId"].update({"legacy-id": {
            "id": "legacy-id", "name": "Already present",
            "api_type": "openai_compatible", "base_url": "https://current.test",
            "model": "current", "api_key": None, "enabled": 1,
            "availability": "UNKNOWN", "last_checked_at": None,
            "last_error": None, "api_key_rotated_at": None,
            "created_at": 1, "updated_at": 1,
        }})
    )

    result = import_legacy_sources(
        db_path=db_path, runtime_file=runtime_path, retention_file=retention_path,
        provider=memory_config_provider, merge_defaults=True,
    )
    snapshot = memory_config_provider.read()
    assert result["models"] == 1
    assert result["engines"] == 0
    assert snapshot.values["models"]["llm"]["apiKey"] == "secret-value"
    assert snapshot.values["models"]["llm"]["models"][0]["apiKey"] == "secret-value"
    assert snapshot.values["engines"]["byId"]["legacy-id"]["model"] == "current"
    assert snapshot.values["business"]["retention_days"] == 21
    assert snapshot.values["business"]["retention_last_run_at"] == 1234

    repeated = import_legacy_sources(
        db_path=db_path, runtime_file=runtime_path, retention_file=retention_path,
        provider=memory_config_provider, merge_defaults=True,
    )
    assert repeated["models"] == 0
    assert repeated["engines"] == 0


class _MigrationRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def publish(self, *_args):
        return 1

    def pipeline(self):
        return _MigrationPipeline(self)


class _MigrationPipeline:
    def __init__(self, client):
        self.client = client
        self.pending = None

    def watch(self, _key):
        pass

    def get(self, key):
        return self.client.get(key)

    def multi(self):
        pass

    def set(self, key, value):
        self.pending = (key, value)

    def execute(self):
        self.client.set(*self.pending)

    def reset(self):
        self.pending = None


def test_legacy_import_initializes_empty_redis_and_is_idempotent(tmp_path):
    from src.config.config_repository import RedisConfigProvider
    from src.config.migrate_legacy import import_legacy_sources
    from src.config.redis_store import RedisJsonConfigStore

    redis_client = _MigrationRedis()
    provider = RedisConfigProvider(
        RedisJsonConfigStore(redis_client, key_prefix="migration-test")
    )
    legacy_env = tmp_path / "legacy.env"
    legacy_env.write_text("SUBTRANS_WORKERS=6\n")

    first = import_legacy_sources(env_file=legacy_env, provider=provider)
    assert first["business"] == 1
    assert provider.read().values["business"]["pipeline_workers"] == 6
    assert provider.read().updated_by == "api"

    second = import_legacy_sources(env_file=legacy_env, provider=provider)
    assert second["business"] == 0


def test_merge_defaults_is_explicit_and_source_marker_prevents_reimport(tmp_path):
    from src.config.config_repository import RedisConfigProvider
    from src.config.migrate_legacy import import_legacy_sources
    from src.config.redis_store import RedisJsonConfigStore

    provider = RedisConfigProvider(
        RedisJsonConfigStore(_MigrationRedis(), key_prefix="initialized-migration")
    )
    provider.initialize()
    legacy_env = tmp_path / "legacy.env"
    legacy_env.write_text("SUBTRANS_WORKERS=6\n")

    preserved = import_legacy_sources(env_file=legacy_env, provider=provider)
    assert preserved["business"] == 0
    assert provider.read().values["business"]["pipeline_workers"] == 8

    # Once the source is marked, a later run cannot overwrite values changed in Redis.
    merged_provider = RedisConfigProvider(
        RedisJsonConfigStore(_MigrationRedis(), key_prefix="initialized-migration-merge")
    )
    merged_provider.initialize()
    imported = import_legacy_sources(
        env_file=legacy_env, provider=merged_provider, merge_defaults=True,
    )
    assert imported["business"] == 1
    merged_provider.mutate(
        lambda document: document["business"].update({"pipeline_workers": 8})
    )
    repeated = import_legacy_sources(
        env_file=legacy_env, provider=merged_provider, merge_defaults=True,
    )
    assert repeated["business"] == 0
    assert merged_provider.read().values["business"]["pipeline_workers"] == 8


def test_legacy_source_precedence_audio_then_runtime_then_env(tmp_path):
    from src.config.config_repository import RedisConfigProvider
    from src.config.migrate_legacy import import_legacy_sources
    from src.config.redis_store import RedisJsonConfigStore

    redis_client = _MigrationRedis()
    store = RedisJsonConfigStore(redis_client, key_prefix="migration-priority")
    provider = RedisConfigProvider(store)
    provider.initialize()
    store.set("runtime:audio", {"pipeline_workers": 3})
    legacy_env = tmp_path / "legacy.env"
    legacy_env.write_text("SUBTRANS_WORKERS=1\n")
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text(json.dumps({"business": {"pipeline_workers": 2}}))

    result = import_legacy_sources(
        env_file=legacy_env,
        runtime_file=runtime_file,
        provider=provider,
        merge_defaults=True,
    )

    assert result["business"] == 1
    assert provider.read().values["business"]["pipeline_workers"] == 3

    redis_wins = RedisConfigProvider(RedisJsonConfigStore(
        _MigrationRedis(), key_prefix="migration-priority-redis"
    ))
    redis_wins.initialize()
    redis_wins.mutate(
        lambda document: document["business"].update({"pipeline_workers": 7})
    )
    import_legacy_sources(
        env_file=legacy_env,
        runtime_file=runtime_file,
        provider=redis_wins,
        merge_defaults=True,
    )
    assert redis_wins.read().values["business"]["pipeline_workers"] == 7


def test_missing_explicit_source_aborts_before_redis_initialization(tmp_path):
    from src.config.migrate_legacy import import_legacy_sources

    class TrackedProvider(MemoryConfigProvider):
        initialized = False

        def initialize(self):
            self.initialized = True
            return self.read()

    provider = TrackedProvider()
    with pytest.raises(FileNotFoundError, match="legacy runtime file"):
        import_legacy_sources(runtime_file=tmp_path / "missing.json", provider=provider)
    assert provider.initialized is False
