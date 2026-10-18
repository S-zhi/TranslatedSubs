"""Desktop lifecycle tests use temporary SQLite and no media/cloud services."""

from __future__ import annotations

import builtins
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import runpy
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from src.desktop import lifecycle
from src.store.task_store import STATUSES, TaskStore


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "tasks.sqlite3")


def make_task(store, status="PENDING"):
    record = store.create(
        url="https://example.test/video", source_lang="en", target_lang="zh-CN",
        mode="bilingual", burn="hard", model="local:base.en",
        engine="local-opus-en-zh", source_type="upload", title="Local lecture",
        runtime_config_json='{"saved":"runtime"}',
        plugin_plan_json='{"saved":"plan"}',
    )
    return store.update(
        record.id, status=status, progress=64, current_step="TRANSLATING",
        output_video="output.mp4", output_subtitle="translated.srt",
        output_dubbed_video="dubbed.mp4", error="previous reason",
        error_code="previous_code", tts_status="SUCCESS",
    )


def test_import_and_injected_operations_do_not_bootstrap(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "src" or name.startswith("src."):
            raise AssertionError(f"Unexpected application import: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    namespace = runpy.run_path(str(Path(lifecycle.__file__)))
    injected = SimpleNamespace(list=lambda: [])
    assert namespace["active_task_count"](injected) == 0
    assert namespace["recover_desktop_tasks"](injected) == []


def test_active_count_includes_every_nonterminal_and_unknown_state(store):
    for status in (*STATUSES, "FUTURE_ACTIVE_STAGE"):
        make_task(store, status)
    before = [asdict(record) for record in store.list()]
    assert lifecycle.active_task_count(store) == len(STATUSES) - 3 + 1
    assert [asdict(record) for record in store.list()] == before


def test_active_count_empty_and_uses_default_store(store, monkeypatch):
    from src.handler import deps

    monkeypatch.setattr(deps, "get_store", lambda: store)
    assert lifecycle.active_task_count() == 0
    record = make_task(store)
    assert lifecycle.active_task_count() == 1
    assert lifecycle.recover_desktop_tasks() == [record.id]
    assert lifecycle.active_task_count() == 0


def test_recovery_preserves_records_and_files_and_is_idempotent(store, tmp_path):
    records = [make_task(store, status) for status in STATUSES]
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for name in ("source.mp4", "output.mp4", "translated.srt", "incomplete.part"):
        (artifacts / name).write_bytes(name.encode())
    files_before = {path.name: path.read_bytes() for path in artifacts.iterdir()}
    active = [r.id for r in records if r.status not in lifecycle.TERMINAL_STATUSES]
    store.update(active[0], is_cancelling=1)
    before = {record.id: asdict(store.get(record.id)) for record in records}

    assert set(lifecycle.recover_desktop_tasks(store)) == set(active)
    for record in records:
        after = asdict(store.get(record.id))
        if record.id not in active:
            assert after == before[record.id]
            continue
        assert after["status"] == "FAILED"
        assert after["error_code"] == "desktop_interrupted"
        assert "重试" in after["error"]
        assert after["is_cancelling"] == 0
        for field in ("status", "error", "error_code", "updated_at", "is_cancelling"):
            after.pop(field)
            before[record.id].pop(field)
        assert after == before[record.id]

    recovered = [asdict(record) for record in store.list()]
    assert lifecycle.recover_desktop_tasks(store) == []
    assert [asdict(record) for record in store.list()] == recovered
    assert {path.name: path.read_bytes() for path in artifacts.iterdir()} == files_before


def test_recovery_continues_after_individual_failure_and_can_retry(store, caplog):
    bad, good = make_task(store), make_task(store)

    def update(task_id, **fields):
        if task_id == bad.id:
            raise OSError("sensitive diagnostic must not be exposed")
        return store.update(task_id, **fields)

    injected = SimpleNamespace(list=lambda: [bad, good], update=update)
    assert lifecycle.recover_desktop_tasks(injected) == [good.id]
    assert store.get(bad.id).status == "PENDING"
    assert store.get(good.id).status == "FAILED"
    assert "OSError" in caplog.text
    assert "sensitive diagnostic" not in caplog.text
    assert lifecycle.recover_desktop_tasks(store) == [bad.id]


def test_recovery_does_not_claim_a_deleted_record():
    injected = SimpleNamespace(
        list=lambda: [SimpleNamespace(id="gone", status="PENDING")],
        update=lambda *args, **kwargs: None,
    )
    assert lifecycle.recover_desktop_tasks(injected) == []


def test_unreadable_store_is_not_reported_as_empty():
    def fail():
        raise OSError("database unavailable")

    injected = SimpleNamespace(list=fail)
    with pytest.raises(OSError):
        lifecycle.active_task_count(injected)
    with pytest.raises(OSError):
        lifecycle.recover_desktop_tasks(injected)


@pytest.fixture
def real_runner(store, tmp_path, monkeypatch):
    # Keep even first-import runner initialization inside the test directory.
    from src.config import config
    from src.config.bootstrap import DEFAULT_BOOTSTRAP

    bootstrap = {key: dict(value) for key, value in DEFAULT_BOOTSTRAP.items()}
    bootstrap["paths"].update({
        "dbPath": str(store.db_path), "dataDir": str(tmp_path),
    })
    monkeypatch.setattr(config, "load_bootstrap", lambda: bootstrap)
    from src.service import orchestrator, runner

    monkeypatch.setattr(runner, "_store", store)
    monkeypatch.setattr(runner, "_procs", {})
    monkeypatch.setattr(runner, "_task_state_locks", {})
    monkeypatch.setattr(runner, "_executor", None)
    monkeypatch.setattr(runner, "_current_max_workers", 0)
    monkeypatch.setattr(runner, "task_dir", lambda task_id: tmp_path / task_id)
    monkeypatch.setattr(orchestrator, "_cancel_events", {})
    return runner


def test_shutdown_cancels_tasks_terminates_registered_process_preserves_results(
    store, real_runner, tmp_path,
):
    active = make_task(store, "BURNING")
    terminal = [make_task(store, status) for status in lifecycle.TERMINAL_STATUSES]
    preserved = {record.id: asdict(record) for record in terminal}
    directory = tmp_path / active.id
    directory.mkdir()
    for name in ("source.mp4", "output.mp4", "translated.srt", "download.part"):
        (directory / name).write_bytes(name.encode())
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    real_runner.register_process(active.id, process)
    try:
        result = lifecycle.shutdown_desktop_tasks()
        assert result["cancelled"] == [active.id]
        assert result["errors"] == []
        assert result["active_remaining"] == 0
        assert result["registered_processes_observed"] == 1
        assert result["processes_still_running"] == []
        assert result["processes_unverified"] == []
        assert result["executor_shutdown_requested"] is True
        assert result["workers_joined"] is False
        assert process.poll() is not None
        assert real_runner.is_cancelled_signal(active.id)
        cancelled = store.get(active.id)
        assert cancelled.status == "CANCELLED"
        assert cancelled.is_cancelling == 0
        assert cancelled.output_video == active.output_video
        assert cancelled.plugin_plan_json == active.plugin_plan_json
        assert cancelled.runtime_config_json == active.runtime_config_json
        assert not (directory / "download.part").exists()
        for name in ("source.mp4", "output.mp4", "translated.srt"):
            assert (directory / name).read_bytes() == name.encode()
        assert {r.id: asdict(store.get(r.id)) for r in terminal} == preserved
        assert lifecycle.shutdown_desktop_tasks()["cancelled"] == []
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_shutdown_keeps_queued_pipeline_from_starting(store, real_runner, monkeypatch):
    task = make_task(store)
    release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    blocker = executor.submit(release.wait)
    calls = []
    monkeypatch.setattr(real_runner, "_executor", executor)
    monkeypatch.setattr(real_runner, "run_pipeline", lambda *a, **kw: calls.append(a))
    queued = executor.submit(real_runner._run, task.id)
    try:
        result = lifecycle.shutdown_desktop_tasks(store, runner=real_runner)
        assert result["cancelled"] == [task.id]
        assert not blocker.done()  # shutdown did not join a running worker
        release.set()
        queued.result(timeout=5)
        assert calls == []
        assert store.get(task.id).status == "CANCELLED"
    finally:
        release.set()
        executor.shutdown(wait=True)


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


class FakeRunner:
    def __init__(self, store):
        self._store = store
        self._procs = {}
        self._procs_lock = threading.Lock()
        self.signalled = []
        self.terminated = []
        self.shutdown_wait = []

    def cancel_pipeline(self, task_id):
        self.set_cancelled_signal(task_id)
        self._store.update(task_id, status="CANCELLED")
        return True

    def set_cancelled_signal(self, task_id):
        self.signalled.append(task_id)

    def _terminate_process(self, process, task_id=None):
        self.terminated.append((task_id, process.pid))
        process.returncode = -15

    def unregister_process(self, task_id, process):
        if process in self._procs.get(task_id, []):
            self._procs[task_id].remove(process)

    def shutdown_executor(self, wait=True):
        self.shutdown_wait.append(wait)


def test_shutdown_handles_cancel_error_popped_and_late_processes(store):
    bad, good = make_task(store), make_task(store)
    runner = FakeRunner(store)
    initial, late = FakeProcess(11), FakeProcess(12)
    runner._procs[bad.id] = [initial]

    def cancel(task_id):
        if task_id == bad.id:
            runner._procs.pop(task_id)
            raise OSError("private cancellation detail")
        runner._store.update(task_id, status="CANCELLED")
        runner._procs[task_id] = [late]
        return True

    runner.cancel_pipeline = cancel
    result = lifecycle.shutdown_desktop_tasks(store, runner=runner)
    assert result["cancelled"] == [good.id]
    assert result["active_remaining"] == 1
    assert result["errors"] == [{
        "operation": "cancel_task", "task_id": bad.id, "error_type": "OSError",
    }]
    assert set(runner.terminated) == {(bad.id, 11), (good.id, 12)}
    assert result["registered_processes_observed"] == 2
    assert runner.shutdown_wait == [False]


def test_shutdown_handles_terminal_registered_process_without_rewriting_record(store):
    terminal = make_task(store, "SUCCESS")
    runner = FakeRunner(store)
    runner._procs[terminal.id] = [FakeProcess(21)]
    before = asdict(store.get(terminal.id))
    result = lifecycle.shutdown_desktop_tasks(store, runner=runner)
    assert result["cancelled"] == []
    assert result["errors"] == []
    assert runner.terminated == [(terminal.id, 21)]
    assert asdict(store.get(terminal.id)) == before


def test_shutdown_reports_survivor_and_continues_after_process_failure(store):
    runner = FakeRunner(store)
    bad, good = FakeProcess(31), FakeProcess(32)
    runner._procs = {"bad": [bad], "good": [good]}
    terminate = runner._terminate_process

    def fail_one(process, task_id=None):
        if process is bad:
            raise PermissionError("private process detail")
        terminate(process, task_id)

    runner._terminate_process = fail_one
    result = lifecycle.shutdown_desktop_tasks(store, runner=runner)
    assert result["processes_still_running"] == [{"task_id": "bad", "pid": 31}]
    assert result["errors"] == [{
        "operation": "terminate_process", "task_id": "bad", "error_type": "PermissionError",
    }]
    assert good.returncode == -15
    assert runner._procs["bad"] == [bad]
    assert runner.shutdown_wait == [False]


def test_shutdown_does_not_claim_silently_failed_termination_or_worker_completion(store):
    runner = FakeRunner(store)
    runner._procs["still-running"] = [FakeProcess(41)]
    runner._terminate_process = lambda *a, **kw: None
    result = lifecycle.shutdown_desktop_tasks(store, runner=runner)
    assert result["active_remaining"] == 0
    assert result["processes_still_running"] == [{"task_id": "still-running", "pid": 41}]
    assert result["workers_joined"] is False


def test_shutdown_handles_list_count_and_executor_errors_still_terminates_processes():
    def fail(*args, **kwargs):
        raise OSError("not readable")

    store = SimpleNamespace(list=fail)
    runner = FakeRunner(store)
    runner._procs["orphan"] = [FakeProcess(51)]
    runner.shutdown_executor = fail
    result = lifecycle.shutdown_desktop_tasks(store, runner=runner)
    assert result["active_remaining"] is None
    assert result["executor_shutdown_requested"] is False
    assert runner.terminated == [("orphan", 51)]
    assert [error["operation"] for error in result["errors"]] == [
        "list_tasks", "shutdown_executor", "count_tasks",
    ]


def test_shutdown_reports_false_cancel_and_unknown_process_state(store):
    record = make_task(store)
    runner = FakeRunner(store)
    runner.cancel_pipeline = lambda task_id: False

    def fail_poll():
        raise OSError("poll unavailable")

    runner._procs[record.id] = [SimpleNamespace(pid=61, poll=fail_poll)]
    result = lifecycle.shutdown_desktop_tasks(store, runner=runner)
    assert result["not_cancelled"] == [record.id]
    assert result["active_remaining"] == 1
    assert result["processes_unverified"] == [{"task_id": record.id, "pid": 61}]
    assert runner.shutdown_wait == [False]


def test_shutdown_reports_registry_failure_and_still_cancels_tasks(store):
    record = make_task(store)
    runner = FakeRunner(store)
    del runner._procs
    result = lifecycle.shutdown_desktop_tasks(store, runner=runner)
    assert result["cancelled"] == [record.id]
    assert [error["operation"] for error in result["errors"]] == [
        "snapshot_processes", "snapshot_processes",
    ]
    assert result["executor_shutdown_requested"] is True
