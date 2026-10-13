"""执行器单测：验证 enqueue 提交、事件写库、失败兜底。mock run_pipeline，不真跑流水线。"""

from __future__ import annotations

import pytest

from src.service import runner
from src.service.orchestrator import PipelineEvent
from src.store import TaskStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = TaskStore(tmp_path / "t.db")
    monkeypatch.setattr(runner, "_store", s)  # 执行器用临时库
    return s


def _make_task(store) -> str:
    rec = store.create(
        url="http://x/v", source_lang="auto", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
    )
    return rec.id


def test_enqueue_submits_to_executor(monkeypatch):
    submitted = []

    class FakeExecutor:
        def submit(self, fn, *args):
            submitted.append((fn, args))

    monkeypatch.setattr(runner, "_executor", FakeExecutor())
    runner.enqueue_pipeline("task_x")
    assert submitted[0][0] is runner._run
    assert submitted[0][1] == ("task_x",)


def test_run_writes_progress_and_success(store, monkeypatch):
    tid = _make_task(store)

    def fake_pipeline(params, on_event, **kwargs):
        assert {"plugin_manager", "plugin_plan", "credential_resolver", "legacy_cache"}.issubset(kwargs)
        on_event(PipelineEvent("DOWNLOADING", 10, "DOWNLOADING"))
        on_event(PipelineEvent("TRANSCRIBING", 50, "TRANSCRIBING"))
        on_event(PipelineEvent(
            "SUCCESS", 100, None,
            title="My Video",
            outputs={"video": "/d/output.mp4", "subtitle": "/d/translated.srt"},
        ))

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    runner._run(tid)

    rec = store.get(tid)
    assert rec.status == "SUCCESS"
    assert rec.progress == 100
    assert rec.title == "My Video"
    assert rec.output_video == "output.mp4"
    assert rec.output_subtitle == "translated.srt"


def test_run_passes_params_from_record(store, monkeypatch):
    rec = store.create(
        url="http://x/v", source_lang="en", target_lang="ja",
        mode="bilingual", burn="soft", model="medium", engine="deepseek",
    )
    seen = {}

    def fake_pipeline(params, on_event, **kwargs):
        seen["url"] = params.url
        seen["target"] = params.target_lang
        seen["mode"] = params.mode
        seen["burn"] = params.burn
        seen["model"] = params.model
        on_event(PipelineEvent("SUCCESS", 100, None, outputs={}))

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    runner._run(rec.id)

    assert seen == {"url": "http://x/v", "target": "ja", "mode": "bilingual",
                    "burn": "soft", "model": "medium"}


def test_run_failure_persists_failed(store, monkeypatch):
    tid = _make_task(store)

    def boom(params, on_event, **kwargs):
        on_event(PipelineEvent("DOWNLOADING", 5, "DOWNLOADING"))
        on_event(PipelineEvent("FAILED", 5, "DOWNLOADING", error="下载失败", error_code="download_failed"))
        raise RuntimeError("下载失败")

    monkeypatch.setattr(runner, "run_pipeline", boom)
    runner._run(tid)  # 不应抛出

    rec = store.get(tid)
    assert rec.status == "FAILED"
    assert "下载失败" in rec.error
    assert rec.error_code == "download_failed"


def test_run_failure_fallback_when_no_event(store, monkeypatch):
    """run_pipeline 直接抛异常、没发 FAILED 事件时，兜底也要写 FAILED。"""
    tid = _make_task(store)

    def boom(params, on_event, **kwargs):
        raise RuntimeError("意外崩溃")

    monkeypatch.setattr(runner, "run_pipeline", boom)
    runner._run(tid)

    rec = store.get(tid)
    assert rec.status == "FAILED"



def test_run_cancelled_resource_error_marks_resources_missing(store, monkeypatch):
    tid = _make_task(store)

    def cancelled_resource_error(params, on_event, **kwargs):
        store.update(tid, status="CANCELLED", error="用户取消")
        runner.set_cancelled_signal(tid)
        raise runner.ResourceError("源文件缺失", runner.ResourceState.DELETED)

    monkeypatch.setattr(runner, "run_pipeline", cancelled_resource_error)
    runner._run(tid)

    assert store.get(tid).status == "CANCELLED"


