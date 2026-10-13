"""Small Redis JSON configuration adapter and invalidation subscriber.

The adapter is deliberately independent from application settings.  A caller
can inject a redis-py compatible client (which also makes tests deterministic)
or provide a URL.  Redis failures are treated as an unavailable cache and do
not prevent the application from using its existing local store.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class RevisionConflict(RuntimeError):
    """Raised when a conditional write observes a newer revision."""


class ConfigUnavailable(RuntimeError):
    """Raised when Redis is configured but cannot persist configuration."""


@dataclass(frozen=True)
class ConfigSnapshot:
    schema: int
    revision: int
    values: Any
    updated_at: int
    updated_by: str | None = None

    @property
    def value(self) -> Any:
        """Compatibility alias for callers that use singular terminology."""
        return self.values


def _decode(raw: Any) -> ConfigSnapshot | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise ValueError("Redis config value must be a JSON object")
    if int(data.get("schema", 0)) != SCHEMA_VERSION:
        raise ValueError("unsupported Redis config schema")
    if int(data.get("revision", 0)) < 0:
        raise ValueError("invalid Redis config revision")
    return ConfigSnapshot(
        schema=int(data.get("schema", 0)),
        revision=int(data.get("revision", 0)),
        values=data.get("values", data.get("value")),
        updated_at=int(data.get("updatedAt", 0)),
        updated_by=data.get("updatedBy"),
    )


class RedisJsonConfigStore:
    """Store versioned JSON values in Redis strings.

    ``client`` is expected to implement ``get``, ``set`` and ``publish``.
    Redis-py clients additionally get an optimistic WATCH/MULTI CAS path.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        url: str | None = None,
        key_prefix: str = "subtrans:config",
        channel: str = "subtrans:config:invalidate",
    ) -> None:
        if client is None and url:
            try:
                import redis

                # Keep optional Redis strictly best-effort during startup and
                # request handling when the service is down.
                client = redis.Redis.from_url(
                    url,
                    decode_responses=True,
                    socket_connect_timeout=0.5,
                    socket_timeout=0.5,
                )
            except Exception as exc:  # pragma: no cover - depends on deployment
                logger.warning("Redis 配置存储不可用: %s", exc)
        self.client = client
        self.key_prefix = key_prefix.rstrip(":")
        self.channel = channel

    @property
    def available(self) -> bool:
        return self.client is not None

    def _key(self, key: str) -> str:
        return f"{self.key_prefix}:{key}"

    def get(self, key: str) -> ConfigSnapshot | None:
        if not self.available:
            raise ConfigUnavailable("Redis client is not configured")
        try:
            return _decode(self.client.get(self._key(key)))
        except Exception as exc:
            logger.warning("读取 Redis 配置失败 key=%s: %s", key, exc)
            raise ConfigUnavailable(str(exc)) from exc

    def set(
        self,
        key: str,
        value: Any,
        *,
        expected_revision: int | None = None,
        updated_by: str | None = None,
    ) -> ConfigSnapshot | None:
        """Write a value and publish invalidation after a successful write."""
        if not self.available:
            raise ConfigUnavailable("Redis client is not configured")
        redis_key = self._key(key)
        try:
            snapshot = self._set_cas(redis_key, value, expected_revision, updated_by)
            try:
                self.client.publish(
                    self.channel,
                    json.dumps(
                        {"key": key, "schema": snapshot.schema, "revision": snapshot.revision,
                         "updatedAt": snapshot.updated_at, "updatedBy": snapshot.updated_by},
                        separators=(",", ":"),
                    ),
                )
            except Exception as exc:
                logger.warning("发布 Redis 配置失效通知失败 key=%s: %s", key, exc)
            return snapshot
        except RevisionConflict:
            raise
        except Exception as exc:
            if exc.__class__.__name__ == "WatchError":
                raise RevisionConflict("配置版本冲突") from exc
            logger.warning("写入 Redis 配置失败 key=%s: %s", key, exc)
            raise ConfigUnavailable(str(exc)) from exc

    def _set_cas(
        self,
        redis_key: str,
        value: Any,
        expected_revision: int | None,
        updated_by: str | None,
    ) -> ConfigSnapshot:
        client = self.client
        old = _decode(client.get(redis_key))
        if expected_revision is not None and (old.revision if old else 0) != expected_revision:
            actual = old.revision if old else 0
            raise RevisionConflict(
                f"配置版本冲突: expected={expected_revision}, actual={actual}"
            )
        revision = (old.revision if old else 0) + 1
        snapshot = ConfigSnapshot(
            SCHEMA_VERSION, revision, value, int(time.time() * 1000), updated_by,
        )
        encoded = json.dumps({"schema": snapshot.schema, "revision": revision,
                              "updatedAt": snapshot.updated_at, "updatedBy": updated_by,
                              "values": value}, ensure_ascii=False)
        pipeline_factory = getattr(client, "pipeline", None)
        if pipeline_factory is not None:
            pipe = pipeline_factory()
            try:
                pipe.watch(redis_key)
                current = _decode(pipe.get(redis_key))
                if expected_revision is not None and (
                    current.revision if current else 0
                ) != expected_revision:
                    raise RevisionConflict("配置版本冲突")
                revision = (current.revision if current else 0) + 1
                snapshot = ConfigSnapshot(
                    SCHEMA_VERSION, revision, value, snapshot.updated_at, updated_by,
                )
                encoded = json.dumps({"schema": snapshot.schema, "revision": revision,
                                      "updatedAt": snapshot.updated_at, "updatedBy": updated_by,
                                      "values": value}, ensure_ascii=False)
                pipe.multi()
                pipe.set(redis_key, encoded)
                pipe.execute()
            finally:
                close = getattr(pipe, "reset", None)
                if close:
                    close()
        else:
            if expected_revision is not None:
                raise ConfigUnavailable("Redis client does not support atomic CAS")
            client.set(redis_key, encoded)
        return snapshot


class RedisInvalidationSubscriber:
    """Best-effort background Pub/Sub listener for cache invalidation."""

    def __init__(
        self,
        store: RedisJsonConfigStore,
        on_invalidate: Callable[[str, int], None],
    ) -> None:
        self.store = store
        self.on_invalidate = on_invalidate
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if not self.store.available or (self._thread and self._thread.is_alive()):
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="redis-config-sub", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    def _run(self) -> None:
        backoff = 0.25
        while not self._stop.is_set():
            pubsub = None
            try:
                pubsub = self.store.client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(self.store.channel)
                backoff = 0.25
                while not self._stop.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if message:
                        self.handle_message(message)
            except Exception as exc:
                if not self._stop.is_set():
                    logger.warning("Redis 配置订阅器连接失败，将在 %.2fs 后重试: %s", backoff, exc)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 5.0)
            finally:
                close = getattr(pubsub, "close", None)
                if close:
                    try:
                        close()
                    except Exception:
                        logger.debug("关闭 Redis 配置订阅器失败", exc_info=True)

    def handle_message(self, message: dict[str, Any]) -> bool:
        if not isinstance(message, dict):
            return False
        if message.get("type") not in (None, "message", "pmessage"):
            return False
        data = message.get("data") if isinstance(message, dict) else None
        try:
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            payload = json.loads(data) if isinstance(data, str) else data
            key, revision = payload["key"], int(payload["revision"])
            self.on_invalidate(str(key), revision)
            return True
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
