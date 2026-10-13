"""Execution context supplied to each plugin session."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from src.plugin_system.errors import PluginCancelled


@runtime_checkable
class PluginContext(Protocol):
    @property
    def task_id(self) -> str: ...

    @property
    def invocation_id(self) -> str: ...

    @property
    def task_dir(self) -> Path: ...

    @property
    def purpose(self) -> str: ...

    def check_cancelled(self) -> None: ...

    def report_progress(self, percent: float | None) -> None: ...

    def register_process(self, proc: Any) -> None: ...

    def unregister_process(self, proc: Any) -> None: ...

    def resolve_credential(self, ref: str) -> str: ...


class FakeContext:
    """Deterministic test context with read-only identity and in-memory callbacks."""

    def __init__(
        self,
        task_id: str = "task_test",
        invocation_id: str | None = None,
        task_dir: Path = Path("/tmp/task_test"),
        purpose: str = "translate",
        credentials: Mapping[str, str] | None = None,
        cancelled: bool = False,
    ) -> None:
        self._task_id = task_id
        self._invocation_id = invocation_id or f"invocation_{uuid.uuid4().hex}"
        self._task_dir = task_dir
        self._purpose = purpose
        self.credentials = dict(credentials or {})
        self.cancelled = cancelled
        self.progress: list[float | None] = []
        self.registered_processes: list[Any] = []
        self.unregistered_processes: list[Any] = []
        self._lock = threading.Lock()

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def invocation_id(self) -> str:
        return self._invocation_id

    @property
    def task_dir(self) -> Path:
        return self._task_dir

    @property
    def purpose(self) -> str:
        return self._purpose

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise PluginCancelled("plugin invocation cancelled")

    def report_progress(self, percent: float | None) -> None:
        if percent is not None and (
            isinstance(percent, bool)
            or not isinstance(percent, (int, float))
            or not 0 <= percent <= 100
        ):
            raise ValueError("progress must be between 0 and 100 or None")
        with self._lock:
            self.progress.append(percent)

    def register_process(self, proc: Any) -> None:
        with self._lock:
            self.registered_processes.append(proc)

    def unregister_process(self, proc: Any) -> None:
        with self._lock:
            self.unregistered_processes.append(proc)
            try:
                self.registered_processes.remove(proc)
            except ValueError:
                pass

    def resolve_credential(self, ref: str) -> str:
        try:
            return self.credentials[ref]
        except KeyError as exc:
            raise KeyError(f"credential reference is unavailable: {ref}") from exc

    def assert_credentials_not_exposed(self, value: object) -> None:
        """Fail if any stored secret appears in serialized/log-like content."""
        if isinstance(value, str):
            rendered = value
        else:
            try:
                rendered = json.dumps(value, default=repr, sort_keys=True)
            except (TypeError, ValueError):
                rendered = repr(value)
        for secret in self.credentials.values():
            if secret and secret in rendered:
                raise AssertionError("credential value exposed")