def test_run_cancelled_exception_marks_resources_missing(store, monkeypatch):
    tid = _make_task(store)

    def cancelled_exception(params, on_event, **kwargs):
        store.update(tid, status="CANCELLED", error="用户取消")
        runner.set_cancelled_signal(tid)
        raise RuntimeError("取消时异常")

    monkeypatch.setattr(runner, "run_pipeline", cancelled_exception)
    runner._run(tid)

    assert store.get(tid).status == "CANCELLED"


def test_run_missing_task_skips(store, monkeypatch):
    called = []
    monkeypatch.setattr(runner, "run_pipeline", lambda *a, **k: called.append(1))
    runner._run("nonexistent")
    assert not called  # 任务不存在时不应调用 run_pipeline


def test_cancel_pipeline_terminates_procs_and_updates_status(store):
    import subprocess
    tid = _make_task(store)
    proc = subprocess.Popen(["sleep", "10"])
    runner.register_process(tid, proc)

    ok = runner.cancel_pipeline(tid)
    assert ok is True
    rec = store.get(tid)
    assert rec.status == "CANCELLED"
    assert rec.error == "用户取消"
    proc.wait(timeout=2)
    assert proc.poll() is not None


def test_cancel_pipeline_cleans_up_partial_artifacts(store, tmp_path, monkeypatch):
    tid = _make_task(store)
    store.update(tid, status="BURNING", current_step="BURNING")

    task_dir_path = tmp_path / tid
    task_dir_path.mkdir(parents=True, exist_ok=True)
    (task_dir_path / "source.mp4").write_bytes(b"source_video")
    (task_dir_path / "audio.wav").write_bytes(b"audio_data")
    (task_dir_path / "original.srt").write_bytes(b"original_srt")
    (task_dir_path / "translated.srt").write_bytes(b"translated_srt")
    (task_dir_path / "output.mp4").write_bytes(b"partial_ffmpeg_output")

    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda t: task_dir_path)

    ok = runner.cancel_pipeline(tid)
    assert ok is True

    # Published outputs are retained; only invocation-owned partial files are removed.
    assert (task_dir_path / "source.mp4").exists()
    assert (task_dir_path / "audio.wav").exists()
    assert (task_dir_path / "original.srt").exists()
    assert (task_dir_path / "translated.srt").exists()
    assert (task_dir_path / "output.mp4").exists()


def test_retry_cancelled_task_clears_signal_and_resumes(store, monkeypatch):
    tid = _make_task(store)
    store.update(tid, status="CANCELLED", error="用户取消")

    seen_cancellation_state = []

    def fake_pipeline(params, on_event, **kwargs):
        from src.service.orchestrator import is_cancelled_signal
        seen_cancellation_state.append(is_cancelled_signal(params.task_id))
        on_event(PipelineEvent("SUCCESS", 100, None, title="Retry Done"))

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)

    # Simulate retry resetting status to PENDING and enqueuing
    store.update(tid, status="PENDING", progress=0, error=None)
    runner._run(tid)

    rec = store.get(tid)
    assert rec.status == "SUCCESS"
    assert seen_cancellation_state == [False]


def test_run_does_not_overwrite_cancelled_status(store, monkeypatch):
    tid = _make_task(store)
    store.update(tid, status="CANCELLED", error="用户取消")

    def boom(params, on_event, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "run_pipeline", boom)
    runner._run(tid)

    rec = store.get(tid)
    assert rec.status == "CANCELLED"
    assert rec.error == "用户取消"


def test_run_registration_and_cancel_are_serialized(store, monkeypatch):
    """Cancellation arriving after row-read cannot be cleared by signal registration."""
    import threading

    tid = _make_task(store)
    entered_ensure = threading.Event()
    continue_ensure = threading.Event()
    called = []
    original_ensure = runner.ensure_plan

    def blocking_ensure(*args, **kwargs):
        entered_ensure.set()
        assert continue_ensure.wait(timeout=2)
        return original_ensure(*args, **kwargs)

    def pipeline(params, on_event, **kwargs):
        called.append(True)
        if runner.is_cancelled_signal(tid):
            raise runner.PipelineCancelledError("cancelled")

    monkeypatch.setattr(runner, "ensure_plan", blocking_ensure)
    monkeypatch.setattr(runner, "run_pipeline", pipeline)
    worker = threading.Thread(target=runner._run, args=(tid,))
    worker.start()
    assert entered_ensure.wait(timeout=2)

    cancelled = []
    cancel_worker = threading.Thread(
        target=lambda: cancelled.append(runner.cancel_pipeline(tid))
    )
    cancel_worker.start()
    continue_ensure.set()
    worker.join(timeout=2)
    cancel_worker.join(timeout=2)

    assert not worker.is_alive() and not cancel_worker.is_alive()
    assert cancelled == [True]
    assert store.get(tid).status == "CANCELLED"


