"""Desktop admission/publication races, without models, network or processing."""

from __future__ import annotations

import builtins
import dataclasses
import importlib
import threading
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Follow the public facade's initialization order (deps/app/routes are cyclic).
from src.handler import tasks as compat
from src.handler.deps import get_store, get_translation_engine_store, require_api_token
from src.service.task import task_lifecycle
from src.store import TaskStore, TranslationEngineStore


class ObservedLock:
    """A real non-reentrant Lock with an event for a proven contended acquire."""

    def __init__(self):
        self.lock = threading.Lock()
        self.contended = threading.Event()

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            self.contended.set()
            assert self.lock.acquire(timeout=5), "admission/publication deadlock"
        return self

    def __exit__(self, *_):
        self.lock.release()

    def assert_held(self):
        acquired = self.lock.acquire(blocking=False)
        if acquired:
            self.lock.release()
        assert not acquired, "model snapshot or admission escaped publication lock"


def _start(callback):
    result = Future()

    def run():
        try:
            result.set_result(callback())
        except BaseException as exc:
            result.set_exception(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return result


@pytest.fixture
def admission(tmp_path, monkeypatch):
    models = importlib.import_module("src.desktop.models")
    lifecycle = importlib.import_module("src.desktop.lifecycle")
    lock = ObservedLock()
    monkeypatch.setattr(models, "model_mutation_lock", lock)
    store = TaskStore(tmp_path / "tasks.db")
    engines = TranslationEngineStore()
    engines.ensure_default_deepseek()
    engines.update("deepseek", api_key="test-placeholder", availability="AVAILABLE")
    state = {"state": "ready", "ready": True, "revision": "old"}
    reads, snapshots, queued = [], [], []

    def status():
        lock.assert_held()
        reads.append(state["revision"])
        return dict(state)

    monkeypatch.setattr(models, "get_desktop_model_manager", lambda: SimpleNamespace(status=status))
    monkeypatch.setattr(lifecycle, "active_task_count", lambda: sum(
        record.status not in {"SUCCESS", "FAILED", "CANCELLED"} for record in store.list()
    ))
    monkeypatch.setattr(compat, "_ensure_local_model_ready", lambda *_: None)
    monkeypatch.setattr(compat, "probe_duration", lambda *_: 10.0)
    monkeypatch.setattr(compat, "task_dir", lambda task_id: tmp_path / task_id)
    monkeypatch.setattr(compat, "settings", dataclasses.replace(compat.settings, data_dir=tmp_path))
    resolve_runtime = compat.resolve_runtime

    def resolve(**kwargs):
        if state.get("assert_locked", True):
            lock.assert_held()
        snapshots.append(state["revision"])
        return resolve_runtime(**kwargs)

    monkeypatch.setattr(compat, "resolve_runtime", resolve)
    monkeypatch.setattr(compat, "enqueue_pipeline", queued.append)
    app = FastAPI()
    app.include_router(task_lifecycle.router)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_translation_engine_store] = lambda: engines
    app.dependency_overrides[require_api_token] = lambda: None
    monkeypatch.setenv("SUBTRANS_DESKTOP", "1")
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, store=store, lock=lock, models=models, state=state,
            reads=reads, snapshots=snapshots, queued=queued, tmp=tmp_path,
        )


def _request(admission, kind, *, need_subtitle=True, **overrides):
    payload = dict(
        sourceLang="en", targetLang="zh-CN", mode="bilingual", burn="hard",
        model="local:base.en", engine="deepseek", needSubtitle=need_subtitle,
    )
    payload.update(overrides)
    if kind == "url":
        return lambda: admission.client.post(
            "/api/tasks", json={"url": "https://example.com/video", **payload},
        )
    if kind == "upload":
        return lambda: admission.client.post(
            "/api/tasks/upload", data=payload,
            files={"file": ("clip.mp4", b"fake-local-video", "video/mp4")},
        )
    record = admission.store.create(
        url="https://example.com/retry", source_lang="en", target_lang="zh-CN",
        mode="bilingual", burn="hard", model="local:base.en", engine="deepseek",
        need_subtitle=need_subtitle,
    )
    admission.store.update(record.id, status="FAILED", error="interrupted")
    return lambda: admission.client.post(f"/api/tasks/{record.id}/retry")


def _replacement_guard(admission):
    # The model work line may rename the guard while retaining this contract.
    return getattr(admission.models, "model_replacement_guard", None) or admission.models.model_install_guard


