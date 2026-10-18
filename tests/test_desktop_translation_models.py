"""Desktop translation preparation: no real model or network downloads."""

from __future__ import annotations

import errno
import hashlib
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from src.service import translation_model_manager as models


SNAPSHOT = {
    name: name.encode() for name in (
        "config.json", "pytorch_model.bin", "source.spm", "target.spm",
        "vocab.json", "tokenizer_config.json",
    )
}


def write_snapshot(path):
    for name, data in SNAPSHOT.items():
        (Path(path) / name).write_bytes(data)


class Tokenizer:
    supported_language_codes = [">>cmn_Hans<<"]
    eos_token_id = 2
    model_max_length = 512

    def encode(self, text, add_special_tokens=True):
        return [1, 2]

    def convert_ids_to_tokens(self, ids):
        return [str(item) for item in ids]

    def convert_tokens_to_ids(self, tokens):
        return [1]

    def decode(self, tokens, skip_special_tokens=True):
        return "你好"


class Translator:
    def translate_batch(self, tokens, **kwargs):
        return [SimpleNamespace(hypotheses=[["1"]]) for _ in tokens]


class Converter:
    def __init__(self, source):
        self.source = Path(source)

    def convert(self, output, *, quantization):
        assert quantization == "int8"
        output = Path(output)
        output.mkdir()
        (output / "config.json").write_text("{}")
        (output / "model.bin").write_bytes(b"x" * 4096)
        (output / "shared_vocabulary.json").write_text('["hello"]')


@pytest.fixture
def manager_factory(tmp_path, monkeypatch):
    monkeypatch.delenv("SUBTRANS_DESKTOP", raising=False)
    monkeypatch.delenv("SUBTRANS_DESKTOP_MODEL_ENDPOINT", raising=False)
    managers = []

    def make(**kwargs):
        kwargs.setdefault("converter_factory", Converter)
        kwargs.setdefault("runtime_factory", lambda _path: (Tokenizer(), Translator()))
        manager = models.TranslationModelManager(tmp_path / str(len(managers)) / "model", **kwargs)
        managers.append(manager)
        return manager

    yield make
    for manager in managers:
        manager._executor.shutdown(wait=True)


def finish(manager):
    manager.start_download()
    manager._future.result(timeout=5)
    return manager.status()


@pytest.fixture
def mock_hub(monkeypatch):
    """Actual HTTPX streaming through an in-memory transport; fake Hub metadata."""
    state = SimpleNamespace(
        requests=[], metadata_calls=[], unknown_size=False, sha=models.MODEL_REVISION,
        corrupt=False, network_failure=False, observed=[],
    )

    class HfApi:
        def __init__(self, *, endpoint, token):
            assert token is False
            state.endpoint = endpoint

        def model_info(self, repo, **kwargs):
            state.metadata_calls.append((repo, kwargs))
            siblings = []
            for name, data in SNAPSHOT.items():
                is_lfs = name == "pytorch_model.bin"
                siblings.append(SimpleNamespace(
                    rfilename=name, size=None if state.unknown_size else len(data),
                    lfs=SimpleNamespace(sha256=hashlib.sha256(data).hexdigest()) if is_lfs else None,
                    blob_id=hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest(),
                ))
            siblings.append(SimpleNamespace(rfilename="README.md", size=100000))
            return SimpleNamespace(sha=state.sha, siblings=siblings)

    hub = ModuleType("huggingface_hub")
    hub.HfApi = HfApi
    hub.hf_hub_url = lambda repo, name, *, revision, endpoint: f"{endpoint}/{repo}/resolve/{revision}/{name}"
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)

    class Chunks(httpx.SyncByteStream):
        def __init__(self, data):
            self.data = data

        def __iter__(self):
            for offset in range(0, len(self.data), 3):
                yield self.data[offset:offset + 3]
                if hasattr(state, "manager"):
                    state.observed.append(state.manager.status())

    def respond(request):
        state.requests.append(request)
        if state.network_failure:
            raise httpx.ReadError("signed-url-secret", request=request)
        data = SNAPSHOT[request.url.path.rsplit("/", 1)[1]]
        if state.corrupt:
            data = b"!" * len(data)
        return httpx.Response(200, stream=Chunks(data))

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    return state


