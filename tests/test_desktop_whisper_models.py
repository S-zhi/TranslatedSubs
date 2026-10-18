from __future__ import annotations

import base64
import hashlib
import sys
import threading
import types
from pathlib import Path

import pytest

from src.service import model_manager as models

PIN = models.OFFICIAL_MODEL_REVISIONS['base.en']
PAYLOADS = {
    'config.json': b'{}', 'model.bin': b'weights',
    'tokenizer.json': b'{}', 'vocabulary.txt': b'hello\n',
}


class Manager(models.LocalModelManager):
    def __init__(self, root):
        self._root = root
        super().__init__()

    @property
    def root(self):
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.delenv('SUBTRANS_DESKTOP', raising=False)
    monkeypatch.delenv('SUBTRANS_DESKTOP_MODEL_ENDPOINT', raising=False)
    value = Manager(tmp_path / 'models')
    yield value
    value._executor.shutdown(wait=True)


def inventory(manager):
    return next(item for item in manager.list_models() if item['name'] == 'base.en')


def install_fakes(monkeypatch, manager, *, known_total=True, download=None, load=None, sha=PIN):
    calls = []

    class HfApi:
        def __init__(self, **kwargs):
            calls.append(('api', kwargs))

        def model_info(self, repo_id, **kwargs):
            calls.append(('metadata', repo_id, kwargs))
            return types.SimpleNamespace(sha=sha, siblings=[
                types.SimpleNamespace(
                    rfilename=name, size=len(content) if known_total else None,
                    lfs={'sha256': hashlib.sha256(content).hexdigest()},
                ) for name, content in PAYLOADS.items()
            ])

    def snapshot_download(**kwargs):
        calls.append(('snapshot', kwargs))
        stage = Path(kwargs['local_dir'])
        if download:
            download(stage)
        else:
            for name, content in PAYLOADS.items():
                (stage / name).write_bytes(content)

    def whisper(path, **kwargs):
        calls.append(('load', kwargs))
        assert not (manager.root / 'base.en').exists()
        assert inventory(manager)['phase'] == 'checking'
        if load:
            load(Path(path))
        return object()

    monkeypatch.setitem(sys.modules, 'huggingface_hub', types.SimpleNamespace(
        HfApi=HfApi, snapshot_download=snapshot_download,
    ))
    monkeypatch.setitem(sys.modules, 'huggingface_hub.utils._runtime', types.SimpleNamespace(
        is_xet_available=lambda: False,
    ))
    monkeypatch.setitem(sys.modules, 'faster_whisper', types.SimpleNamespace(WhisperModel=whisper))
    return calls


def run_download(manager, **kwargs):
    manager.download('base.en', **kwargs)
    # Queue a barrier behind the job instead of timing sleeps.
    manager._executor.shutdown(wait=True)
    return inventory(manager)


def test_pin_progress_local_load_and_restart_readiness(monkeypatch, manager):
    entered, release = threading.Event(), threading.Event()

    def download(stage):
        cache = stage / '.cache' / 'huggingface' / 'download'
        cache.mkdir(parents=True)
        prefix = base64.urlsafe_b64encode(hashlib.sha1(b'model.bin.metadata').digest()).decode()
        partial = cache / f'{prefix}.fake.incomplete'
        partial.write_bytes(b'weig')
        (cache / 'unrelated.metadata').write_bytes(b'x' * 1000)
        entered.set()
        assert release.wait(5)
        for name, content in PAYLOADS.items():
            (stage / name).write_bytes(content)
        partial.unlink()

    calls = install_fakes(monkeypatch, manager, download=download)
    monkeypatch.setenv('SUBTRANS_DESKTOP', '1')
    manager.download('base.en')
    try:
        assert entered.wait(5)
        row = inventory(manager)
        assert row['downloadedBytes'] == 4
        assert row['totalBytes'] == sum(map(len, PAYLOADS.values()))
        assert row['revision'] == PIN
        assert not manager.is_ready('base.en')
        manager.download('base.en')  # Deduplicate the running request.
    finally:
        release.set()
    manager._executor.shutdown(wait=True)
    row = inventory(manager)
    assert row['status'] == 'READY'
    assert row['phase'] == 'ready'
    assert row['downloadedBytes'] == row['totalBytes']
    assert [kind for kind, *_ in calls].count('snapshot') == 1
    snap = next(call[1] for call in calls if call[0] == 'snapshot')
    assert snap['revision'] == PIN and snap['token'] is False
    assert snap['endpoint'] == 'https://huggingface.co'
    assert set(snap['allow_patterns']) == set(PAYLOADS)
    assert next(call[1] for call in calls if call[0] == 'load') == {
        'device': 'cpu', 'compute_type': 'int8', 'local_files_only': True,
    }
    fresh = Manager(manager.root)
    try:
        assert fresh.is_ready('base.en')
        assert inventory(fresh)['revision'] == PIN
        (manager.root / 'base.en' / 'model.bin').write_bytes(b'broken')
        assert not fresh.is_ready('base.en')
    finally:
        fresh._executor.shutdown(wait=True)