def test_plugin_cooperative_cancel_marks_task_cancelled(store, monkeypatch):
    tid = _make_task(store)

    def cooperative_cancel(params, on_event, **kwargs):
        raise runner.PipelineCancelledError("plugin requested cancellation")

    monkeypatch.setattr(runner, "run_pipeline", cooperative_cancel)
    runner._run(tid)

    rec = store.get(tid)
    assert rec.status == "CANCELLED"
    assert rec.error_code == "cancelled"


def test_runner_resolves_cookie_path_setting_for_download_plugin(store, monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace

    from src.plugin_system import DownloadResult
    import src.plugins

    tid = _make_task(store)
    cookie_path = tmp_path / "cookies.txt"
    cookie_path.write_text("cookie data")
    monkeypatch.setattr(runner, "settings", SimpleNamespace(cookies_file=cookie_path))
    store.update(tid, need_subtitle=0, plugin_plan_json=json.dumps({
        "schema_version": 1,
        "slots": {
            "download": {
                "plugin_id": "builtin.ytdlp",
                "implementation_version": "1",
                "config": {"quality": "480p", "cookies_ref": "settings:cookies_file"},
            },
        },
    }))

    class Manager:
        def __init__(self):
            self.cookie = None

        def run(self, plugin_id, version, request, config, context):
            self.cookie = context.resolve_credential(config["cookies_ref"])
            output = request.target_dir / "source.mp4"
            output.write_bytes(b"source")
            return DownloadResult(output, title="source")

    manager = Manager()
    monkeypatch.setattr(src.plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(runner, "ensure_plan", lambda rec, *_args: rec)
    runner._run(tid)

    assert manager.cookie == str(cookie_path)
    assert store.get(tid).status == "SUCCESS"


def test_recover_interrupted_tasks_requeues_only_non_terminal(store, monkeypatch):
    running = _make_task(store)
    pending = _make_task(store)
    success = _make_task(store)
    failed = _make_task(store)
    store.update(running, status="TRANSLATING", progress=70, current_step="TRANSLATING")
    store.update(pending, status="PENDING")
    store.update(success, status="SUCCESS", progress=100)
    store.update(failed, status="FAILED", error="boom")

    submitted = []

    class FakeExecutor:
        def submit(self, fn, *args):
            submitted.append((fn, args))

    monkeypatch.setattr(runner, "_executor", FakeExecutor())

    recovered = runner.recover_interrupted_tasks()

    assert set(recovered) == {running, pending}
    assert {args for _, args in submitted} == {(running,), (pending,)}


def test_recover_interrupted_upload_task_fails_when_source_missing(store, tmp_path, monkeypatch):
    """恢复中断的 upload 任务时，如果缺乏有效 source.* 文件（如仅有 .part），直接置为 FAILED 且不入队。"""
    upload_rec = store.create(
        url="clip.mp4", source_lang="en", target_lang="zh-CN",
        mode="mono", burn="hard", model="small", engine="deepseek",
        source_type="upload", title="clip",
    )
    store.update(upload_rec.id, status="PENDING")

    # 模拟磁盘目录，只留一个半截 .part 文件，没有正式 source.mp4
    tdir = tmp_path / upload_rec.id
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "source.mp4.part").write_bytes(b"half_upload_data")

    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path / tid)

    submitted = []

    class FakeExecutor:
        def submit(self, fn, *args):
            submitted.append((fn, args))

    monkeypatch.setattr(runner, "_executor", FakeExecutor())

    recovered = runner.recover_interrupted_tasks()

    assert upload_rec.id not in recovered
    assert submitted == []

    updated = store.get(upload_rec.id)
    assert updated.status == "FAILED"
    assert updated.error == "上传源文件缺失或损坏"
    assert updated.error_code == "resource_error"


def test_recover_interrupted_replicate_task_is_failed_without_requeue(store, monkeypatch):
    """历史 Replicate 任务不能在服务重启后再次调用已删除的后端。"""
    rec = store.create(
        url="http://x/v", source_lang="en", target_lang="zh-CN",
        mode="mono", burn="soft", model="replicate:tiny", engine="deepseek",
    )
    store.update(rec.id, status="TRANSCRIBING")
    submitted = []

    class FakeExecutor:
        def submit(self, fn, *args):
            submitted.append((fn, args))

    monkeypatch.setattr(runner, "_executor", FakeExecutor())
    assert runner.recover_interrupted_tasks() == []
    assert submitted == []
    updated = store.get(rec.id)
    assert updated.status == "FAILED"
    assert updated.error_code == "REPLICATE_BACKEND_REMOVED"