def test_http_progress_uses_actual_chunks_pinned_revision_and_selected_files(manager_factory, mock_hub):
    manager = manager_factory()
    mock_hub.manager = manager
    status = finish(manager)
    expected = sum(map(len, SNAPSHOT.values()))
    assert status["model_status"] == models.MODEL_STATUS_READY
    assert status["phase"] == "ready"
    assert status["revision"] == models.MODEL_REVISION
    assert status["downloaded_bytes"] == status["total_bytes"] == expected
    assert status["installed_bytes"] > expected  # Converted bytes are independent.
    observed = [item["downloaded_bytes"] for item in mock_hub.observed]
    assert observed == sorted(observed)
    assert 0 < observed[0] < expected
    assert all(item["phase"] == "downloading" for item in mock_hub.observed)
    assert len(mock_hub.requests) == len(SNAPSHOT)
    assert mock_hub.metadata_calls == [(models.MODEL_REPO, {
        "revision": models.MODEL_REVISION, "files_metadata": True, "timeout": 15,
    })]
    assert all(models.MODEL_REVISION in str(request.url) for request in mock_hub.requests)
    assert all("authorization" not in request.headers for request in mock_hub.requests)
    assert manager.start_download() == status
    assert len(mock_hub.requests) == len(SNAPSHOT)
    manager.validate_offline()


def test_missing_metadata_size_stays_unknown_even_after_download(manager_factory, mock_hub):
    mock_hub.unknown_size = True
    status = finish(manager_factory())
    assert status["model_status"] == models.MODEL_STATUS_READY
    assert status["downloaded_bytes"] == sum(map(len, SNAPSHOT.values()))
    assert status["total_bytes"] is None


def test_conversion_and_checking_do_not_change_transfer_progress(manager_factory):
    phases = []

    def snapshot(*, local_dir, progress_callback, **kwargs):
        write_snapshot(local_dir)
        progress_callback(17, 17)
        phases.append(manager.status())
        return local_dir

    class InspectConverter(Converter):
        def convert(self, output, **kwargs):
            super().convert(output, **kwargs)
            phases.append(manager.status())

    def runtime(path):
        phases.append(manager.status())
        assert Path(path).name == "converted"
        assert not manager.model_dir.exists()  # Publish only after smoke test.
        return Tokenizer(), Translator()

    manager = manager_factory(
        snapshot_download_fn=snapshot, converter_factory=InspectConverter,
        runtime_factory=runtime,
    )
    status = finish(manager)
    assert status["phase"] == "ready"
    assert [phase["phase"] for phase in phases] == ["downloading", "converting", "checking"]
    assert all(phase["downloaded_bytes"] == phase["total_bytes"] == 17 for phase in phases)
    assert phases[1]["installed_bytes"] > phases[0]["installed_bytes"]


def test_legacy_injected_signature_unknown_progress_and_concurrent_idempotency(manager_factory):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def snapshot(*, repo_id, revision, allow_patterns, local_dir):
        calls.append(local_dir)
        write_snapshot(local_dir)
        entered.set()
        assert release.wait(3)
        return local_dir

    manager = manager_factory(snapshot_download_fn=snapshot)
    try:
        manager.start_download()
        assert entered.wait(3)
        future = manager._future
        state = manager.start_download()
        assert manager._future is future
        assert len(calls) == 1
        assert state["downloaded_bytes"] is state["total_bytes"] is None
        assert state["installed_bytes"] > 0
    finally:
        release.set()
    future.result(timeout=5)
    assert manager.status()["phase"] == "ready"
    assert manager.status()["downloaded_bytes"] is None