def test_unknown_total_and_endpoint_share_origin(monkeypatch, manager):
    calls = install_fakes(monkeypatch, manager, known_total=False)
    row = run_download(manager, revision=PIN, endpoint='https://models.example.org/')
    assert row['status'] == 'READY'
    assert row['totalBytes'] is None
    assert row['downloadedBytes'] == sum(map(len, PAYLOADS.values()))
    assert next(call[1] for call in calls if call[0] == 'api') == {
        'endpoint': 'https://models.example.org', 'token': False,
    }
    assert next(call[1] for call in calls if call[0] == 'snapshot')['endpoint'] == 'https://models.example.org'


@pytest.mark.parametrize('endpoint', [
    'http://mirror.example', 'https://u:p@mirror.example', 'https://mirror.example/path',
    'https://mirror.example?token=secret', 'https://mirror.example#fragment',
    'https://mirror.example:bad', 'https://mirror.example:0', 'https://',
    'https://mirror.example\n', 'https://mirror.example\\evil', '',
])
def test_bad_endpoint_never_queues(manager, endpoint):
    with pytest.raises(models.ModelValidationError, match='HTTPS'):
        manager.download('base.en', revision=PIN, endpoint=endpoint)
    assert manager._states == {}


@pytest.mark.parametrize('revision', ['', 'main', '../other', '1234'])
def test_bad_revision_never_queues(manager, revision):
    with pytest.raises(models.ModelValidationError, match='SHA'):
        manager.download('base.en', revision=revision)


def test_cpu_failure_retry_and_scoped_interrupted_cleanup(monkeypatch, manager):
    stale = manager.root / '.download-base.en-desktop'
    stale.mkdir()
    (stale / 'old.bin').write_bytes(b'old')
    unrelated = manager.root / '.download-tiny-desktop'
    unrelated.mkdir()
    (unrelated / 'keep').write_bytes(b'keep')
    outside = manager.root.parent / 'user-model'
    outside.mkdir()
    (outside / 'keep').write_bytes(b'keep')
    (stale / 'link').symlink_to(outside, target_is_directory=True)

    def fail(_):
        raise RuntimeError('cannot load on cpu')

    install_fakes(monkeypatch, manager, load=fail)
    row = run_download(manager, revision=PIN)
    assert row['status'] == 'ERROR' and 'cannot load' in row['error']
    assert not manager.is_ready('base.en')
    assert not stale.exists()
    assert (unrelated / 'keep').exists() and (outside / 'keep').exists()
    # A new process/manager retries a failed preparation using the same API.
    retry = Manager(manager.root)
    try:
        install_fakes(monkeypatch, retry)
        assert run_download(retry, revision=PIN)['status'] == 'READY'
    finally:
        retry._executor.shutdown(wait=True)


@pytest.mark.parametrize('failure', ['hash', 'missing', 'symlink', 'network'])
def test_download_failure_never_publishes(monkeypatch, manager, failure):
    def download(stage):
        for name, content in PAYLOADS.items():
            (stage / name).write_bytes(content)
        if failure == 'network':
            raise OSError('network interrupted')
        target = stage / 'model.bin'
        if failure == 'hash':
            target.write_bytes(b'corrupt')  # Same size; digest must reject it.
        elif failure == 'missing':
            target.unlink()
        else:
            target.unlink()
            target.symlink_to(stage / 'vocabulary.txt')

    calls = install_fakes(monkeypatch, manager, download=download)
    row = run_download(manager, revision=PIN)
    assert row['status'] == 'ERROR'
    assert not (manager.root / 'base.en').exists()
    assert not (manager.root / '.download-base.en-desktop').exists()
    assert not any(call[0] == 'load' for call in calls)


