from __future__ import annotations

import builtins
import importlib.util
import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import os
import unittest
from unittest.mock import patch

import pytest

from src.desktop import models


WHISPER_REVISION = "a" * 40
TRANSLATION_REVISION = "b" * 40


class FakeWhisper:
    def __init__(self, events, *, installed=False):
        self.events = events
        self.state = "READY" if installed else "NOT_INSTALLED"
        self.revision = WHISPER_REVISION if installed else None
        self.steps = []
        self.downloads = []
        self.fail_next = False

    def list_models(self):
        if self.steps:
            self.state = self.steps.pop(0)
            if self.state == "READY":
                self.revision = WHISPER_REVISION
                self.events.append("whisper-ready")
        return [{
            "name": "base.en", "status": self.state,
            "phase": "checking" if self.state == "CHECKING" else self.state.lower(),
            "downloadedBytes": 42, "totalBytes": None,
            "error": "whisper download failed" if self.state == "ERROR" else None,
            "revision": self.revision,
        }]

    def download(self, name, *, revision, endpoint):
        self.downloads.append((name, revision, endpoint))
        self.events.append("whisper-start")
        self.state = "DOWNLOADING"
        self.steps = ["DOWNLOADING", "CHECKING", "ERROR" if self.fail_next else "READY"]
        self.fail_next = False
        return {"status": "DOWNLOADING"}


class FakeTranslation:
    revision = TRANSLATION_REVISION
    endpoint = None

    def __init__(self, events, *, installed=False):
        self.events = events
        self.state = "READY" if installed else "NOT_INSTALLED"
        self.steps = []
        self.downloads = []
        self.dependencies_ready = True
        self.fail_validation = False
        self.installed_revision = TRANSLATION_REVISION if installed else None

    def status(self):
        if self.steps:
            self.state = self.steps.pop(0)
            if self.state == "READY":
                self.installed_revision = self.revision
                self.events.append("translation-ready")
        return {
            "model_status": self.state, "phase": self.state.lower(),
            "downloaded_bytes": 84, "total_bytes": 100,
            "installed_bytes": 123, "model_error": None,
            "revision": self.installed_revision,
        }

    def start_download(self):
        self.events.append("translation-start")
        self.downloads.append(self.endpoint)
        self.state = "DOWNLOADING"
        self.steps = ["DOWNLOADING", "CONVERTING", "CHECKING", "READY"]
        return {"model_status": "DOWNLOADING"}

    def dependency_status(self):
        return {"ready": self.dependencies_ready, "missing": [] if self.dependencies_ready else ["sentencepiece"]}

    def validate_offline(self):
        self.events.append("translation-validate")
        if self.fail_validation:
            self.state = "FAILED"
            raise RuntimeError("offline smoke test failed")


def make_manager(*, installed=False, **kwargs):
    events = []
    whisper = FakeWhisper(events, installed=installed)
    translation = FakeTranslation(events, installed=installed)
    options = {
        "official_revisions": {"base.en": WHISPER_REVISION},
        "translation_revision": TRANSLATION_REVISION,
        "dependency_check": lambda: {"ready": True},
        "whisper_validator": lambda manager: events.append("whisper-validate"),
        "poll_interval": 0.001, "max_poll_interval": 0.003,
        "prepare_timeout": 1,
    }
    options.update(kwargs)
    coordinator = models.DesktopModelManager(whisper, translation, **options)
    return coordinator, whisper, translation, events


def finish(coordinator):
    coordinator._worker.join(timeout=3)
    assert not coordinator._worker.is_alive(), "Preparation worker did not finish"
    return coordinator.status()