@pytest.mark.parametrize("desktop,explicit,expected", [
    ("1", None, "https://mirror.example/hub"),
    ("0", None, None),
    ("1", "https://override.example/", "https://override.example"),
])
def test_endpoint_is_desktop_scoped_and_explicit_override_wins(
    manager_factory, monkeypatch, desktop, explicit, expected,
):
    captured = []

    def snapshot(*, local_dir, **kwargs):
        captured.append(kwargs.get("endpoint"))
        write_snapshot(local_dir)
        return local_dir

    monkeypatch.setenv("SUBTRANS_DESKTOP", desktop)
    monkeypatch.setenv("SUBTRANS_DESKTOP_MODEL_ENDPOINT", "https://mirror.example/hub/")
    assert finish(manager_factory(snapshot_download_fn=snapshot, endpoint=explicit))["phase"] == "ready"
    assert captured == [expected]


@pytest.mark.parametrize("endpoint", [
    "http://mirror.example", "file:///tmp/model", "https://user:secret@mirror.example",
    "https://mirror.example/?token=secret", "https://mirror.example/#fragment",
    "https://mirror.example:invalid", "https://mirror.example/ bad",
])
def test_invalid_endpoint_fails_without_calling_downloader(manager_factory, endpoint):
    manager = manager_factory(
        endpoint=endpoint, snapshot_download_fn=lambda **kw: pytest.fail("unexpected download"),
    )
    status = finish(manager)
    assert status["phase"] == "failed"
    assert "HTTPS" in status["model_error"]
    assert "secret" not in status["model_error"]
    assert list(manager.model_dir.parent.iterdir()) == []


def test_configured_endpoint_reaches_metadata_and_file_requests(manager_factory, mock_hub, monkeypatch):
    monkeypatch.setenv("SUBTRANS_DESKTOP", "1")
    monkeypatch.setenv("SUBTRANS_DESKTOP_MODEL_ENDPOINT", "https://mirror.example/hub")
    assert finish(manager_factory())["phase"] == "ready"
    assert mock_hub.endpoint == "https://mirror.example/hub"
    assert all(str(request.url).startswith(mock_hub.endpoint + "/") for request in mock_hub.requests)


def test_low_disk_fails_before_weight_transfer_and_retry_resets_progress(
    manager_factory, mock_hub, monkeypatch,
):
    manager = manager_factory()
    real_usage = models.shutil.disk_usage
    monkeypatch.setattr(models.shutil, "disk_usage", lambda _path: SimpleNamespace(free=1))
    status = finish(manager)
    assert status["phase"] == "failed"
    assert "磁盘空间不足" in status["model_error"]
    assert status["downloaded_bytes"] == 0
    assert status["total_bytes"] == sum(map(len, SNAPSHOT.values()))
    assert mock_hub.requests == []
    assert list(manager.model_dir.parent.iterdir()) == []
    monkeypatch.setattr(models.shutil, "disk_usage", real_usage)
    status = finish(manager)
    assert status["phase"] == "ready"
    assert status["model_error"] is None


@pytest.mark.parametrize("fault", ["revision", "checksum", "network"])
def test_download_failure_is_retryable_and_cleans_only_own_staging(manager_factory, mock_hub, fault):
    manager = manager_factory()
    manager.model_dir.parent.mkdir(parents=True)
    unrelated = manager.model_dir.parent / ".other-agent-work"
    unrelated.write_text("keep")
    if fault == "revision":
        mock_hub.sha = "0" * 40
    elif fault == "checksum":
        mock_hub.corrupt = True
    else:
        mock_hub.network_failure = True
    status = finish(manager)
    assert status["phase"] == "failed"
    assert "signed-url-secret" not in status["model_error"]
    assert list(manager.model_dir.parent.iterdir()) == [unrelated]
    mock_hub.sha, mock_hub.corrupt, mock_hub.network_failure = models.MODEL_REVISION, False, False
    assert finish(manager)["phase"] == "ready"
    assert unrelated.read_text() == "keep"


