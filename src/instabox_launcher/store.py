"""Durable reservations and recovery state for a single launcher process."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


class ReservationDenied(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self) -> None:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.handle = os.fdopen(descriptor, "a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("Another launcher process owns this database; use one worker") from exc

    def release(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class TrialStore:
    def __init__(self, path: Path, launcher_id: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS launcher_identity (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS trials (
                    id TEXT PRIMARY KEY,
                    owner_key TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    source_revision TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    create_key TEXT NOT NULL UNIQUE,
                    delete_key TEXT NOT NULL UNIQUE,
                    phase TEXT NOT NULL DEFAULT 'creating',
                    outcome TEXT,
                    holds_capacity INTEGER NOT NULL DEFAULT 1,
                    create_attempted INTEGER NOT NULL DEFAULT 0,
                    create_uncertain INTEGER NOT NULL DEFAULT 0,
                    operation_id TEXT,
                    machine_id TEXT,
                    delete_operation_id TEXT,
                    port_url TEXT,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS trials_owner ON trials(owner_key, created_at DESC);
                CREATE INDEX IF NOT EXISTS trials_capacity ON trials(holds_capacity, next_attempt);
                CREATE UNIQUE INDEX IF NOT EXISTS trials_one_owner
                    ON trials(owner_key) WHERE holds_capacity = 1;
            """)
            identity = conn.execute("SELECT id FROM launcher_identity").fetchone()
            if identity and identity["id"] != launcher_id:
                raise RuntimeError("Launcher origin/secret changed; retain them while recovering this database")
            conn.execute("INSERT OR IGNORE INTO launcher_identity(id) VALUES (?)", (launcher_id,))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            with conn:
                yield conn
        finally:
            conn.close()

    def current(self, owner: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trials WHERE owner_key = ? "
                "ORDER BY holds_capacity DESC, created_at DESC, rowid DESC LIMIT 1", (owner,),
            ).fetchone()
            return dict(row) if row else None

    def get(self, trial_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM trials WHERE id = ?", (trial_id,)).fetchone()
            return dict(row) if row else None

    def reserve(self, owner: str, *, now: float, ttl: int, max_active: int,
                hourly: int, daily: int, revision: str,
                payload_factory: Callable[[str], dict]) -> dict:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM trials WHERE owner_key = ? AND holds_capacity = 1", (owner,),
            ).fetchone()
            if existing:
                return dict(existing)
            active = conn.execute("SELECT count(*) FROM trials WHERE holds_capacity = 1").fetchone()[0]
            if active >= max_active:
                raise ReservationDenied("capacity")
            for duration, budget in ((3600, hourly), (86400, daily)):
                count = conn.execute("SELECT count(*) FROM trials WHERE created_at > ?", (now - duration,)).fetchone()[0]
                if count >= budget:
                    raise ReservationDenied("budget")
            trial_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO trials(id, owner_key, created_at, expires_at, source_revision, "
                "payload, create_key, delete_key, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trial_id, owner, now, now + ttl, revision,
                 json.dumps(payload_factory(trial_id), sort_keys=True, separators=(",", ":")),
                 f"trial-create-{trial_id}", f"trial-delete-{trial_id}", now),
            )
            return dict(conn.execute("SELECT * FROM trials WHERE id = ?", (trial_id,)).fetchone())

    def update(self, trial_id: str, *, now: float, **values) -> dict:
        allowed = {"phase", "outcome", "holds_capacity", "create_attempted", "create_uncertain",
                   "operation_id", "machine_id", "delete_operation_id", "delete_key", "port_url", "next_attempt"}
        if not values or not values.keys() <= allowed:
            raise ValueError("Invalid trial state update")
        values["updated_at"] = now
        with self._connect() as conn:
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(f"UPDATE trials SET {assignments} WHERE id = ?", (*values.values(), trial_id))
            return dict(conn.execute("SELECT * FROM trials WHERE id = ?", (trial_id,)).fetchone())

    def finish(self, trial_id: str, outcome: str, now: float) -> dict:
        return self.update(trial_id, now=now, phase=outcome, outcome=outcome,
                           holds_capacity=0, port_url=None, next_attempt=0)

    def request_cleanup(self, trial_id: str, outcome: str, now: float) -> dict:
        with self._connect() as conn:
            conn.execute(
                "UPDATE trials SET phase='deleting', outcome=?, port_url=NULL, next_attempt=0, updated_at=? "
                "WHERE id=? AND holds_capacity=1", (outcome, now, trial_id),
            )
            return dict(conn.execute("SELECT * FROM trials WHERE id=?", (trial_id,)).fetchone())

    def due(self, now: float) -> list[dict]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM trials WHERE holds_capacity=1 AND "
                "((phase != 'ready' AND next_attempt <= ?) OR (phase='ready' AND expires_at <= ?))",
                (now, now),
            ).fetchall()]