class DesktopModelsTests(unittest.TestCase):
    def setUp(self):
        self.patch_environment({"SUBTRANS_DESKTOP_MODEL_ENDPOINT": ""})

    def patch_object(self, target, attribute, value):
        patcher = patch.object(target, attribute, value)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def patch_environment(self, values):
        patcher = patch.dict(os.environ, values)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_default_whisper_validation_is_offline_and_releases_runtime(self):
        calls, references = [], []

        class FakeRuntime:
            def __init__(self, path, **kwargs):
                calls.append((path, kwargs))
                references.append(weakref.ref(self))

        with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=FakeRuntime)}):
            manager = SimpleNamespace(resolve_path=lambda name: "/fake/" + name)
            models._validate_whisper(manager)
        assert calls == [("/fake/base.en", {
            "device": "cpu", "compute_type": "int8", "local_files_only": True,
        })]
        assert references[0]() is None

    def test_phase_mapping_and_unknown_transfer_size(self):
        coordinator, whisper, translation, _ = make_manager()
        self.patch_object(whisper, "list_models", lambda: [{
            "name": "base.en", "status": "DOWNLOADING", "phase": "checking",
            "downloadedBytes": 42, "totalBytes": None,
        }])
        self.patch_object(translation, "status", lambda: {
            "model_status": "CONVERTING", "downloaded_bytes": None,
            "installed_bytes": 999, "total_bytes": None,
        })
        assert coordinator._read_model(0)["status"] == "checking"
        converted = coordinator._read_model(1)
        assert converted["status"] == converted["phase"] == "converting"
        assert converted["downloaded_bytes"] is None
        assert converted["total_bytes"] is None

    def test_endpoint_is_not_changed_during_existing_translation_download(self):
        coordinator, whisper, translation, _ = make_manager(endpoint="https://models.example.org")
        translation.state = "DOWNLOADING"
        coordinator.start_prepare()
        result = finish(coordinator)
        assert result["state"] == "failed"
        assert "active translation download" in result["error"]
        assert translation.endpoint is None
        assert not whisper.downloads and not translation.downloads

    def test_defaults_are_exact_and_independent(self):
        expected = {
            "sourceLang": "en", "targetLang": "zh-CN", "mode": "bilingual",
            "burn": "hard", "model": "local:base.en", "engine": "local-opus-en-zh",
            "needSubtitle": True, "ttsEnabled": False,
        }
        assert models.desktop_task_defaults() == expected
        models.desktop_task_defaults()["model"] = "wrong"
        assert models.desktop_task_defaults() == expected

    def test_import_and_constructor_have_no_service_or_runtime_side_effects(self):
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            assert not name.startswith(("src.config", "src.service", "faster_whisper", "transformers"))
            return original_import(name, *args, **kwargs)

        self.patch_object(builtins, "__import__", guarded_import)
        spec = importlib.util.spec_from_file_location("isolated_desktop_models", Path(models.__file__))
        isolated = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(isolated)
        coordinator = isolated.DesktopModelManager()
        assert coordinator.status()["state"] == "not_started"
        assert coordinator._worker is None

    def test_serial_async_preparation_pins_endpoint_and_progress(self):
        coordinator, whisper, translation, events = make_manager(endpoint="https://models.example.org")
        assert coordinator.start_prepare()["state"] == "preparing"
        result = finish(coordinator)
        assert result["state"] == "ready" and result["ready"] is True
        assert whisper.downloads == [("base.en", WHISPER_REVISION, "https://models.example.org")]
        assert translation.downloads == ["https://models.example.org"]
        assert events.index("whisper-ready") < events.index("translation-start")
        assert result["models"][0]["downloaded_bytes"] == 42
        assert result["models"][0]["total_bytes"] is None
        assert result["models"][1]["downloaded_bytes"] == 84
        assert result["models"][1]["total_bytes"] == 100
        assert all(row["status"] == "ready" for row in result["models"])
        assert result["models"][0]["name"] == "英文转写 · Whisper base.en"
        # Owners validate before publishing fresh installs; no second runtime load.
        assert not any(event.endswith("-validate") for event in events)
        assert coordinator.start_prepare()["state"] == "preparing"
        assert finish(coordinator)["ready"]
        assert len(whisper.downloads) == len(translation.downloads) == 1

    def test_cached_status_and_parallel_starts_do_not_block_on_manager_io(self):
        coordinator, whisper, translation, _ = make_manager()
        entered, release = threading.Event(), threading.Event()
        original = whisper.list_models

        def blocked_inventory():
            entered.set()
            assert release.wait(2)
            return original()

        self.patch_object(whisper, "list_models", blocked_inventory)
        try:
            coordinator.start_prepare()
            assert entered.wait(1)
            with ThreadPoolExecutor(max_workers=8) as pool:
                replies = list(pool.map(lambda _: coordinator.start_prepare(), range(32)))
            assert all(reply["state"] == "preparing" for reply in replies)
            snapshot = coordinator.status()
            snapshot["models"][0]["status"] = "tampered"
            assert coordinator.status()["models"][0]["status"] != "tampered"
            assert not translation.downloads
        finally:
            release.set()
            finish(coordinator)
        assert len(whisper.downloads) == len(translation.downloads) == 1

    def test_installed_models_require_offline_validation_before_ready(self):
        entered, release = threading.Event(), threading.Event()

        def validate(_manager):
            entered.set()
            assert release.wait(2)

        coordinator, whisper, translation, events = make_manager(installed=True, whisper_validator=validate)
        try:
            coordinator.start_prepare()
            assert entered.wait(1)
            snapshot = coordinator.status()
            assert not snapshot["ready"]
            assert snapshot["models"][0]["status"] == "checking"
            assert "translation-validate" not in events
        finally:
            release.set()
            result = finish(coordinator)
        assert result["ready"]
        assert not whisper.downloads and not translation.downloads
        assert events == ["translation-validate"]

    def test_download_failure_stops_sequence_and_retries(self):
        coordinator, whisper, translation, _ = make_manager()
        whisper.fail_next = True
        coordinator.start_prepare()
        result = finish(coordinator)
        assert result["state"] == "failed" and not result["ready"]
        assert result["models"][0]["status"] == "failed"
        assert "whisper download failed" in result["error"]
        assert not translation.downloads
        coordinator.start_prepare()
        assert finish(coordinator)["ready"]
        assert len(whisper.downloads) == 2
        assert len(translation.downloads) == 1

    def test_offline_failure_can_retry_without_redownloading_ready_whisper(self):
        coordinator, whisper, translation, events = make_manager(installed=True)
        translation.fail_validation = True
        coordinator.start_prepare()
        result = finish(coordinator)
        assert result["state"] == "failed"
        assert result["models"][0]["status"] == "ready"
        assert result["models"][1]["status"] == "failed"
        assert "offline smoke test failed" in result["error"]
        translation.fail_validation = False
        coordinator.start_prepare()
        assert finish(coordinator)["ready"]
        assert not whisper.downloads
        assert len(translation.downloads) == 1

    def test_unpinned_whisper_never_downloads_or_becomes_ready(self):
        for revision in [None, 'main', '', 'abc123']:
            with self.subTest(revision=revision):
                coordinator, whisper, translation, _ = make_manager(official_revisions={"base.en": revision})
                coordinator.start_prepare()
                result = finish(coordinator)
                assert result["state"] == "failed"
                assert "OFFICIAL_MODEL_REVISIONS" in result["error"]
                assert not whisper.downloads and not translation.downloads

    def test_translation_revision_mismatch_is_rejected(self):
        coordinator, whisper, translation, _ = make_manager()
        translation.revision = "main"
        coordinator.start_prepare()
        assert "pinned revision" in finish(coordinator)["error"]
        assert not whisper.downloads and not translation.downloads

    def test_legacy_whisper_without_revision_is_repaired_with_pin(self):
        coordinator, whisper, _, _ = make_manager(installed=True)
        whisper.revision = None
        coordinator.start_prepare()
        assert finish(coordinator)["ready"]
        assert whisper.downloads == [("base.en", WHISPER_REVISION, None)]

    def test_manager_cannot_claim_ready_without_installed_revision(self):
        coordinator, whisper, translation, _ = make_manager(installed=True)
        whisper.revision = None
        self.patch_object(whisper, "download", lambda *args, **kwargs: {"status": "READY"})
        coordinator.start_prepare()
        result = finish(coordinator)
        assert result["state"] == "failed"
        assert "revision is missing" in result["error"]
        assert not translation.downloads

    def test_missing_dependencies_prevent_ready_and_allow_retry(self):
        for translation_missing in [True, False]:
            with self.subTest(translation_missing=translation_missing):
                dependency_state = {"ready": translation_missing, "missing": ["FFmpeg subtitles/libass"]}
                coordinator, whisper, translation, _ = make_manager(
                    installed=True, dependency_check=lambda: dependency_state,
                )
                translation.dependencies_ready = not translation_missing
                coordinator.start_prepare()
                result = finish(coordinator)
                assert result["state"] == "failed"
                assert "dependencies unavailable" in result["error"]
                assert not whisper.downloads and not translation.downloads
                dependency_state["ready"] = translation.dependencies_ready = True
                coordinator.start_prepare()
                assert finish(coordinator)["ready"]

    def test_dependency_loss_during_validation_is_not_ready(self):
        coordinator, _, translation, _ = make_manager(installed=True)
        coordinator._whisper_validator = lambda _: setattr(translation, "dependencies_ready", False)
        coordinator.start_prepare()
        result = finish(coordinator)
        assert result["state"] == "failed" and not result["ready"]
        assert "sentencepiece" in result["error"]

    def test_external_in_progress_download_is_observed_not_duplicated(self):
        coordinator, whisper, translation, events = make_manager()
        whisper.state = "DOWNLOADING"
        whisper.steps = ["DOWNLOADING", "CHECKING", "READY"]
        coordinator.start_prepare()
        assert finish(coordinator)["ready"]
        assert not whisper.downloads
        assert events.index("whisper-validate") < events.index("translation-start")
        assert len(translation.downloads) == 1

    def test_timeout_then_retry_observes_existing_download(self):
        coordinator, whisper, translation, _ = make_manager(prepare_timeout=0.008)
        whisper.state = "DOWNLOADING"
        coordinator.start_prepare()
        result = finish(coordinator)
        assert result["state"] == "failed"
        assert "timed out" in result["error"]
        assert not whisper.downloads and not translation.downloads
        whisper.steps = ["DOWNLOADING", "READY"]
        coordinator.start_prepare()
        assert finish(coordinator)["ready"]
        assert not whisper.downloads

    def test_backoff_is_bounded(self):
        delays = []
        self.patch_object(models.time, "sleep", delays.append)
        coordinator, _, _, _ = make_manager(poll_interval=0.01, max_poll_interval=0.02)
        coordinator.start_prepare()
        assert finish(coordinator)["ready"]
        assert len(delays) == 5
        for actual, expected in zip(delays, [0.01, 0.015, 0.01, 0.015, 0.02]):
            self.assertAlmostEqual(actual, expected)

    def test_endpoint_validation_does_not_leak_credentials(self):
        for endpoint in ['http://example.org', 'https://user:secret@example.org', 'https://example.org?token=secret']:
            with self.subTest(endpoint=endpoint):
                coordinator, whisper, _, _ = make_manager(endpoint=endpoint)
                coordinator.start_prepare()
                result = finish(coordinator)
                assert result["state"] == "failed"
                assert "secret" not in result["error"]
                assert not whisper.downloads

    def test_endpoint_environment_forwarded_to_both_managers(self):
        self.patch_environment({"SUBTRANS_DESKTOP_MODEL_ENDPOINT": "https://models.example.org"})
        coordinator, whisper, translation, _ = make_manager()
        coordinator.start_prepare()
        assert finish(coordinator)["ready"]
        assert whisper.downloads[0][2] == translation.downloads[0] == "https://models.example.org"

    def test_singleton_creation_is_thread_safe_and_does_not_start_work(self):
        self.patch_object(models, "_manager", None)
        with ThreadPoolExecutor(max_workers=8) as pool:
            instances = list(pool.map(lambda _: models.get_desktop_model_manager(), range(32)))
        assert all(instance is instances[0] for instance in instances)
        assert instances[0].status()["state"] == "not_started"
        assert instances[0]._worker is None