@pytest.mark.parametrize("failure", ["conversion", "checking", "disk"])
def test_failed_replacement_preserves_previous_model(manager_factory, failure):
    def snapshot(*, local_dir, **kwargs):
        write_snapshot(local_dir)
        return local_dir

    manager = manager_factory(snapshot_download_fn=snapshot)
    assert finish(manager)["phase"] == "ready"
    original = (manager.model_dir / "model.bin").read_bytes()
    manager.mark_failed(RuntimeError("force reinstallation"))

    def fail(_path):
        if failure == "disk":
            raise OSError(errno.ENOSPC, "disk full")
        raise RuntimeError("simulated failure")

    if failure == "checking":
        manager._runtime_factory = fail
    else:
        manager._converter_factory = fail
    status = finish(manager)
    assert status["phase"] == "failed"
    assert (manager.model_dir / "model.bin").read_bytes() == original
    assert list(manager.model_dir.parent.iterdir()) == [manager.model_dir]
    if failure == "disk":
        assert "磁盘空间不足" in status["model_error"]
    else:
        assert ("checking" if failure == "checking" else "converting") in status["model_error"]


@pytest.mark.parametrize("restore_fails", [False, True])
def test_publication_failure_never_deletes_previous_model(manager_factory, monkeypatch, restore_fails):
    def snapshot(*, local_dir, **kwargs):
        write_snapshot(local_dir)
        return local_dir

    manager = manager_factory(snapshot_download_fn=snapshot)
    assert finish(manager)["phase"] == "ready"
    original = (manager.model_dir / "model.bin").read_bytes()
    manager.mark_failed(RuntimeError("retry"))
    rename = Path.rename

    def fail_publish(path, target):
        if path.name == "converted" or (restore_fails and path.name == "previous"):
            raise OSError("simulated rename failure")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_publish)
    status = finish(manager)
    assert status["phase"] == "failed"
    if restore_fails:
        backup = list(manager.model_dir.parent.glob(".opus-mt-en-zh-*/previous/model.bin"))
        assert len(backup) == 1
        assert backup[0].read_bytes() == original
        assert "旧模型仍保留" in status["model_error"]
    else:
        assert (manager.model_dir / "model.bin").read_bytes() == original
        assert list(manager.model_dir.parent.iterdir()) == [manager.model_dir]


def injected_snapshot(*, local_dir, **kwargs):
    write_snapshot(local_dir)
    return local_dir


@pytest.mark.parametrize('fault', ['unknown', 'old_pin', 'same_size_corruption'])
def test_desktop_retry_repairs_unknown_old_or_changed_assets(manager_factory, monkeypatch, fault):
    import json
    from src.desktop import lifecycle
    monkeypatch.setattr(lifecycle, 'active_task_count', lambda: 0)
    manager = manager_factory(snapshot_download_fn=injected_snapshot)
    assert finish(manager)['phase'] == 'ready'
    marker = manager.model_dir / models._PROVENANCE
    if fault == 'unknown':
        marker.unlink()
    elif fault == 'old_pin':
        provenance = json.loads(marker.read_text())
        provenance['revision'] = 'a' * 40
        marker.write_text(json.dumps(provenance))
    else:
        (manager.model_dir / 'model.bin').write_bytes(b'!' * 4096)
    # Server compatibility: pre-manifest installations still work, but their
    # revision must never be synthesized from current configuration.
    if fault == 'unknown':
        assert manager.is_ready()
        assert manager.status()['revision'] is None
    monkeypatch.setenv('SUBTRANS_DESKTOP', '1')
    assert not manager.is_ready()
    assert manager.status()['revision'] != models.MODEL_REVISION
    repaired = finish(manager)
    assert repaired['phase'] == 'ready'
    assert repaired['revision'] == models.MODEL_REVISION
    assert (manager.model_dir / 'model.bin').read_bytes() == b'x' * 4096
    assert manager._provenance()['validated'] == 'cpu/int8'
    # Validate retained predecessor is outside the published model.
    assert list(manager.model_dir.parent.glob('.opus-mt-en-zh-*/previous/model.bin'))


def test_restart_status_reads_persisted_provenance_and_transfer_counts(manager_factory, mock_hub):
    manager = manager_factory()
    original = finish(manager)
    restarted = models.TranslationModelManager(manager.model_dir)
    try:
        status = restarted.status()
        assert status['revision'] == models.MODEL_REVISION
        assert status['downloaded_bytes'] == original['downloaded_bytes']
        assert status['total_bytes'] == original['total_bytes']
        assert len(mock_hub.metadata_calls) == 1
    finally:
        restarted._executor.shutdown(wait=True)