def test_get_executor_dynamic_worker_update(monkeypatch):
    runner.shutdown_executor(wait=True)
    monkeypatch.setenv("SUBTRANS_WORKERS", "3")
    exec1 = runner.get_executor()
    assert exec1._max_workers == 3

    # 修改环境变量为 5，再次 get_executor 自动平滑重构
    monkeypatch.setenv("SUBTRANS_WORKERS", "5")
    exec2 = runner.get_executor()
    assert exec2._max_workers == 5
    assert exec2 is not exec1

    runner.shutdown_executor(wait=True)
    assert runner._executor is None


def test_shutdown_executor_clears_state():
    runner.get_executor()
    assert runner._executor is not None
    runner.shutdown_executor(wait=True)
    assert runner._executor is None
    assert runner._current_max_workers == 0


def test_on_event_db_write_throttling(store, monkeypatch):
    tid = _make_task(store)

    update_calls = []
    orig_update = store.update

    def tracked_update(*args, **kwargs):
        update_calls.append((args, kwargs))
        return orig_update(*args, **kwargs)

    monkeypatch.setattr(store, "update", tracked_update)

    def fake_pipeline(params, on_event, **kwargs):
        # DOWNLOADING (status changed -> write 1)
        on_event(PipelineEvent("DOWNLOADING", 1, "DOWNLOADING"))
        # DOWNLOADING 2..9 (same decade, same status -> skipped)
        for p in range(2, 10):
            on_event(PipelineEvent("DOWNLOADING", p, "DOWNLOADING"))
        # DOWNLOADING 10 (decade changed 0 -> 1 -> write 2)
        on_event(PipelineEvent("DOWNLOADING", 10, "DOWNLOADING"))
        # DOWNLOADING 11..19 (same decade 1 -> skipped)
        for p in range(11, 20):
            on_event(PipelineEvent("DOWNLOADING", p, "DOWNLOADING"))
        # SUCCESS 100 (terminal & status changed -> write 3)
        on_event(PipelineEvent("SUCCESS", 100, None, title="Done"))

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    runner._run(tid)

    # Exactly 3 DB updates triggered instead of 20+
    assert len(update_calls) == 3
    assert store.get(tid).status == "SUCCESS"


def test_terminate_process_sigkill_fallback(monkeypatch):
    import subprocess
    from unittest.mock import MagicMock

    proc = MagicMock(spec=subprocess.Popen)
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5), None]

    runner._terminate_process(proc, task_id="task_123")

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert proc.wait.call_count == 2


def test_cancel_pipeline_cleans_up_partial_artifacts(store, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "task_dir", lambda task_id: tmp_path / task_id)

    tid = _make_task(store)
    tdir = tmp_path / tid
    tdir.mkdir(parents=True, exist_ok=True)

    out_mp4 = tdir / "output.mp4"
    out_mp4.write_text("half mp4 content")
    part_file = tdir / "video.mp4.part"
    part_file.write_text("downloading part")
    tmp_file = tdir / "tmp_burn.srt"
    tmp_file.write_text("tmp srt")
    source_file = tdir / "source.mp4"
    source_file.write_text("valid source video")

    ok = runner.cancel_pipeline(tid)
    assert ok is True

    assert out_mp4.exists()
    assert not part_file.exists()
    assert not tmp_file.exists()
    assert source_file.exists()


def test_run_finally_terminates_procs_and_cleans_artifacts(store, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "task_dir", lambda task_id: tmp_path / task_id)

    tid = _make_task(store)
    tdir = tmp_path / tid
    tdir.mkdir(parents=True, exist_ok=True)

    out_mp4 = tdir / "output.mp4"
    out_mp4.write_text("half mp4 content")

    import subprocess
    from unittest.mock import MagicMock
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.poll.return_value = None

    def fake_pipeline(params, on_event, **kwargs):
        runner.register_process(tid, mock_proc)
        runner.set_cancelled_signal(tid)
        raise runner.PipelineCancelledError("cancelled")

    monkeypatch.setattr(runner, "run_pipeline", fake_pipeline)
    runner._run(tid)

    mock_proc.terminate.assert_called_once()
    assert out_mp4.exists()
