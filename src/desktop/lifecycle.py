"""Desktop task recovery and cooperative shutdown.

Importing this module does not initialize configuration, SQLite, or the runner.
The runtime must recover before accepting work (instead of server recovery),
and close task admission before calling shutdown. Only the native process owner
can enforce the final backend process-group timeout.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.store.task_store import TaskStore

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILED", "CANCELLED"})
INTERRUPTED_ERROR_CODE = "desktop_interrupted"
INTERRUPTED_ERROR = (
    "上次应用退出或意外中断，任务未完成。请点击“重试”重新处理；"
    "已完成的文件会保留，但不保证从中断位置继续。"
)


def _resolve_store(store: TaskStore | None) -> TaskStore:
    if store is not None:
        return store
    from src.handler.deps import get_store

    return get_store()


def active_task_count(store: TaskStore | None = None) -> int:
    """Count all nonterminal records, including queued work, without pagination."""
    return sum(
        record.status not in TERMINAL_STATUSES
        for record in _resolve_store(store).list()
    )


def recover_desktop_tasks(store: TaskStore | None = None) -> list[str]:
    """Mark interrupted tasks FAILED, exposing the existing manual retry action.

    Call only before workers/admission start. Never enqueue or inspect/delete
    artifacts. Return IDs actually updated; individual write failures are logged
    and remain eligible for a later recovery attempt. A failed list operation
    propagates so startup cannot mistake an unreadable database for no work.
    """
    store = _resolve_store(store)
    recovered = []
    for record in store.list():
        if record.status in TERMINAL_STATUSES:
            continue
        try:
            updated = store.update(
                record.id,
                status="FAILED",
                error=INTERRUPTED_ERROR,
                error_code=INTERRUPTED_ERROR_CODE,
                is_cancelling=0,
            )
        except Exception as exc:
            logger.warning(
                "Desktop recovery failed: task=%s error_type=%s",
                record.id, type(exc).__name__,
            )
            continue
        if updated is not None:
            recovered.append(record.id)
    return recovered


def _registered_processes(runner: Any) -> list[tuple[str, Any]]:
    # Runner has no public snapshot API. Keep access to its existing registry
    # under the same lock; do not hold that lock while waiting for a process.
    with runner._procs_lock:
        return [
            (task_id, process)
            for task_id, processes in runner._procs.items()
            for process in processes
        ]


def shutdown_desktop_tasks(
    store: TaskStore | None = None, *, runner: Any = None,
) -> dict[str, Any]:
    """Request cancellation and nonblocking executor shutdown after admission closes.

    Tests may supply a runner-shaped object and its store. The injected runner's
    cancel_pipeline must operate on that same store. Default calls use the live
    runner's store. Cancellation delegates to runner so queued jobs, cooperative
    signals, and partial-file cleanup retain their existing semantics.

    Snapshot registered processes before cancellation: cancel_pipeline removes
    them from its registry even if termination fails. Also inspect registrations
    made during cancellation, including processes belonging to terminal records.
    Only those registered objects are handled, never arbitrary PIDs/services.

    Results describe observations, not worker completion. A zero active count or
    no observed subprocesses does NOT establish that native inference stopped.
    The runtime/native owner must still exit the backend with a bounded timeout.
    """
    if runner is None:
        from src.service import runner

    if store is None:
        store = runner._store

    result: dict[str, Any] = {
        "cancelled": [],
        "not_cancelled": [],
        "errors": [],
        "active_remaining": None,
        "registered_processes_observed": 0,
        "processes_still_running": [],
        "processes_unverified": [],
        "executor_shutdown_requested": False,
        "workers_joined": False,
    }

    def record_error(operation: str, exc: Exception, task_id: str | None = None) -> None:
        # Exception strings can contain paths/URLs/credentials. Return only the
        # failure category; callers can display an understandable operation name.
        result["errors"].append({
            "operation": operation, "task_id": task_id,
            "error_type": type(exc).__name__,
        })

    processes: dict[int, tuple[str, Any]] = {}

    def snapshot_processes() -> None:
        try:
            for task_id, process in _registered_processes(runner):
                processes[id(process)] = (task_id, process)
        except Exception as exc:
            record_error("snapshot_processes", exc)

    snapshot_processes()
    try:
        records = store.list()
    except Exception as exc:
        record_error("list_tasks", exc)
        records = []

    for record in records:
        if record.status in TERMINAL_STATUSES:
            continue
        try:
            cancelled = runner.cancel_pipeline(record.id)
        except Exception as exc:
            record_error("cancel_task", exc, record.id)
        else:
            key = "cancelled" if cancelled else "not_cancelled"
            result[key].append(record.id)

    snapshot_processes()
    result["registered_processes_observed"] = len(processes)
    for task_id, process in processes.values():
        observation = {"task_id": task_id, "pid": getattr(process, "pid", None)}
        try:
            if process.poll() is None:
                runner.set_cancelled_signal(task_id)
                runner._terminate_process(process, task_id=task_id)
        except Exception as exc:
            record_error("terminate_process", exc, task_id)
        try:
            if process.poll() is None:
                result["processes_still_running"].append(observation)
            else:
                runner.unregister_process(task_id, process)
        except Exception as exc:
            record_error("verify_process", exc, task_id)
            result["processes_unverified"].append(observation)

    try:
        runner.shutdown_executor(wait=False)
        result["executor_shutdown_requested"] = True
    except Exception as exc:
        record_error("shutdown_executor", exc)
    try:
        result["active_remaining"] = active_task_count(store)
    except Exception as exc:
        record_error("count_tasks", exc)
    return result