if __name__ == "__main__":
    unittest.main()


def test_passive_health_refresh_detects_removal_without_downloading():
    coordinator, whisper, translation, _ = make_manager(installed=True)
    coordinator.start_prepare()
    assert finish(coordinator)["ready"]
    whisper.state = "NOT_INSTALLED"
    coordinator._checked_at = float("-inf")
    assert not coordinator.status()["ready"]
    assert finish(coordinator)["state"] == "failed"
    assert whisper.downloads == translation.downloads == []
    coordinator.start_prepare()
    assert finish(coordinator)["ready"]
    assert len(whisper.downloads) == 1
    assert translation.downloads == []


def test_ready_dependency_loss_and_retry_without_redownload():
    coordinator, whisper, translation, _ = make_manager(installed=True)
    coordinator.start_prepare()
    assert finish(coordinator)["ready"]
    translation.dependencies_ready = False
    coordinator._checked_at = float("-inf")
    coordinator.status()
    assert finish(coordinator)["state"] == "failed"
    translation.dependencies_ready = True
    coordinator.start_prepare()
    assert finish(coordinator)["ready"]
    assert whisper.downloads == translation.downloads == []


def test_expired_status_does_not_block_on_dependencies_or_trigger_downloads():
    coordinator, whisper, translation, _ = make_manager(installed=True)
    coordinator.start_prepare()
    assert finish(coordinator)["ready"]
    entered, release = threading.Event(), threading.Event()

    def dependencies():
        entered.set()
        assert release.wait(3)
        return {"ready": True}

    coordinator._dependency_check = dependencies
    coordinator._checked_at = float("-inf")
    try:
        assert not coordinator.status()["ready"]
        assert entered.wait(1)
        worker = coordinator._worker
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert all(not row["ready"] for row in pool.map(lambda _: coordinator.status(), range(32)))
        coordinator.start_prepare()
        assert coordinator._worker is worker
        assert whisper.downloads == translation.downloads == []
    finally:
        release.set()
        assert finish(coordinator)["ready"]


