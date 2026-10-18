"""Redis-only repository for the aggregate shared configuration document."""
from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any, Protocol

from .bootstrap import load_bootstrap
from .redis_store import ConfigSnapshot, RedisJsonConfigStore, RevisionConflict, ConfigUnavailable
from .schema import BUSINESS_DEFAULTS, SECRET_KEYS, default_document, validate_business_value
from .model_schema import validate_engines, validate_models
from .plugin_schema import validate_plugin_section


class ConfigProvider(Protocol):
    def read(self) -> ConfigSnapshot: ...
    def mutate(
        self,
        callback: Callable[[dict[str, Any]], dict[str, Any] | None],
        expected_revision: int | None = None,
    ) -> ConfigSnapshot: ...


class RedisConfigProvider:
    def __init__(self, store: RedisJsonConfigStore):
        self.store = store
        self._lock = threading.RLock()
        self._cache: ConfigSnapshot | None = None
        self._cache_expires = 0.0
        self._subscriber = None

    def initialize(self) -> ConfigSnapshot:
        """Initialize defaults once, only when application startup authorizes it."""
        current = self.store.get("runtime")
        if current is not None:
            _ensure_valid_document(current.values)
            marker_key = self.store._key("initialized")
            self.store.client.set(marker_key, "1", nx=True)
            self._cache_set(current)
            return current
        marker_key = self.store._key("initialized")
        if self.store.client.get(marker_key):
            # A competing initializer may have committed the document after
            # our first GET but before the initialization marker was observed.
            current = self.store.get("runtime")
            if current is None:
                raise ConfigUnavailable("Redis configuration key is missing after initialization")
            _ensure_valid_document(current.values)
            self._cache_set(current)
            return current
        try:
            created = self.store.set(
                "runtime", default_document(), expected_revision=0,
                updated_by="bootstrap",
            )
        except RevisionConflict:
            # Another application process may initialize the same empty
            # namespace between our GET and WATCH/EXEC. Adopt its winner.
            created = self.store.get("runtime")
        if created is None:
            raise ConfigUnavailable("Redis configuration initialization failed")
        _ensure_valid_document(created.values)
        self.store.client.set(marker_key, "1", nx=True)
        self._cache_set(created)
        return created

    def _cache_set(self, snapshot: ConfigSnapshot) -> None:
        if self._cache is not None and snapshot.revision < self._cache.revision:
            return
        self._cache = _copy_snapshot(snapshot)
        self._cache_expires = time.monotonic() + 1.0

    def invalidate(self, key: str, revision: int) -> None:
        with self._lock:
            if key == "runtime" and (self._cache is None or revision > self._cache.revision):
                self._cache = None
                self._cache_expires = 0.0

    def read(self) -> ConfigSnapshot:
        with self._lock:
            if self._cache is not None and time.monotonic() < self._cache_expires:
                return _copy_snapshot(self._cache)
            snapshot = self.store.get("runtime")
            if snapshot is None:
                raise ConfigUnavailable("Redis configuration document is missing; initialize explicitly")
            _ensure_valid_document(snapshot.values)
            self._cache_set(snapshot)
            return _copy_snapshot(self._cache)

    def mutate(
        self,
        callback: Callable[[dict[str, Any]], dict[str, Any] | None],
        expected_revision: int | None = None,
    ) -> ConfigSnapshot:
        attempts = 1 if expected_revision is not None else 3
        for _ in range(attempts):
            with self._lock:
                current = self.read()
                if expected_revision is not None and expected_revision != current.revision:
                    raise RevisionConflict(
                        f"expected revision {expected_revision}, found {current.revision}"
                    )
                values = copy.deepcopy(current.values)
                changed = callback(values)
                if changed is not None:
                    values = changed
                _validate_document(values)
                try:
                    result = self.store.set(
                        "runtime", values, expected_revision=current.revision,
                        updated_by="api",
                    )
                except RevisionConflict:
                    self._cache = None
                    self._cache_expires = 0.0
                    if expected_revision is not None:
                        raise
                    continue
                if result is None:
                    raise ConfigUnavailable("Redis configuration write failed")
                self._cache_set(result)
                return _copy_snapshot(result)
        raise RevisionConflict("configuration changed repeatedly during update")


