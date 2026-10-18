"""Process-local operation locks; TaskStore claims provide cross-worker exclusion."""

from __future__ import annotations

import threading
from weakref import WeakValueDictionary

_locks: WeakValueDictionary[str, threading.RLock] = WeakValueDictionary()
_guard = threading.Lock()


def get_task_operation_lock(task_id: str) -> threading.RLock:
    with _guard:
        lock = _locks.get(task_id)
        if lock is None:
            lock = threading.RLock()
            _locks[task_id] = lock
        return lock