def test_whisper_load_failure_is_invalidated_for_retry(monkeypatch):
    coordinator, whisper, translation, _ = make_manager(installed=True)

    def failed(name, error):
        assert name == "base.en"
        whisper.state = "ERROR"

    monkeypatch.setattr(whisper, "mark_failed", failed, raising=False)
    coordinator._whisper_validator = lambda _: (_ for _ in ()).throw(RuntimeError("bad weights"))
    coordinator.start_prepare()
    assert finish(coordinator)["state"] == "failed"
    assert whisper.state == "ERROR"
    coordinator._whisper_validator = lambda _: None
    coordinator.start_prepare()
    assert finish(coordinator)["ready"]
    assert len(whisper.downloads) == 1


def test_install_guard_checks_active_tasks_under_shared_admission_lock(monkeypatch):
    from src.desktop import lifecycle
    monkeypatch.setenv("SUBTRANS_DESKTOP", "1")
    monkeypatch.setattr(lifecycle, "active_task_count", lambda: 1)
    import pytest
    with pytest.raises(RuntimeError, match="任务正在处理"):
        with models.model_install_guard():
            pytest.fail("publication must not run")
    monkeypatch.setattr(lifecycle, "active_task_count", lambda: 0)
    with models.model_install_guard():
        assert not models.model_mutation_lock.acquire(blocking=False)
    assert models.model_mutation_lock.acquire(blocking=False)
    models.model_mutation_lock.release()


