"""保留策略后台调度器服务。

定期（默认每小时）或在修改保留配置时，自动执行离线产物清理，
并将上次清理执行时间 lastRunAt 回写到 Redis 业务配置。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from src.config import settings
from src.store import ProbeStore, TaskStore

logger = logging.getLogger(__name__)

_scheduler_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def execute_retention_cleanup(
    store: Optional[TaskStore] = None,
    probes: Optional[ProbeStore] = None,
) -> Optional[object]:
    """根据当前配置的保留天数执行自动清理。"""
    from src.handler.storage import (
        CleanupPreviewRequest,
        _load_retention,
        _record_retention_run,
        execute_cleanup,
    )

    ret = _load_retention()
    if ret.days is None or ret.days <= 0:
        return None

    task_store = store or TaskStore(settings.db_path)
    probe_store = probes or ProbeStore(settings.db_path)

    req = CleanupPreviewRequest(
        olderThanDays=ret.days,
        preserveTaskRecords=True,
    )
    res = execute_cleanup(req, task_store, probe_store)

    now_ms = int(time.time() * 1000)
    _record_retention_run(now_ms)

    logger.info(
        "保留策略自动清理完成: days=%d, cleaned_task_artifacts=%d, deleted_bytes=%d",
        ret.days,
        len(res.partial),
        res.deletedBytes,
    )
    return res


def _scheduler_loop(check_interval_sec: float = 3600.0) -> None:
    logger.info("保留策略调度器已启动")
    while not _stop_event.is_set():
        try:
            execute_retention_cleanup()
        except Exception as e:
            logger.exception("保留策略自动清理执行异常: %s", e)

        _stop_event.wait(check_interval_sec)


def start_retention_scheduler(check_interval_sec: float = 3600.0) -> None:
    """启动保留策略后台调度线程。"""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _stop_event.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        args=(check_interval_sec,),
        daemon=True,
        name="retention-scheduler",
    )
    _scheduler_thread.start()


def trigger_retention_cleanup_async() -> None:
    """异步触发一次保留策略清理。"""
    threading.Thread(
        target=execute_retention_cleanup,
        daemon=True,
        name="retention-cleanup-async",
    ).start()
