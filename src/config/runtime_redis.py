"""Redis bridge for the small runtime audio settings allow-list."""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from .redis_store import ConfigUnavailable, RedisJsonConfigStore, RevisionConflict

_KEY = "runtime:audio"
_store: RedisJsonConfigStore | None = None
_cache: dict[str, Any] = {}
_cache_expires = 0.0
_cache_loaded = False
_cache_revision = 0
_remote_seen = False
_cache_lock = threading.RLock()
_subscriber = None


def _get_store() -> RedisJsonConfigStore:
    global _store
    if _store is None:
        namespace = os.getenv("SUBTRANS_REDIS_NAMESPACE", "isolated").strip() or "isolated"
        _store = RedisJsonConfigStore(
            url=os.getenv("SUBTRANS_REDIS_URL"),
            key_prefix=f"subtrans:{namespace}:config",
            channel=f"subtrans:{namespace}:config:invalidate",
        )
    return _store


def get_runtime_value(key: str) -> Any:
    global _cache_expires, _cache_loaded, _cache_revision, _remote_seen
    with _cache_lock:
        if _cache_loaded and time.monotonic() < _cache_expires:
            return _cache.get(key)
    snapshot = _get_store().get(_KEY)
    if snapshot is None or not isinstance(snapshot.values, dict):
        with _cache_lock:
            if not _cache_loaded:
                _cache.clear()
                _cache_loaded = True
            _cache_expires = time.monotonic() + 15
            return _cache.get(key)
    with _cache_lock:
        if snapshot.revision < _cache_revision:
            return _cache.get(key)
        _remote_seen = True
        _cache.clear()
        _cache.update(snapshot.values)
        _cache_revision = snapshot.revision
        _cache_loaded = True
        _cache_expires = time.monotonic() + 15
        return _cache.get(key)


def redis_configured() -> bool:
    return bool(os.getenv("SUBTRANS_REDIS_URL"))


def get_runtime_document() -> tuple[bool, dict[str, Any] | None]:
    """Return configured flag and the latest remote document, if available."""
    if not redis_configured():
        return False, None
    get_runtime_value("__probe__")
    with _cache_lock:
        return True, dict(_cache) if _remote_seen else None


def update_runtime_values(values: dict[str, Any]) -> None:
    store = _get_store()
    if not store.available:
        if redis_configured():
            raise ConfigUnavailable("Redis 配置存储不可用")
        return
    current = store.get(_KEY)
    if current and isinstance(current.values, dict):
        merged = dict(current.values)
    else:
        from .config import _RUNTIME_SETTINGS, _load_runtime_settings
        _load_runtime_settings()
        merged = dict(_RUNTIME_SETTINGS)
    for key, value in values.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    try:
        result = store.set(_KEY, merged, expected_revision=current.revision if current else 0,
                           updated_by="runtime-settings")
    except RevisionConflict:
        result = None
    if result is None:
        current = store.get(_KEY)
        merged = dict(current.values) if current and isinstance(current.values, dict) else {}
        for key, value in values.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        result = store.set(_KEY, merged, expected_revision=current.revision if current else 0,
                           updated_by="runtime-settings")
    if result is not None:
        with _cache_lock:
            global _cache_loaded, _cache_expires, _cache_revision
            _cache.update(merged)
            _cache_loaded = True
            _cache_revision = result.revision
            _cache_expires = time.monotonic() + 15


def _invalidate(key: str, revision: int) -> None:
    if key == _KEY:
        with _cache_lock:
            global _cache_loaded, _cache_revision
            _cache.clear()
            _cache_loaded = False
            _cache_revision = max(_cache_revision, revision)


def start_runtime_subscription() -> bool:
    global _subscriber
    if _subscriber is None:
        from .redis_store import RedisInvalidationSubscriber
        _subscriber = RedisInvalidationSubscriber(_get_store(), _invalidate)
    return _subscriber.start()


def stop_runtime_subscription() -> None:
    global _subscriber
    if _subscriber is not None:
        _subscriber.stop()
    _subscriber = None


def reset_runtime_store() -> None:
    global _store, _remote_seen, _cache_loaded, _cache_revision
    stop_runtime_subscription()
    _store = None
    with _cache_lock:
        _cache.clear()
        _cache_loaded = False
        _cache_revision = 0
        _remote_seen = False
