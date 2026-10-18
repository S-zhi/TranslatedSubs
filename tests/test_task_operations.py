from concurrent.futures import ThreadPoolExecutor

from src.store import TaskStore


def _terminal(store):
    task = store.create(url="http://example.test/video", source_lang="en", target_lang="zh-CN",
                        mode="mono", burn="hard", model="tiny", engine="deepseek")
    return store.update(task.id, status="SUCCESS", progress=100)


def test_only_one_worker_can_claim_and_finish_operation(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = _terminal(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: TaskStore(store.db_path).claim_operation(task.id), range(2)))
    assert sum(result is not None for result in results) == 1
    previous, token = next(result for result in results if result is not None)
    assert previous.status == "SUCCESS"
    assert store.get(task.id).current_step == token
    assert store.finish_operation(task.id, "reburn:wrong", status="SUCCESS") is None
    assert store.update_if_status(task.id, ("SUCCESS",), status="PENDING") is None
    assert not store.delete_if_status(task.id, ("SUCCESS",))
    result = store.finish_operation(task.id, token, status=previous.status, progress=previous.progress,
                                    current_step=previous.current_step)
    assert result.status == "SUCCESS"
    assert store.finish_operation(task.id, token, status="SUCCESS") is None


def test_subtitle_edit_claim_restores_failed_task(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = _terminal(store)
    store.update(task.id, status="FAILED", error="original failure")
    previous, token = store.claim_operation(task.id, operation="subtitle_edit")
    assert token.startswith("subtitle_edit:")
    result = store.finish_operation(task.id, token, status=previous.status,
                                    current_step=previous.current_step, error=previous.error)
    assert result.status == "FAILED"
    assert result.error == "original failure"


def test_cancelled_or_running_claim_cannot_publish(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = _terminal(store)
    _, token = store.claim_operation(task.id)
    store.update(task.id, is_cancelling=1)
    assert store.finish_operation(task.id, token, status="SUCCESS") is None
    assert store.claim_operation(task.id) is None