def test_active_pipeline_prevents_translation_replacement(manager_factory, monkeypatch):
    from src.desktop import lifecycle
    manager = manager_factory(snapshot_download_fn=injected_snapshot)
    assert finish(manager)['phase'] == 'ready'
    old_stamp = (manager.model_dir / 'model.bin').stat().st_ino
    monkeypatch.setenv('SUBTRANS_DESKTOP', '1')
    monkeypatch.setattr(lifecycle, 'active_task_count', lambda: 1)
    manager.mark_failed(RuntimeError('retry'))
    failed = finish(manager)
    assert failed['phase'] == 'failed' and '任务正在处理' in failed['model_error']
    assert (manager.model_dir / 'model.bin').stat().st_ino == old_stamp
    assert not list(manager.model_dir.parent.glob('.opus-mt-en-zh-*'))
    monkeypatch.setattr(lifecycle, 'active_task_count', lambda: 0)
    assert finish(manager)['phase'] == 'ready'
    assert (manager.model_dir / 'model.bin').stat().st_ino != old_stamp


def test_retry_does_not_show_old_transfer_counters(manager_factory):
    def first(*, local_dir, progress_callback, **kwargs):
        write_snapshot(local_dir)
        progress_callback(123, 123)
        return local_dir

    manager = manager_factory(snapshot_download_fn=first)
    assert finish(manager)['downloaded_bytes'] == 123
    entered, release = threading.Event(), threading.Event()

    def retry(*, local_dir, **kwargs):
        entered.set()
        assert release.wait(3)
        return injected_snapshot(local_dir=local_dir)

    manager.mark_failed(RuntimeError('retry'))
    manager._snapshot_download_fn = retry
    try:
        manager.start_download()
        assert entered.wait(3)
        state = manager.status()
        assert state['downloaded_bytes'] is None and state['total_bytes'] is None
    finally:
        release.set()
    manager._future.result(timeout=5)
    assert manager.status()['downloaded_bytes'] is None


def test_desktop_installations_serialize_across_managers(manager_factory, monkeypatch):
    from contextlib import contextmanager
    from src.desktop import lifecycle
    monkeypatch.setenv('SUBTRANS_DESKTOP', '1')
    monkeypatch.setattr(lifecycle, 'active_task_count', lambda: 0)
    entered, release, waiting, second_download = (threading.Event() for _ in range(4))

    def first_snapshot(*, local_dir, **kwargs):
        entered.set()
        assert release.wait(3)
        return injected_snapshot(local_dir=local_dir)

    def second_snapshot(*, local_dir, **kwargs):
        second_download.set()
        return injected_snapshot(local_dir=local_dir)

    first = manager_factory(snapshot_download_fn=first_snapshot)
    second = manager_factory(snapshot_download_fn=second_snapshot)
    second.model_dir = first.model_dir
    original_lock = second._installation_lock

    @contextmanager
    def observed_lock():
        waiting.set()
        with original_lock():
            yield

    monkeypatch.setattr(second, '_installation_lock', observed_lock)
    try:
        first.start_download()
        assert entered.wait(3)
        second.start_download()
        assert waiting.wait(3)
        assert not second_download.wait(0.05)
    finally:
        release.set()
    first._future.result(timeout=5)
    second._future.result(timeout=5)
    assert first.status()['phase'] == second.status()['phase'] == 'ready'
    assert second_download.is_set()


def test_installed_runtime_is_reused_and_replacement_invalidates_old_cache(manager_factory):
    runtimes = []

    def runtime(_path):
        runtimes.append((Tokenizer(), Translator()))
        return runtimes[-1]

    manager = manager_factory(snapshot_download_fn=injected_snapshot, runtime_factory=runtime)
    assert finish(manager)['phase'] == 'ready'
    assert manager._get_runtime() == runtimes[0]
    manager.validate_offline()
    assert len(runtimes) == 1
    manager.mark_failed(RuntimeError('replace'))
    assert finish(manager)['phase'] == 'ready'
    assert manager._get_runtime() == runtimes[1]
    manager.validate_offline()
    assert len(runtimes) == 2