class MemoryConfigProvider:
    """Explicit test provider; never selected by production bootstrap."""
    def __init__(self, values: dict[str, Any] | None = None):
        self._values = copy.deepcopy(values or default_document())
        self._revision = 1
        self._updated_at = int(datetime.now(timezone.utc).timestamp() * 1000)
        self._lock = threading.RLock()

    def read(self) -> ConfigSnapshot:
        with self._lock:
            return ConfigSnapshot(1, self._revision, copy.deepcopy(self._values), self._updated_at)

    def mutate(self, callback, expected_revision: int | None = None) -> ConfigSnapshot:
        with self._lock:
            if expected_revision is not None and expected_revision != self._revision:
                raise RevisionConflict("configuration revision changed")
            values = copy.deepcopy(self._values)
            changed = callback(values)
            if changed is not None:
                values = changed
            _validate_document(values)
            self._values = values
            self._revision += 1
            self._updated_at = int(datetime.now(timezone.utc).timestamp() * 1000)
            return self.read()

    def initialize(self) -> ConfigSnapshot:
        return self.read()


_provider: ConfigProvider | None = None
_provider_lock = threading.RLock()


def get_config_provider() -> ConfigProvider:
    global _provider
    with _provider_lock:
        if _provider is None:
            bootstrap = load_bootstrap()
            redis = bootstrap["redis"]
            namespace = str(redis.get("namespace") or "translatedsubs-local").strip()
            url = str(redis.get("url") or "redis://localhost:6379/0")
            store = RedisJsonConfigStore(
                url=url,
                key_prefix=f"subtrans:{namespace}:config",
                channel=f"subtrans:{namespace}:config:invalidate",
            )
            _provider = RedisConfigProvider(store)
        return _provider


def set_config_provider(provider: ConfigProvider | None) -> None:
    """Inject an explicit provider (normally only for tests)."""
    global _provider
    with _provider_lock:
        _provider = provider


def start_config_subscription() -> bool:
    provider = get_config_provider()
    if not isinstance(provider, RedisConfigProvider):
        return False
    if getattr(provider, "_subscriber", None) is None:
        from .redis_store import RedisInvalidationSubscriber
        provider._subscriber = RedisInvalidationSubscriber(provider.store, provider.invalidate)
    return provider._subscriber.start()


def stop_config_subscription() -> None:
    provider = _provider
    if isinstance(provider, RedisConfigProvider):
        subscriber = getattr(provider, "_subscriber", None)
        if subscriber is not None:
            subscriber.stop()
            provider._subscriber = None


def read_config_snapshot() -> ConfigSnapshot:
    return get_config_provider().read()


def initialize_config_document() -> ConfigSnapshot:
    provider = get_config_provider()
    initializer = getattr(provider, "initialize", None)
    return initializer() if initializer else provider.read()


def _validate_document(values: Any) -> None:
    if not isinstance(values, dict):
        raise ValueError("configuration document must be an object")
    if not isinstance(values.get("business"), dict):
        raise ValueError("configuration business section must be an object")
    if not isinstance(values.get("models"), dict):
        raise ValueError("configuration models section must be an object")
    engines = values.get("engines")
    if not isinstance(engines, dict) or not isinstance(engines.get("byId"), dict):
        raise ValueError("configuration engines.byId section must be an object")
    validate_models(values["models"])
    validate_engines(engines["byId"])
    if "plugins" in values:
        validate_plugin_section(values["plugins"], document=values)
    meta = values.get("meta", {})
    if not isinstance(meta, dict):
        raise ValueError("configuration meta section must be an object")
    imported_sources = meta.get("legacy_sources_imported", [])
    if not isinstance(imported_sources, list) or not all(
        isinstance(source, str) for source in imported_sources
    ):
        raise ValueError("configuration migration marker is invalid")
    allowed = set(BUSINESS_DEFAULTS) | set(SECRET_KEYS) | {"retention_last_run_at"}
    for key, value in values["business"].items():
        if key == "retention_last_run_at":
            if value is not None and not isinstance(value, (int, float, str)):
                raise ValueError("retention_last_run_at has an invalid value")
            continue
        if key not in allowed:
            raise ValueError(f"unsupported business setting: {key}")
        validate_business_value(key, value)


def _ensure_valid_document(values: Any) -> None:
    try:
        _validate_document(values)
    except (TypeError, ValueError, KeyError) as exc:
        # Do not leak potentially sensitive JSON fragments in API diagnostics.
        raise ConfigUnavailable("Redis configuration document is invalid") from exc


def _copy_snapshot(snapshot: ConfigSnapshot) -> ConfigSnapshot:
    return ConfigSnapshot(
        schema=snapshot.schema,
        revision=snapshot.revision,
        values=copy.deepcopy(snapshot.values),
        updated_at=snapshot.updated_at,
        updated_by=snapshot.updated_by,
    )