def test_disk_space_and_wrong_revision_fail_before_download(monkeypatch, manager):
    calls = install_fakes(monkeypatch, manager)
    monkeypatch.setattr(models.shutil, 'disk_usage', lambda _: types.SimpleNamespace(free=1))
    assert run_download(manager, revision=PIN)['status'] == 'ERROR'
    assert not any(call[0] == 'snapshot' for call in calls)


def test_source_cannot_change_pinned_revision(monkeypatch, manager):
    calls = install_fakes(monkeypatch, manager, sha='0' * 40)
    row = run_download(manager, revision=PIN)
    assert row['status'] == 'ERROR' and '版本' in row['error']
    assert not any(call[0] == 'snapshot' for call in calls)


def test_legacy_marker_stays_server_only(monkeypatch, manager):
    path = manager.root / 'base.en'
    path.mkdir()
    (path / '.subtrans-ready').touch()
    assert manager.is_ready('base.en')
    monkeypatch.setenv('SUBTRANS_DESKTOP', '1')
    assert not manager.is_ready('base.en')


def test_legacy_submission_passes_no_new_kwargs(manager):
    submissions = []
    manager._executor.shutdown(wait=True)
    manager._executor = types.SimpleNamespace(
        submit=lambda *args, **kwargs: submissions.append((args, kwargs)),
        shutdown=lambda **_: None,
    )
    manager.download('tiny')
    assert submissions[0][0][1:] == ('tiny',)
    assert submissions[0][1] == {}


def test_same_manager_can_retry_after_network_failure(monkeypatch, manager):
    # Inline scheduling makes the retry boundary deterministic, while the separate
    # progress test above exercises real worker threads.
    monkeypatch.setattr(manager._executor, 'submit', lambda fn, *args, **kw: fn(*args, **kw))

    def interrupt(stage):
        (stage / 'model.bin').write_bytes(b'wei')
        raise OSError('network interrupted')

    install_fakes(monkeypatch, manager, download=interrupt)
    first = manager.download('base.en', revision=PIN)
    assert first['status'] == 'ERROR'
    assert first['downloadedBytes'] == 3
    install_fakes(monkeypatch, manager)
    second = manager.download('base.en', revision=PIN)
    assert second['status'] == 'READY' and second['error'] is None


def test_two_managers_deduplicate_without_cleaning_active_stage(monkeypatch, manager):
    entered, release = threading.Event(), threading.Event()

    def download(stage):
        (stage / 'model.bin').write_bytes(b'wei')
        entered.set()
        assert release.wait(5)
        for name, content in PAYLOADS.items():
            (stage / name).write_bytes(content)

    calls = install_fakes(monkeypatch, manager, download=download)
    second = Manager(manager.root)
    try:
        manager.download('base.en', revision=PIN)
        assert entered.wait(5)
        second.download('base.en', revision=PIN)
        assert (manager.root / '.download-base.en-desktop' / 'model.bin').read_bytes() == b'wei'
        release.set()
        manager._executor.shutdown(wait=True)
        second._executor.shutdown(wait=True)
        assert manager.is_ready('base.en') and second.is_ready('base.en')
        assert [kind for kind, *_ in calls].count('snapshot') == 1
    finally:
        release.set()
        second._executor.shutdown(wait=True)


def test_staging_symlink_only_removes_link(monkeypatch, manager):
    outside = manager.root.parent / 'original'
    outside.mkdir()
    (outside / 'keep').write_bytes(b'keep')
    (manager.root / '.download-base.en-desktop').symlink_to(outside, target_is_directory=True)
    install_fakes(monkeypatch, manager)
    assert run_download(manager, revision=PIN)['status'] == 'READY'
    assert (outside / 'keep').read_bytes() == b'keep'


def test_ready_marker_symlink_is_not_trusted(monkeypatch, manager):
    install_fakes(monkeypatch, manager)
    assert run_download(manager, revision=PIN)['status'] == 'READY'
    path = manager.root / 'base.en'
    marker = path / '.subtrans-ready'
    marker.unlink()
    marker.symlink_to(path / 'config.json')
    assert not manager.is_ready('base.en')


def test_unpinned_endpoint_resolves_once_and_downloads_exact_commit(monkeypatch, manager):
    calls = install_fakes(monkeypatch, manager)
    assert run_download(manager, endpoint='https://mirror.example:8443')['revision'] == PIN
    metadata = next(call for call in calls if call[0] == 'metadata')
    snapshot = next(call[1] for call in calls if call[0] == 'snapshot')
    assert metadata[2]['revision'] == 'main'
    assert snapshot['revision'] == PIN
    assert snapshot['endpoint'] == 'https://mirror.example:8443'