def test_real_managers_integrate_revalidation_and_retry_without_live_weights(tmp_path, monkeypatch):
    from src.desktop import lifecycle
    from src.service import model_manager as whisper_module
    from src.service import translation_model_manager as translation_module
    from test_desktop_whisper_models import Manager, install_fakes, PAYLOADS
    from test_desktop_translation_models import Converter, Tokenizer, Translator, injected_snapshot

    monkeypatch.setenv('SUBTRANS_DESKTOP', '1')
    monkeypatch.setattr(lifecycle, 'active_task_count', lambda: 0)
    whisper = Manager(tmp_path / 'whisper')
    calls = install_fakes(monkeypatch, whisper)
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=lambda *a, **kw: object()))
    translation = translation_module.TranslationModelManager(
        tmp_path / 'translation' / 'model', snapshot_download_fn=injected_snapshot,
        converter_factory=Converter, runtime_factory=lambda _: (Tokenizer(), Translator()),
    )
    monkeypatch.setattr(translation, 'dependency_status', lambda: {'ready': True})
    coordinator = models.DesktopModelManager(
        whisper, translation, official_revisions=whisper_module.OFFICIAL_MODEL_REVISIONS,
        translation_revision=translation_module.MODEL_REVISION,
        dependency_check=lambda: {'ready': True}, poll_interval=0.001,
        max_poll_interval=0.003, prepare_timeout=2,
    )
    try:
        coordinator.start_prepare()
        assert finish(coordinator)['ready']
        (whisper.root / 'base.en' / 'model.bin').write_bytes(b'!' * len(PAYLOADS['model.bin']))
        (translation.model_dir / translation_module._PROVENANCE).unlink()
        coordinator._checked_at = float('-inf')
        coordinator.status()
        assert finish(coordinator)['state'] == 'failed'
        assert [kind for kind, *_ in calls].count('snapshot') == 1
        coordinator.start_prepare()
        assert finish(coordinator)['ready']
        assert [kind for kind, *_ in calls].count('snapshot') == 2
        assert translation.status()['revision'] == translation_module.MODEL_REVISION
        # Already ready is revalidated, but no weights are fetched again.
        coordinator.start_prepare()
        assert finish(coordinator)['ready']
        assert [kind for kind, *_ in calls].count('snapshot') == 2
    finally:
        whisper._executor.shutdown(wait=True)
        translation._executor.shutdown(wait=True)


