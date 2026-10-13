from __future__ import annotations

import pytest

from src.config.config import Settings, settings_scope, update_runtime_settings
from src.config.config_repository import MemoryConfigProvider, get_config_provider, set_config_provider
from src.config.redis_store import ConfigSnapshot, ConfigUnavailable, RevisionConflict
from src.config.schema import default_document


def test_settings_scope_uses_one_business_snapshot(memory_config_provider):
    values = default_document()
    values["business"]["pipeline_workers"] = 3
    values["business"]["transcriber_api_key"] = "private"
    set_config_provider(MemoryConfigProvider(values))
    settings = Settings()

    captured = settings.capture_business_snapshot()
    assert captured["pipeline_workers"] == 3
    assert "transcriber_api_key" not in captured
    with settings_scope({**captured, "pipeline_workers": 7}):
        assert settings.pipeline_workers == 7
    assert settings.pipeline_workers == 3


def test_update_runtime_settings_mutates_business_domain(memory_config_provider):
    result = update_runtime_settings({"vocal_separation_threads": 4, "transcriber_api_key": "secret"})
    snapshot = get_config_provider().read()
    assert snapshot.values["business"]["vocal_separation_threads"] == 4
    assert snapshot.values["business"]["transcriber_api_key"] == "secret"
    assert result["vocal_separation_threads"] == 4
    assert "transcriber_api_key" not in result


def test_config_provider_conflict_and_unavailable_contract():
    provider = MemoryConfigProvider()
    initial = provider.read()
    provider.mutate(lambda doc: doc["business"].update({"debug": True}))
    with pytest.raises(RevisionConflict):
        provider.mutate(lambda doc: doc["business"].update({"debug": False}), expected_revision=initial.revision)

    class DownProvider:
        def read(self):
            raise ConfigUnavailable("down")
        def mutate(self, *_args, **_kwargs):
            raise ConfigUnavailable("down")
    set_config_provider(DownProvider())
    with pytest.raises(ConfigUnavailable):
        Settings().capture_business_snapshot()
