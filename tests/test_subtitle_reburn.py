"""Transactional reburning through the real Manager, without model/cloud calls."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from src.config import settings
from src.plugin_system import PluginCancelled, PluginError, PluginManifest, SubtitleRenderResult
from src.plugin_system.manager import PluginManager
from src.plugin_system.registry import PluginRegistry
from src.service import asset_resolver, subtitle_reburn
from src.service.asset_resolver import AssetResolver
from src.store import TaskStore

MP4 = b"\x00\x00\x00\x18ftypisomrendered-video"
SRT = "1\n00:00:00,000 --> 00:00:01,000\nHello\n"


class _RenderFactory:
    stage = "render"
    manifest = PluginManifest(
        plugin_id="test.saved-render", stage="render", implementation_version="7",
        operations=frozenset({"subtitle", "dub_mux"}),
    )

    def __init__(self, callback=None):
        self.callback = callback
        self.calls = []
        self.closed = 0

    def create_session(self, config, context):
        factory = self

        class Session:
            def validate(self, request):
                assert request.mode == config["mode"]
                context.check_cancelled()

            def run(self, request):
                factory.calls.append((request, dict(config), context))
                context.report_progress(40)
                if factory.callback is not None:
                    return factory.callback(request, context)
                request.output_video.write_bytes(MP4)
                return SubtitleRenderResult(request.output_video)

            def close(self):
                factory.closed += 1

        return Session()


def _plan(plugin_id="test.saved-render", version="7", mode="hard"):
    return json.dumps({
        "schema_version": 1,
        "slots": {"render.subtitle": {
            "plugin_id": plugin_id, "implementation_version": version,
            "config": {"mode": mode},
        }},
    })


@pytest.fixture
def task(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks.db")
    record = store.create(
        url="https://example.test/video", source_lang="en", target_lang="zh-CN",
        mode="mono", burn="hard", model="local", engine="disabled",
        plugin_plan_json=_plan(),
    )
    store.update(
        record.id, status="SUCCESS", progress=100, output_video="output_hard.mp4",
        output_dubbed_video="output_dubbed.mp4", tts_enabled=1, tts_status="SUCCESS",
    )
    directory = tmp_path / record.id
    directory.mkdir()
    (directory / "source.mp4").write_bytes(MP4 + b"source")
    (directory / "translated.srt").write_text(SRT, encoding="utf-8")
    (directory / "output_hard.mp4").write_bytes(MP4 + b"old-output")
    (directory / "output_dubbed.mp4").write_bytes(MP4 + b"old-dub")
    (directory / "dubbed.wav").write_bytes(b"old-speech")
    for purpose in ("render.subtitle", "tts", "render.dub_mux"):
        (directory / f"{purpose}.plugin-meta.json").write_text(f"old-{purpose}")
    monkeypatch.setattr(subtitle_reburn, "task_dir", lambda task_id: tmp_path / task_id)
    monkeypatch.setattr(asset_resolver, "task_dir", lambda task_id: tmp_path / task_id)
    return store, record.id, directory


def _install(monkeypatch, callback=None):
    factory = _RenderFactory(callback)
    registry = PluginRegistry()
    registry.register(factory.manifest, factory)
    manager = PluginManager(registry)
    monkeypatch.setattr(subtitle_reburn, "get_plugin_manager", lambda: manager)
    return factory


def _files(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def test_saved_slot_runs_once_and_commits_formal_metadata(task, monkeypatch):
    store, task_id, directory = task
    frozen = store.get(task_id).plugin_plan_json
    factory = _install(monkeypatch)
    result = subtitle_reburn.reburn_task(store, task_id, mode="soft")
    assert result == directory / "output_soft.mp4"
    assert result.read_bytes() == MP4
    request, config, context = factory.calls[0]
    assert len(factory.calls) == factory.closed == 1
    assert request.output_video.name.startswith(".reburn-")
    assert request.output_video != result
    assert config == {"mode": "soft"}
    record = store.get(task_id)
    assert (record.status, record.burn, record.output_video) == ("SUCCESS", "soft", result.name)
    assert record.plugin_plan_json == frozen
    assert record.tts_status == "PENDING"
    assert record.output_dubbed_video is None
    assert (directory / "output_hard.mp4").read_bytes() == MP4 + b"old-output"
    assert (directory / "output_dubbed.mp4").read_bytes() == MP4 + b"old-dub"
    assert (directory / "dubbed.wav").read_bytes() == b"old-speech"
    assert not (directory / "tts.plugin-meta.json").exists()
    assert not (directory / "render.dub_mux.plugin-meta.json").exists()
    assert not list(directory.glob(".reburn-*"))
    metadata = json.loads((directory / "render.subtitle.plugin-meta.json").read_text())
    assert metadata == AssetResolver.plugin_metadata(
        task_id=task_id, stage="render", operation="subtitle", purpose="render.subtitle",
        plugin_id="test.saved-render", implementation_version="7", config={"mode": "soft"},
        inputs={"source_video": directory / "source.mp4", "translated_srt": directory / "translated.srt"},
        output_names=("output_soft.mp4",),
    )
    context.report_progress(99)  # Manager suppresses callbacks after session completion.
    with pytest.raises(PluginCancelled):
        context.check_cancelled()


def test_reburn_metadata_includes_existing_producer_provenance(task, monkeypatch):
    store, task_id, directory = task
    _install(monkeypatch)
    for purpose in ("download", "translate"):
        (directory / f"{purpose}.plugin-meta.json").write_text(f"{purpose}-producer")
    subtitle_reburn.reburn_task(store, task_id)
    assert AssetResolver.plugin_cache_matches(
        task_id, stage="render", operation="subtitle", purpose="render.subtitle",
        plugin_id="test.saved-render", implementation_version="7", config={"mode": "hard"},
        inputs={"source_video": directory / "source.mp4", "translated_srt": directory / "translated.srt"},
        output_names=("output_hard.mp4",),
        upstream_purposes={"source_video": "download", "translated_srt": "translate"},
    )


def test_cancelled_terminal_task_can_reburn_despite_old_cancel_signal(task, monkeypatch):
    from src.service.orchestrator import is_cancelled_signal, set_cancelled_signal

    store, task_id, directory = task
    store.update(task_id, status="CANCELLED", progress=85, current_step="BURNING")
    set_cancelled_signal(task_id)
    _install(monkeypatch)
    subtitle_reburn.reburn_task(store, task_id)
    assert store.get(task_id).status == "SUCCESS"
    assert not is_cancelled_signal(task_id)


def test_null_plan_uses_historical_builtin_without_migration(task, monkeypatch):
    from src.core import subtitle_burner

    store, task_id, directory = task
    store.update(task_id, plugin_plan_json=None)
    monkeypatch.setenv("SUBTRANS_PIPELINE_PLUGINS", '{"render":{"plugin_id":"missing","implementation_version":"9"}}')
    calls = []

    def burn(video, srt, tid, *, output_path=None, **kwargs):
        calls.append((video, srt, tid, output_path))
        output_path.write_bytes(MP4)
        return subtitle_burner.BurnResult(output_path, kwargs["mode"], len(MP4))

    def migrate(*args, **kwargs):
        pytest.fail("reburn must never materialize a full plugin plan")

    monkeypatch.setattr(subtitle_burner, "burn_subtitles", burn)
    monkeypatch.setattr("src.service.plugin_plan.ensure_plan", migrate)
    monkeypatch.setattr("src.service.runner.enqueue_pipeline", migrate)
    subtitle_reburn.reburn_task(store, task_id)
    assert len(calls) == 1
    assert store.get(task_id).plugin_plan_json is None
    assert json.loads((directory / "render.subtitle.plugin-meta.json").read_text())["plugin_id"] == "builtin.ffmpeg_render"


@pytest.mark.parametrize("plan", ["", "{broken", "null", '{"schema_version":1,"slots":{}}', _plan(mode="invalid")])
def test_bad_plan_fails_without_touching_old_outputs(task, monkeypatch, plan):
    store, task_id, directory = task
    store.update(task_id, plugin_plan_json=plan)
    factory = _install(monkeypatch)
    before = _files(directory)
    with pytest.raises(subtitle_reburn.SubtitleOperationError, match="render.subtitle"):
        subtitle_reburn.reburn_task(store, task_id)
    assert factory.calls == []
    assert _files(directory) == before
    assert store.get(task_id).status == "SUCCESS"
    assert store.get(task_id).plugin_plan_json == plan


@pytest.mark.parametrize("plugin_id,version,code", [
    ("test.unknown", "7", "plugin_unavailable"),
    ("test.saved-render", "8", "version_mismatch"),
])
def test_saved_identity_is_not_replaced_by_default(task, monkeypatch, plugin_id, version, code):
    store, task_id, directory = task
    store.update(task_id, plugin_plan_json=_plan(plugin_id, version))
    _install(monkeypatch)
    before = _files(directory)
    with pytest.raises(PluginError) as caught:
        subtitle_reburn.reburn_task(store, task_id)
    assert caught.value.code == code
    assert _files(directory) == before
    assert store.get(task_id).status == "SUCCESS"


@pytest.mark.parametrize("failure", ["exception", "empty", "not_mp4", "wrong_path", "cancel"])
def test_render_failures_preserve_all_published_files(task, monkeypatch, failure):
    store, task_id, directory = task
    before = _files(directory)

    def fail(request, context):
        request.output_video.write_bytes(MP4)
        if failure == "exception":
            raise RuntimeError("renderer failed")
        if failure == "empty":
            request.output_video.write_bytes(b"")
        if failure == "not_mp4":
            request.output_video.write_bytes(b"not a video")
        if failure == "wrong_path":
            return SubtitleRenderResult(directory / "output_hard.mp4")
        if failure == "cancel":
            from src.service.orchestrator import set_cancelled_signal
            set_cancelled_signal(task_id)
        return SubtitleRenderResult(request.output_video)

    factory = _install(monkeypatch, fail)
    with pytest.raises((PluginError, PluginCancelled)):
        subtitle_reburn.reburn_task(store, task_id)
    assert factory.closed == 1
    assert _files(directory) == before
    current = store.get(task_id)
    assert (current.status, current.progress, current.current_step) == ("SUCCESS", 100, None)
    assert current.output_video == "output_hard.mp4"
    assert current.tts_status == "SUCCESS"


@pytest.mark.parametrize("failure", ["metadata", "db_exception", "db_rejected"])
def test_publication_failure_restores_video_and_metadata(task, monkeypatch, failure):
    store, task_id, directory = task
    before = _files(directory)
    _install(monkeypatch)
    original_finish = store.finish_operation
    original_write = AssetResolver.write_plugin_cache_metadata

    def write_then_fail(task_id, metadata):
        original_write(task_id, metadata)
        raise OSError("metadata failed after replacement")

    def finish(task_id, token, **fields):
        if "output_video" in fields:
            if failure == "db_rejected":
                return None
            raise OSError("database write failed")
        return original_finish(task_id, token, **fields)

    if failure == "metadata":
        monkeypatch.setattr(AssetResolver, "write_plugin_cache_metadata", write_then_fail)
    else:
        monkeypatch.setattr(store, "finish_operation", finish)
    with pytest.raises((OSError, PluginCancelled)):
        subtitle_reburn.reburn_task(store, task_id)
    assert _files(directory) == before
    record = store.get(task_id)
    assert (record.status, record.current_step, record.progress) == ("SUCCESS", None, 100)
    assert record.output_dubbed_video == "output_dubbed.mp4"


def test_reburn_uses_saved_ffmpeg_settings_scope(task, monkeypatch):
    store, task_id, directory = task
    snapshot = {"business": {"ffmpeg_bin": "/saved/ffmpeg", "ffprobe_bin": "/saved/ffprobe"}}
    store.update(task_id, runtime_config_json=json.dumps(snapshot))
    seen = []

    def render(request, context):
        seen.append((settings.ffmpeg_bin, settings.ffprobe_bin))
        request.output_video.write_bytes(MP4)
        return SubtitleRenderResult(request.output_video)

    _install(monkeypatch, render)
    subtitle_reburn.reburn_task(store, task_id)
    assert seen == [("/saved/ffmpeg", "/saved/ffprobe")]
    assert settings.ffmpeg_bin != "/saved/ffmpeg"


@pytest.mark.parametrize("status", ["PENDING", "DOWNLOADING", "TRANSLATING", "BURNING", "SYNTHESIZING", "DUBBING"])
def test_active_tasks_cannot_reburn(task, monkeypatch, status):
    store, task_id, directory = task
    store.update(task_id, status=status)
    factory = _install(monkeypatch)
    before = _files(directory)
    with pytest.raises(subtitle_reburn.SubtitleOperationError):
        subtitle_reburn.reburn_task(store, task_id)
    assert not factory.calls
    assert _files(directory) == before
    assert store.get(task_id).status == status


def test_claim_lost_to_other_worker_does_not_invoke_render(task, monkeypatch):
    store, task_id, directory = task
    factory = _install(monkeypatch)
    real_claim = store.claim_operation

    def claim(task_id, **kwargs):
        real_claim(task_id, operation="subtitle_edit")
        return None

    monkeypatch.setattr(store, "claim_operation", claim)
    with pytest.raises(subtitle_reburn.SubtitleOperationError, match="正在处理"):
        subtitle_reburn.reburn_task(store, task_id)
    assert not factory.calls
    assert store.get(task_id).current_step.startswith("subtitle_edit:")
    assert not list(directory.glob(".reburn-*"))


def test_recover_interrupted_publication_restores_old_files_and_previous_state(task):
    store, task_id, directory = task
    previous, token = store.claim_operation(task_id)
    output = directory / "output_hard.mp4"
    metadata = directory / "render.subtitle.plugin-meta.json"
    before = _files(directory)
    temporary = directory / (".reburn-" + "a" * 32 + ".mp4")
    temporary.write_bytes(MP4 + b"unfinished")
    subtitle_reburn.SubtitlePublication(
        directory, (output, metadata), record=previous, token=token,
        temporary_names=(temporary.name,),
    )
    replacement = directory / ".replacement"
    replacement.write_bytes(MP4 + b"new-output")
    replacement.replace(output)
    replacement.write_text("new-metadata")
    replacement.replace(metadata)
    restored = subtitle_reburn.recover_subtitle_publication(task_id, directory, expected_token=token)
    assert restored == {"token": token, "previous": {"status": "SUCCESS", "progress": 100, "current_step": None}}
    assert _files(directory) == before
    assert subtitle_reburn.recover_subtitle_publication(task_id, directory, expected_token=token) is None


def test_recover_interrupted_subtitle_save_uses_same_journal(task):
    store, task_id, directory = task
    previous, token = store.claim_operation(task_id, operation="subtitle_edit")
    target = directory / "translated.v2.srt"
    subtitle_reburn.SubtitlePublication(directory, (target,), record=previous, token=token)
    target.write_text("partial subtitle")
    restored = subtitle_reburn.recover_subtitle_publication(task_id, directory, expected_token=token)
    assert restored["previous"]["status"] == "SUCCESS"
    assert not target.exists()
    assert not (directory / ".subtitle-publication.json").exists()


@pytest.mark.parametrize("damage", ["path_escape", "backup_escape", "backup_symlink", "target_symlink", "token", "task_id"])
def test_recovery_rejects_untrusted_journal_and_keeps_backups(task, damage):
    store, task_id, directory = task
    previous, token = store.claim_operation(task_id)
    output = directory / "output_hard.mp4"
    subtitle_reburn.SubtitlePublication(directory, (output,), record=previous, token=token)
    journal_path = directory / ".subtitle-publication.json"
    journal = json.loads(journal_path.read_text())
    outside = directory.parent / "outside.mp4"
    outside.write_bytes(b"do-not-touch")
    if damage == "path_escape":
        journal["files"]["../outside.mp4"] = None
    elif damage == "backup_escape":
        journal["files"][output.name] = "../outside.mp4"
    elif damage == "backup_symlink":
        backup = directory / journal["files"][output.name]
        backup.unlink()
        backup.symlink_to(outside)
    elif damage == "target_symlink":
        output.unlink()
        output.symlink_to(outside)
    elif damage == "token":
        journal["token"] = "reburn:" + "f" * 32
    else:
        journal["task_id"] = "another-task"
    journal_path.write_text(json.dumps(journal))
    with pytest.raises(subtitle_reburn.SubtitleOperationError):
        subtitle_reburn.recover_subtitle_publication(task_id, directory, expected_token=token)
    assert outside.read_bytes() == b"do-not-touch"
    assert journal_path.is_file()
    assert list(directory.glob(".reburn-backup-*"))


def test_discard_committed_journal_keeps_new_output_and_allows_next_reburn(task, monkeypatch):
    store, task_id, directory = task
    previous, token = store.claim_operation(task_id)
    output = directory / "output_hard.mp4"
    subtitle_reburn.SubtitlePublication(directory, (output,), record=previous, token=token)
    replacement = directory / ".replacement"
    replacement.write_bytes(MP4 + b"committed")
    replacement.replace(output)
    store.finish_operation(task_id, token, status="SUCCESS", current_step=None, progress=100)
    assert subtitle_reburn.discard_subtitle_publication(task_id, directory)
    assert output.read_bytes() == MP4 + b"committed"
    assert not list(directory.glob(".reburn-backup-*"))
    assert not subtitle_reburn.discard_subtitle_publication(task_id, directory)
    _install(monkeypatch)
    assert subtitle_reburn.reburn_task(store, task_id) == output


def test_discard_refuses_invalid_journal_without_deleting_output(task):
    store, task_id, directory = task
    journal = directory / ".subtitle-publication.json"
    journal.write_text('{"task_id":"forged"}')
    before = _files(directory)
    with pytest.raises(subtitle_reburn.SubtitleOperationError):
        subtitle_reburn.discard_subtitle_publication(task_id, directory)
    assert _files(directory) == before


def test_filesystem_rollback_failure_keeps_claim_and_journal_for_recovery(task, monkeypatch):
    store, task_id, directory = task
    _install(monkeypatch)
    original_finish = store.finish_operation
    original_restore = subtitle_reburn.SubtitlePublication.restore

    def fail_commit(task_id, token, **fields):
        if "output_video" in fields:
            raise OSError("database full")
        return original_finish(task_id, token, **fields)

    def fail_restore(self):
        raise OSError("volume unavailable")

    monkeypatch.setattr(store, "finish_operation", fail_commit)
    monkeypatch.setattr(subtitle_reburn.SubtitlePublication, "restore", fail_restore)
    with pytest.raises(OSError, match="volume unavailable"):
        subtitle_reburn.reburn_task(store, task_id)
    record = store.get(task_id)
    assert record.status == "BURNING"
    assert record.current_step.startswith("reburn:")
    assert (directory / ".subtitle-publication.json").exists()
    monkeypatch.setattr(subtitle_reburn.SubtitlePublication, "restore", original_restore)
    restored = subtitle_reburn.recover_subtitle_publication(task_id, directory, expected_token=record.current_step)
    assert restored["previous"]["status"] == "SUCCESS"
    assert (directory / "output_hard.mp4").read_bytes() == MP4 + b"old-output"


def test_restore_can_be_retried_after_partial_recovery(task, monkeypatch):
    store, task_id, directory = task
    previous, token = store.claim_operation(task_id)
    output = directory / "output_hard.mp4"
    metadata = directory / "render.subtitle.plugin-meta.json"
    before = _files(directory)
    subtitle_reburn.SubtitlePublication(directory, (output, metadata), record=previous, token=token)
    original_replace = subtitle_reburn.os.replace
    count = 0

    def fail_second(source, target):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("interrupted restore")
        return original_replace(source, target)

    monkeypatch.setattr(subtitle_reburn.os, "replace", fail_second)
    with pytest.raises(subtitle_reburn.SubtitleOperationError):
        subtitle_reburn.recover_subtitle_publication(task_id, directory, expected_token=token)
    assert (directory / ".subtitle-publication.json").exists()
    assert len(list(directory.glob(".reburn-backup-*"))) == 2
    monkeypatch.setattr(subtitle_reburn.os, "replace", original_replace)
    assert subtitle_reburn.recover_subtitle_publication(task_id, directory, expected_token=token)
    assert _files(directory) == before


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="requires local FFmpeg")
def test_local_ffmpeg_reburn_smoke_preserves_old_output_until_commit(task, monkeypatch):
    from src.core import subtitle_burner

    store, task_id, directory = task
    source = directory / "source.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=160x120:r=10:d=1",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest",
        "-c:v", "mpeg4", "-c:a", "aac", str(source),
    ], check=True, capture_output=True, timeout=30)
    store.update(task_id, plugin_plan_json=None, burn="soft", output_video="output_soft.mp4")
    output = directory / "output_soft.mp4"
    output.write_bytes(MP4 + b"previous-soft-video")
    before = output.read_bytes()
    monkeypatch.setattr(subtitle_burner, "ensure_task_dir", lambda tid: directory)
    burn = subtitle_burner.burn_subtitles
    seen = []

    def checked_burn(*args, **kwargs):
        assert output.read_bytes() == before
        result = burn(*args, **kwargs)
        assert output.read_bytes() == before
        assert result.output_path.name.startswith(".reburn-")
        seen.append(result.output_path)
        return result

    monkeypatch.setattr(subtitle_burner, "burn_subtitles", checked_burn)
    assert subtitle_reburn.reburn_task(store, task_id) == output
    assert seen and output.read_bytes() != before
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "s", "-show_entries", "stream=codec_name",
        "-of", "csv=p=0", str(output),
    ], check=True, capture_output=True, text=True, timeout=15)
    assert probe.stdout.strip() == "mov_text"
    assert store.get(task_id).status == "SUCCESS"
    assert not list(directory.glob(".reburn-*"))


def test_save_and_second_reburn_rejected_while_render_is_running(task, monkeypatch):
    from fastapi import HTTPException
    from src.handler import subtitle_editor

    store, task_id, directory = task
    entered, release = threading.Event(), threading.Event()
    errors = []

    def render(request, context):
        entered.set()
        assert release.wait(5)
        request.output_video.write_bytes(MP4)
        return SubtitleRenderResult(request.output_video)

    _install(monkeypatch, render)
    monkeypatch.setattr(subtitle_editor, "task_dir", lambda tid: directory)

    def work():
        try:
            subtitle_reburn.reburn_task(store, task_id)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(subtitle_reburn.SubtitleOperationError):
            subtitle_reburn.reburn_task(store, task_id)
        body = subtitle_editor.SubtitleDocument(locale="translated", entries=[{
            "index": 1, "start": 0, "end": 1, "text": "cannot interleave",
        }])
        with pytest.raises(HTTPException) as caught:
            subtitle_editor.save_subtitles(task_id, body, store)
        assert caught.value.status_code == 409
        assert (directory / "translated.srt").read_text() == SRT
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert errors == []
