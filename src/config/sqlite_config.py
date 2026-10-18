"""Durable, credential-free aggregate configuration for the desktop runtime."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .redis_store import ConfigSnapshot, ConfigUnavailable, RevisionConflict, SCHEMA_VERSION
from .schema import SECRET_KEYS, default_document


def _desktop_tool_paths() -> dict[str, str]:
    """Read only launcher-controlled paths; never copy arbitrary environment."""
    paths = {}
    for setting, variable in (
        ("ffmpeg_bin", "SUBTRANS_DESKTOP_FFMPEG"),
        ("ffprobe_bin", "SUBTRANS_DESKTOP_FFPROBE"),
    ):
        value = os.environ.get(variable)
        if value:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError(f"{variable} must be an absolute path")
            paths[setting] = str(path.resolve())
    return paths


def desktop_default_document() -> dict[str, Any]:
    """Seed desktop choices without importing server configuration or secrets."""
    document = default_document()
    business = document["business"]
    business.update({
        "pipeline_workers": 1,
        "startup_probe_enabled": False,
        "local_whisper_model": "base.en",
        "local_whisper_device": "cpu",
        "local_whisper_compute_type": "int8",
        "transcriber_backend": "local_whisper",
        "download_quality": "1080p",
    })
    business.update(_desktop_tool_paths())
    document["models"] = {
        "llm": {"enabled": False, "models": []},
        "whisper": {
            "enabled": True, "provider": "local", "model": "local:base.en",
            "language": "en", "device": "cpu", "threads": 4,
            "wordTimestamps": True, "models": [],
        },
        "tts": {"enabled": False, "models": []},
    }
    return document


def _reject_credentials(value: Any) -> None:
    # Existing model/engine schemas permit API keys. Desktop v1 deliberately
    # rejects populated secret fields rather than silently discarding a write.
    secret_names = {key.replace("_", "").lower() for key in SECRET_KEYS} | {
        "apikey", "token", "sessiontoken", "subtransdesktopsessiontoken",
        "password", "secret", "authorization", "cookie", "cookies",
        "accesstoken", "refreshtoken", "clientsecret",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).replace("_", "").replace("-", "").lower()
            if normalized in secret_names and item not in (None, ""):
                raise ValueError("desktop configuration cannot persist credentials")
            _reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _reject_credentials(item)
    elif isinstance(value, str) and "://" in value:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("desktop configuration cannot persist credential-bearing URLs")
        for key, item in parse_qsl(parsed.query, keep_blank_values=True):
            _reject_credentials({key: item})


def _validate(values: Any) -> None:
    # Lazy import avoids a cycle with the provider factory while reusing exactly
    # the server's business, model, and engine validation rules.
    from .config_repository import _validate_document

    _reject_credentials(values)
    _validate_document(values)


class SQLiteConfigProvider:
    """One aggregate row; each operation owns its connection, never a cache.

    BEGIN IMMEDIATE serializes read/modify/write across threads and processes.
    Callbacks execute once under the write transaction and must remain short;
    they must not recursively mutate this provider or perform external I/O.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        initial_document: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self._initial_document = copy.deepcopy(
            initial_document if initial_document is not None else desktop_default_document()
        )
        self._timeout = timeout

    @contextmanager
    def _connect(self, *, initialize: bool = False) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            if initialize:
                self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
            connection = sqlite3.connect(
                self.db_path.as_uri() + "?mode=rw", uri=True,
                timeout=self._timeout, isolation_level=None,
            )
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        except (OSError, sqlite3.Error) as exc:
            raise ConfigUnavailable("SQLite configuration storage is unavailable") from exc
        finally:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.rollback()
                finally:
                    connection.close()

    def _read(self, connection: sqlite3.Connection) -> ConfigSnapshot:
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ConfigUnavailable("SQLite configuration schema is not initialized or unsupported")
        row = connection.execute(
            "SELECT revision, document, updated_at, updated_by FROM runtime_config WHERE id=1"
        ).fetchone()
        if row is None:
            raise ConfigUnavailable("SQLite configuration document is missing")
        try:
            revision, encoded, updated_at, updated_by = row
            if not isinstance(revision, int) or revision < 1:
                raise ValueError("invalid revision")
            values = json.loads(encoded)
            _validate(values)
        except (TypeError, ValueError, KeyError) as exc:
            raise ConfigUnavailable("SQLite configuration document is invalid") from exc
        return ConfigSnapshot(SCHEMA_VERSION, revision, values, updated_at, updated_by)

    def initialize(self) -> ConfigSnapshot:
        """Seed once and refresh relocated bundle tools without resetting choices."""
        tool_paths = _desktop_tool_paths()
        with self._connect(initialize=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                values = copy.deepcopy(self._initial_document)
                _validate(values)
                values["business"].update(tool_paths)
                encoded = json.dumps(values, ensure_ascii=False, allow_nan=False)
                connection.execute(
                    "CREATE TABLE runtime_config (id INTEGER PRIMARY KEY CHECK (id=1), "
                    "revision INTEGER NOT NULL CHECK (revision>=1), document TEXT NOT NULL, "
                    "updated_at INTEGER NOT NULL, updated_by TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO runtime_config VALUES (1, 1, ?, ?, 'bootstrap')",
                    (encoded, int(time.time() * 1000)),
                )
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            snapshot = self._read(connection)
            # The bundle can move between launches. These two machine-local
            # fields follow the launcher, while all user choices stay intact.
            # Serialize with API writes and advance CAS only on an actual change.
            if any(snapshot.values["business"].get(key) != value
                   for key, value in tool_paths.items()):
                values = snapshot.values
                values["business"].update(tool_paths)
                _validate(values)
                connection.execute(
                    "UPDATE runtime_config SET revision=?, document=?, updated_at=?, "
                    "updated_by='bootstrap' WHERE id=1",
                    (snapshot.revision + 1,
                     json.dumps(values, ensure_ascii=False, allow_nan=False),
                     int(time.time() * 1000)),
                )
                snapshot = self._read(connection)
            connection.commit()
            return snapshot

    def read(self) -> ConfigSnapshot:
        with self._connect() as connection:
            connection.execute("BEGIN")
            return self._read(connection)

    def mutate(
        self,
        callback: Callable[[dict[str, Any]], dict[str, Any] | None],
        expected_revision: int | None = None,
    ) -> ConfigSnapshot:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._read(connection)
            if expected_revision is not None and expected_revision != current.revision:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, found {current.revision}"
                )
            values = current.values
            changed = callback(values)
            if changed is not None:
                values = changed
            _validate(values)
            encoded = json.dumps(values, ensure_ascii=False, allow_nan=False)
            updated_at = int(time.time() * 1000)
            connection.execute(
                "UPDATE runtime_config SET revision=?, document=?, updated_at=?, "
                "updated_by='api' WHERE id=1",
                (current.revision + 1, encoded, updated_at),
            )
            snapshot = self._read(connection)
            connection.commit()
            return snapshot