@pytest.mark.parametrize("kind", ["url", "upload", "retry"])
@pytest.mark.parametrize("stage", ["persist", "enqueue"])
def test_replacement_waits_for_persistence_and_enqueue(admission, monkeypatch, kind, stage):
    request = _request(admission, kind)
    entered, release, published = threading.Event(), threading.Event(), threading.Event()
    if stage == "persist":
        name = {"url": "create_if_no_recent_active", "upload": "create", "retry": "update"}[kind]
        original = getattr(admission.store, name)

        def gated(*args, **kwargs):
            admission.lock.assert_held()
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        monkeypatch.setattr(admission.store, name, gated)
    else:
        def enqueue(task_id):
            admission.lock.assert_held()
            assert admission.store.get(task_id).status == "PENDING"
            entered.set()
            assert release.wait(5)
            admission.queued.append(task_id)

        monkeypatch.setattr(compat, "enqueue_pipeline", enqueue)

    def replace():
        with _replacement_guard(admission)():
            published.set()

    creation = _start(request)
    try:
        assert entered.wait(5)
        replacement = _start(replace)
        assert admission.lock.contended.wait(5)
        assert not published.is_set()
    finally:
        release.set()
    response = creation.result(timeout=5)
    assert response.status_code == (200 if kind == "retry" else 201), response.text
    with pytest.raises(RuntimeError, match="任务"):
        replacement.result(timeout=5)
    assert admission.queued == [response.json()["id"]]
    assert not published.is_set()
    # Admission has returned although the PENDING task has not been processed.
    with admission.lock:
        assert admission.store.get(response.json()["id"]).status == "PENDING"


@pytest.mark.parametrize("kind", ["url", "upload", "retry"])
def test_admission_waits_for_publication_before_readiness_and_snapshot(admission, kind):
    request = _request(admission, kind)
    before = [(r.id, r.status) for r in admission.store.list()]
    with _replacement_guard(admission)():
        creation = _start(request)
        assert admission.lock.contended.wait(5)
        assert admission.reads == []
        assert admission.snapshots == []
        assert [(r.id, r.status) for r in admission.store.list()] == before
        admission.state["revision"] = "published"
    response = creation.result(timeout=5)
    assert response.status_code == (200 if kind == "retry" else 201), response.text
    assert admission.reads == ["published"]
    assert admission.snapshots == ([] if kind == "retry" else ["published"])


@pytest.mark.parametrize("kind", ["url", "upload", "retry"])
def test_unready_publication_rejects_without_partial_record(admission, kind):
    request = _request(admission, kind)
    before = [dataclasses.asdict(r) for r in admission.store.list()]
    with _replacement_guard(admission)():
        creation = _start(request)
        assert admission.lock.contended.wait(5)
        admission.state.update(state="preparing", ready=False)
    response = creation.result(timeout=5)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "DESKTOP_MODELS_NOT_READY"
    assert [dataclasses.asdict(r) for r in admission.store.list()] == before
    assert admission.queued == []
    assert admission.snapshots == []
    assert not list(admission.tmp.glob("task_*"))
    with admission.lock:
        pass


@pytest.mark.parametrize("kind", ["url", "upload", "retry"])
def test_server_admission_does_not_import_models_or_acquire_lock(admission, monkeypatch, kind):
    request = _request(admission, kind)
    monkeypatch.delenv("SUBTRANS_DESKTOP")
    admission.state.update(ready=False, assert_locked=False)
    original_import = builtins.__import__

    def forbid_desktop_models(name, *args, **kwargs):
        assert name != "src.desktop.models", "server admission imported desktop models"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid_desktop_models)
    with admission.lock:
        response = request()
    assert response.status_code == (200 if kind == "retry" else 201), response.text
    assert not admission.lock.contended.is_set()
    assert admission.reads == []


@pytest.mark.parametrize("kind", ["url", "upload", "retry"])
def test_download_only_admission_does_not_require_model_readiness(admission, kind):
    admission.state.update(state="preparing", ready=False)
    response = _request(admission, kind, need_subtitle=False)()
    assert response.status_code == (200 if kind == "retry" else 201), response.text
    assert admission.reads == []


@pytest.mark.parametrize("kind", ["url", "upload", "retry"])
def test_validation_failure_releases_lock_and_preserves_records(admission, monkeypatch, kind):
    request = _request(admission, kind)
    if kind == "retry":
        record = admission.store.list()[0]
        admission.store.update(record.id, status="SUCCESS")
    else:
        from fastapi import HTTPException

        def fail_validation(**_):
            admission.lock.assert_held()
            raise HTTPException(422, "invalid runtime")

        monkeypatch.setattr(compat, "resolve_runtime", fail_validation)
    before = [dataclasses.asdict(r) for r in admission.store.list()]
    response = request()
    assert response.status_code == (409 if kind == "retry" else 422)
    assert [dataclasses.asdict(r) for r in admission.store.list()] == before
    assert admission.queued == []
    with admission.lock:
        pass


def test_upload_media_validation_error_releases_lock_and_cleans_record(admission, monkeypatch):
    monkeypatch.setattr(compat, "probe_duration", lambda *_: None)
    response = _request(admission, "upload")()
    assert response.status_code == 400
    assert admission.store.list() == []
    assert admission.queued == []
    assert not list(admission.tmp.glob("task_*"))
    with admission.lock:
        pass
