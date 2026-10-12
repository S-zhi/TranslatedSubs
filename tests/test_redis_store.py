from __future__ import annotations

import json

import pytest

from src.config.redis_store import (
    RedisInvalidationSubscriber,
    RedisJsonConfigStore,
    RevisionConflict,
)


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.published = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value
        return True

    def publish(self, channel, value):
        self.published.append((channel, value))
        return 1

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.pending = None

    def watch(self, _key):
        return None

    def get(self, key):
        return self.redis.get(key)

    def multi(self):
        return None

    def set(self, key, value):
        self.pending = (key, value)

    def execute(self):
        if self.pending:
            self.redis.set(*self.pending)
        return [True]

    def reset(self):
        self.pending = None


def test_json_roundtrip_revision_and_invalidation():
    redis = FakeRedis()
    store = RedisJsonConfigStore(redis, key_prefix="test")

    first = store.set("llm", {"model": "a"})
    assert first and first.schema == 1 and first.revision == 1
    assert store.get("llm").value == {"model": "a"}
    assert json.loads(redis.published[-1][1])["revision"] == 1

    second = store.set("llm", {"model": "b"}, expected_revision=1)
    assert second and second.revision == 2
    with pytest.raises(RevisionConflict):
        store.set("llm", {}, expected_revision=1)


def test_unavailable_redis_is_non_blocking():
    store = RedisJsonConfigStore()
    assert store.available is False
    assert store.get("llm") is None
    assert store.set("llm", {}) is None
    assert RedisInvalidationSubscriber(store, lambda *_: None).start() is False


def test_subscriber_decodes_invalidation_payload():
    seen = []
    subscriber = RedisInvalidationSubscriber(
        RedisJsonConfigStore(FakeRedis()),
        lambda key, rev: seen.append((key, rev)),
    )
    assert subscriber.handle_message({"data": json.dumps({"key": "tts", "revision": 4})}) is True
    assert seen == [("tts", 4)]
    assert subscriber.handle_message({"data": "not-json"}) is False