@pytest.mark.parametrize("installed", [False, True])
def test_public_revision_is_null_until_verified_then_reports_installed_pin(installed):
    coordinator, _, _, _ = make_manager(installed=installed)
    assert [row["revision"] for row in coordinator.status()["models"]] == [None, None]
    coordinator.start_prepare()
    result = finish(coordinator)
    assert result["ready"]
    assert [row["revision"] for row in result["models"]] == [WHISPER_REVISION, TRANSLATION_REVISION]
    result["models"][0]["revision"] = "tampered"
    assert coordinator.status()["models"][0]["revision"] == WHISPER_REVISION


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("revision", [None, "c" * 40])
def test_missing_or_incompatible_installed_revision_is_never_published(monkeypatch, index, revision):
    coordinator, whisper, translation, _ = make_manager(installed=True)
    if index == 0:
        whisper.revision = revision
        monkeypatch.setattr(whisper, "download", lambda *args, **kwargs: None)
    else:
        translation.installed_revision = revision
        monkeypatch.setattr(translation, "start_download", lambda: None)
    coordinator.start_prepare()
    result = finish(coordinator)
    assert result["state"] == "failed"
    assert result["models"][index]["revision"] is None
    assert "revision is missing" in result["error"]


def test_revision_not_published_during_or_after_failed_offline_validation():
    entered, release = threading.Event(), threading.Event()

    def validate(_):
        entered.set()
        assert release.wait(3)
        raise RuntimeError("invalid model weights")

    coordinator, _, _, _ = make_manager(installed=True, whisper_validator=validate)
    try:
        coordinator.start_prepare()
        assert entered.wait(1)
        assert coordinator.status()["models"][0]["revision"] is None
    finally:
        release.set()
        result = finish(coordinator)
    assert result["state"] == "failed"
    assert result["models"][0]["revision"] is None


@pytest.mark.parametrize("index", [0, 1])
def test_passive_refresh_revokes_revision_when_installed_provenance_changes(index):
    coordinator, whisper, translation, _ = make_manager(installed=True)
    coordinator.start_prepare()
    assert finish(coordinator)["ready"]
    if index == 0:
        whisper.revision = None
    else:
        translation.installed_revision = "c" * 40
    coordinator._checked_at = float("-inf")
    coordinator.status()
    result = finish(coordinator)
    assert result["state"] == "failed"
    assert result["models"][index]["revision"] is None
    assert not whisper.downloads and not translation.downloads