def test_xet_partial_logical_size_is_not_download_progress(monkeypatch, tmp_path):
    stage = tmp_path / 'stage'
    cache = stage / '.cache' / 'huggingface' / 'download'
    cache.mkdir(parents=True)
    (stage / 'config.json').write_bytes(b'{}')
    (cache / 'model.bin.fake.incomplete').write_bytes(b'not yet reconstructed')
    monkeypatch.setitem(sys.modules, 'huggingface_hub.utils._runtime', types.SimpleNamespace(
        is_xet_available=lambda: True,
    ))
    assert models._downloaded_bytes(stage) == 2


@pytest.mark.parametrize('fault', ['old_pin', 'same_size_corruption', 'offline_load'])
def test_retry_replaces_invalid_install_without_manual_deletion(monkeypatch, manager, fault):
    from src.desktop import lifecycle
    monkeypatch.setattr(lifecycle, 'active_task_count', lambda: 0)
    monkeypatch.setenv('SUBTRANS_DESKTOP', '1')
    monkeypatch.setattr(manager._executor, 'submit', lambda fn, *a, **kw: fn(*a, **kw))
    calls = install_fakes(monkeypatch, manager)
    assert manager.download('base.en', revision=PIN)['status'] == 'READY'
    target = manager.root / 'base.en'
    if fault == 'old_pin':
        import json
        marker = target / models._DOWNLOAD_MANIFEST
        value = json.loads(marker.read_text())
        value['revision'] = 'a' * 40
        marker.write_text(json.dumps(value))
    elif fault == 'same_size_corruption':
        (target / 'model.bin').write_bytes(b'broken!')
        assert not manager.is_ready('base.en')
    else:
        manager.mark_failed('base.en', RuntimeError('offline loading failed'))
        assert not manager.is_ready('base.en')
    # The previous model remains in place throughout validation of staging.
    observations = []
    monkeypatch.setitem(sys.modules, 'faster_whisper', types.SimpleNamespace(
        WhisperModel=lambda path, **kw: observations.append((Path(path), target.exists())),
    ))
    repaired = manager.download('base.en', revision=PIN)
    assert repaired['status'] == 'READY' and repaired['revision'] == PIN
    assert (target / 'model.bin').read_bytes() == PAYLOADS['model.bin']
    assert observations[0][1]
    assert [kind for kind, *_ in calls].count('snapshot') == 2
    assert list(manager.root.glob('.previous-base.en-*/model/model.bin'))


def test_active_pipeline_blocks_whisper_replacement_and_retry_recovers(monkeypatch, manager):
    from src.desktop import lifecycle
    monkeypatch.setenv('SUBTRANS_DESKTOP', '1')
    monkeypatch.setattr(manager._executor, 'submit', lambda fn, *a, **kw: fn(*a, **kw))
    install_fakes(monkeypatch, manager)
    assert manager.download('base.en', revision=PIN)['status'] == 'READY'
    target = manager.root / 'base.en'
    manager.mark_failed('base.en', RuntimeError('reload required'))
    monkeypatch.setitem(sys.modules, 'faster_whisper', types.SimpleNamespace(WhisperModel=lambda *a, **kw: object()))
    monkeypatch.setattr(lifecycle, 'active_task_count', lambda: 1)
    row = manager.download('base.en', revision=PIN)
    assert row['status'] == 'ERROR' and '任务正在处理' in row['error']
    assert (target / 'model.bin').read_bytes() == PAYLOADS['model.bin']
    assert not list(manager.root.glob('.previous-*'))
    monkeypatch.setattr(lifecycle, 'active_task_count', lambda: 0)
    assert manager.download('base.en', revision=PIN)['status'] == 'READY'


def test_whisper_failed_publication_rolls_back_old_model(monkeypatch, manager):
    target = manager.root / 'base.en'
    target.mkdir()
    (target / 'model.bin').write_bytes(b'old')
    stage = manager.root / 'staging'
    stage.mkdir()
    (stage / 'model.bin').write_bytes(b'new')
    rename = Path.rename

    def fail_publish(path, destination):
        if path == stage:
            raise OSError('cannot publish')
        return rename(path, destination)

    monkeypatch.setattr(Path, 'rename', fail_publish)
    with pytest.raises(OSError, match='cannot publish'):
        models._publish_model(stage, target)
    assert (target / 'model.bin').read_bytes() == b'old'
    assert not list(manager.root.glob('.previous-*'))
