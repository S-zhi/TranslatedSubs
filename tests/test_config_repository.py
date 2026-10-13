from __future__ import annotations

import json
import threading
import time

import pytest

from src.config.config_repository import RedisConfigProvider, _validate_document
from src.config.redis_store import RedisJsonConfigStore
from src.config.schema import default_document


class WatchError(Exception):
    pass


class AtomicFakeRedis:
    def __init__(self):
        self.values = {}
        self.lock = threading.RLock()
        self.initial_reads = 0
        self.initial_reads_lock = threading.Lock()
        self.initial_reads_barrier = threading.Barrier(2)

    def get(self, key):
        with self.initial_reads_lock:
            if key.endswith(":runtime") and self.initial_reads < 2:
                self.initial_reads += 1
                should_wait = True
            else:
                should_wait = False
        if should_wait:
            self.initial_reads_barrier.wait(timeout=2)
        with self.lock:
            return self.values.get(key)

    def set(self, key, value, nx=False):
        with self.lock:
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

    def publish(self, *_args):
        return 1

    def pipeline(self):
        return AtomicFakePipeline(self)


class AtomicFakePipeline:
    def __init__(self, client):
        self.client = client
        self.watched_key = None
        self.watched_value = None
        self.pending = None

    def watch(self, key):
        self.watched_key = key
        self.watched_value = self.client.get(key)

    def get(self, key):
        return self.client.get(key)

    def multi(self):
        pass

    def set(self, key, value):
        self.pending = (key, value)

    def execute(self):
        with self.client.lock:
            if self.client.values.get(self.watched_key) != self.watched_value:
                raise WatchError("key changed")
            self.client.values[self.pending[0]] = self.pending[1]

    def reset(self):
        self.pending = None


def test_two_providers_can_initialize_empty_redis_concurrently():
    redis = AtomicFakeRedis()
    providers = [
        RedisConfigProvider(RedisJsonConfigStore(redis, key_prefix="init-race"))
        for _ in range(2)
    ]
    results = []
    errors = []

    def initialize(provider):
        try:
            results.append(provider.initialize())
        except Exception as exc:  # surfaced below with the exact cause
            errors.append(exc)

    threads = [threading.Thread(target=initialize, args=(provider,)) for provider in providers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not errors
    assert len(results) == 2
    assert results[0].revision == results[1].revision == 1
    assert providers[0].read().values == providers[1].read().values


def test_cache_refresh_does_not_replace_newer_snapshot_with_inflight_read():
    redis = AtomicFakeRedis()
    # Do not use the initialization barrier in this deterministic read/write race.
    redis.initial_reads = 2
    provider = RedisConfigProvider(RedisJsonConfigStore(redis, key_prefix="read-race"))
    provider.initialize()
    provider._cache_expires = 0
    fetched = threading.Event()
    release = threading.Event()
    original_get = provider.store.get
    first = True

    def delayed_get(key):
        nonlocal first
        result = original_get(key)
        if first:
            first = False
            fetched.set()
            release.wait(timeout=2)
        return result

    provider.store.get = delayed_get
    reader = threading.Thread(target=provider.read)
    reader.start()
    assert fetched.wait(timeout=2)
    writer = threading.Thread(
        target=lambda: provider.mutate(
            lambda document: document["business"].update({"pipeline_workers": 3})
        )
    )
    writer.start()
    time.sleep(0.02)
    release.set()
    reader.join(timeout=3)
    writer.join(timeout=3)

    assert provider.read().values["business"]["pipeline_workers"] == 3


def test_persisted_business_documents_reject_null_for_required_settings():
    document = default_document()
    document["business"]["download_workers"] = None
    with pytest.raises(ValueError, match="may not be null"):
        _validate_document(document)
