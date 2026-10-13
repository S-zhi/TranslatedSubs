from src.config import config
import pytest

from src.config.redis_store import ConfigUnavailable, RedisJsonConfigStore
from src.config import runtime_redis


@pytest.fixture(autouse=True)
def reset_runtime_state():
    runtime_redis.reset_runtime_store()
    config._RUNTIME_SETTINGS.clear()
    config._RUNTIME_SETTINGS_LOADED = False
    config._RUNTIME_SETTINGS_SOURCE = None
    yield
    runtime_redis.reset_runtime_store()


class FakeRedis:
    def __init__(self):
        self.values = {}
    def get(self, key): return self.values.get(key)
    def set(self, key, value): self.values[key] = value
    def publish(self, channel, value): return 1
    def pipeline(self): return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.pending = None
    def watch(self, _key): return None
    def get(self, key): return self.redis.get(key)
    def multi(self): return None
    def set(self, key, value): self.pending = (key, value)
    def execute(self):
        if self.pending:
            self.redis.set(*self.pending)
        return [True]
    def reset(self): self.pending = None


def test_runtime_redis_primary_and_local_fallback(tmp_path, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setenv("SUBTRANS_REDIS_URL", "redis://fake/0")
    monkeypatch.setattr(
        runtime_redis,
        "_store",
        RedisJsonConfigStore(
            fake,
            key_prefix="subtrans:isolated:config",
            channel="subtrans:isolated:config:invalidate",
        ),
    )
    monkeypatch.setenv("SUBTRANS_RUNTIME_SETTINGS_FILE", str(tmp_path / "runtime.json"))
    config._load_runtime_settings()
    config.update_runtime_settings({"vocal_separation_threads": 4})
    assert "subtrans:isolated:config:runtime:audio" in fake.values
    assert config._runtime_value("vocal_separation_threads") == 4


def test_pubsub_invalidation_clears_process_cache(tmp_path, monkeypatch):
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text('{"vocal_separation_threads":4}', encoding="utf-8")
    monkeypatch.setenv("SUBTRANS_RUNTIME_SETTINGS_FILE", str(runtime_path))
    fake = FakeRedis()
    fake.values["subtrans:isolated:config:runtime:audio"] = '{"schema":1,"revision":1,"values":{"vocal_separation_threads":2}}'
    monkeypatch.setenv("SUBTRANS_REDIS_NAMESPACE", "isolated")
    monkeypatch.setattr(
        runtime_redis,
        "_store",
        RedisJsonConfigStore(
            fake,
            key_prefix="subtrans:isolated:config",
            channel="subtrans:isolated:config:invalidate",
        ),
    )
    runtime_redis._cache.clear()
    assert runtime_redis.get_runtime_value("vocal_separation_threads") == 2
    runtime_redis._invalidate("runtime:audio", 2)
    assert runtime_redis._cache == {}
    fake.values.clear()
    assert config._runtime_value("vocal_separation_threads") == 4


def test_configured_redis_failure_does_not_write_local_override(monkeypatch):
    monkeypatch.setenv("SUBTRANS_REDIS_URL", "redis://unavailable.invalid/0")
    monkeypatch.setattr(runtime_redis, "_store", RedisJsonConfigStore())

    with pytest.raises(ConfigUnavailable):
        runtime_redis.update_runtime_values({"vocal_separation_threads": 4})


def test_config_update_rejects_redis_failure_before_local_write(tmp_path, monkeypatch):
    runtime_path = tmp_path / "runtime.json"
    monkeypatch.setenv("SUBTRANS_REDIS_URL", "redis://unavailable.invalid/0")
    monkeypatch.setenv("SUBTRANS_RUNTIME_SETTINGS_FILE", str(runtime_path))
    monkeypatch.setattr(runtime_redis, "_store", RedisJsonConfigStore())

    with pytest.raises(ConfigUnavailable):
        config.update_runtime_settings({"vocal_separation_threads": 4})
    assert not runtime_path.exists()
