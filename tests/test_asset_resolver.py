from __future__ import annotations

import pytest
from pathlib import Path

from src.config.storage import ArtifactStore
from src.service.asset_resolver import (
    ArtifactStatus,
    AssetResolver,
    ProcessingArtifact,
    ResourceState,
    ResourceError,
)


def test_asset_resolver_check_file_state(tmp_path):
    # 1. Non-existent file
    non_existent = tmp_path / "ghost.mp4"
    assert AssetResolver.check_file_state(non_existent) == ResourceState.DELETED

    # 2. Directory instead of file
    directory = tmp_path / "sub_dir"
    directory.mkdir()
    assert AssetResolver.check_file_state(directory) == ResourceState.UNREADABLE

    # 3. Empty file (0 bytes)
    empty_file = tmp_path / "empty.mp4"
    empty_file.touch()
    assert AssetResolver.check_file_state(empty_file) == ResourceState.UNREADABLE

    # 4. Valid file (has content)
    valid_file = tmp_path / "valid.mp4"
    valid_file.write_bytes(b"content")
    assert AssetResolver.check_file_state(valid_file) == ResourceState.AVAILABLE


def test_processing_artifact_preflight_and_storage_operations(tmp_path):
    store = ArtifactStore(tmp_path)
    artifact = AssetResolver.artifact("task_1", "translated.srt", store=store)

    assert isinstance(artifact, ProcessingArtifact)
    missing = artifact.preflight()
    assert isinstance(missing, ArtifactStatus)
    assert missing.state == ResourceState.DELETED
    assert missing.address is None

    artifact.write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\nhello\n")
    ready = artifact.preflight()
    assert ready.available
    assert ready.address == tmp_path / "task_1" / "translated.srt"
    assert artifact.require() == ready.address
    with artifact.open() as handle:
        assert handle.read().startswith(b"1")

    artifact.delete()
    assert artifact.preflight().state == ResourceState.DELETED


def test_processing_artifact_source_preflight_ignores_partial_download(tmp_path):
    store = ArtifactStore(tmp_path)
    task_dir = store.task_dir("task_1", create=True)
    (task_dir / "source.mp4.part").write_bytes(b"partial")
    assert AssetResolver.preflight_source("task_1", store=store).state == ResourceState.DELETED

    (task_dir / "source.mp4").write_bytes(b"video")
    status = AssetResolver.preflight_source("task_1", store=store)
    assert status.available
    assert status.address == task_dir / "source.mp4"


def test_asset_resolver_resolve_source(tmp_path, monkeypatch):
    # Mock task_dir to return a specific tmp_path for the task_id
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path)

    # 1. Directory doesn't exist
    fake_dir = tmp_path / "fake_task"
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: fake_dir)
    state, path, msg = AssetResolver.resolve_source("task_1")
    assert state == ResourceState.DELETED
    assert path is None
    assert "源视频文件" in msg

    # Restore task_dir
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path)

    # 2. Source file missing
    state, path, msg = AssetResolver.resolve_source("task_1")
    assert state == ResourceState.DELETED
    assert path is None

    # 3. Source file unreadable (empty)
    src_file = tmp_path / "source.mp4"
    src_file.touch()
    state, path, msg = AssetResolver.resolve_source("task_1")
    assert state == ResourceState.UNREADABLE
    assert path == src_file

    # 4. Source file available
    src_file.write_bytes(b"video data")
    state, path, msg = AssetResolver.resolve_source("task_1")
    assert state == ResourceState.AVAILABLE
    assert path == src_file


def test_asset_resolver_ignores_partial_download(tmp_path, monkeypatch):
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path)
    partial = tmp_path / "source.mp4.part"
    partial.write_bytes(b"partial video")

    state, path, msg = AssetResolver.resolve_source("task_1")

    assert state == ResourceState.DELETED
    assert path is None


def test_asset_resolver_require_methods(tmp_path, monkeypatch):
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path)

    # Test require_source raises ResourceError when deleted
    with pytest.raises(ResourceError) as exc_info:
        AssetResolver.require_source("task_1")
    assert exc_info.value.state == ResourceState.DELETED

    # Test require_source returns path when available
    src_file = tmp_path / "source.mp4"
    src_file.write_bytes(b"video data")
    assert AssetResolver.require_source("task_1") == src_file

    # Test require_audio raises ResourceError when unreadable (empty)
    audio_file = tmp_path / "audio.wav"
    audio_file.touch()
    with pytest.raises(ResourceError) as exc_info:
        AssetResolver.require_audio("task_1")
    assert exc_info.value.state == ResourceState.UNREADABLE


def test_cleanup_cancelled_artifacts_burning_step(tmp_path, monkeypatch):
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path)

    (tmp_path / "source.mp4").write_bytes(b"video")
    (tmp_path / "audio.wav").write_bytes(b"audio")
    (tmp_path / "original.srt").write_bytes(b"original")
    (tmp_path / "translated.srt").write_bytes(b"translated")
    (tmp_path / "output.mp4").write_bytes(b"partial_output")
    (tmp_path / "tmp_burn.srt").write_bytes(b"temp_srt")

    AssetResolver.cleanup_cancelled_artifacts("t1", current_step="BURNING", source_type="url")

    assert (tmp_path / "source.mp4").exists()
    assert (tmp_path / "audio.wav").exists()
    assert (tmp_path / "original.srt").exists()
    assert (tmp_path / "translated.srt").exists()
    assert not (tmp_path / "output.mp4").exists()
    assert not (tmp_path / "tmp_burn.srt").exists()


def test_cleanup_cancelled_artifacts_translating_step(tmp_path, monkeypatch):
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path)

    (tmp_path / "source.mp4").write_bytes(b"video")
    (tmp_path / "audio.wav").write_bytes(b"audio")
    (tmp_path / "original.srt").write_bytes(b"original")
    (tmp_path / "translated.srt").write_bytes(b"partial_translated")
    (tmp_path / "output.mp4").write_bytes(b"partial_output")

    AssetResolver.cleanup_cancelled_artifacts("t1", current_step="TRANSLATING", source_type="url")

    assert (tmp_path / "source.mp4").exists()
    assert (tmp_path / "audio.wav").exists()
    assert (tmp_path / "original.srt").exists()
    assert not (tmp_path / "translated.srt").exists()
    assert not (tmp_path / "output.mp4").exists()


def test_cleanup_cancelled_artifacts_downloading_url(tmp_path, monkeypatch):
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path)

    (tmp_path / "source.mp4").write_bytes(b"partial_source")
    (tmp_path / "source.mp4.part").write_bytes(b"part_source")
    (tmp_path / "audio.wav").write_bytes(b"audio")

    AssetResolver.cleanup_cancelled_artifacts("t1", current_step="DOWNLOADING", source_type="url")

    assert not (tmp_path / "source.mp4").exists()
    assert not (tmp_path / "source.mp4.part").exists()
    assert not (tmp_path / "audio.wav").exists()


def test_cleanup_cancelled_artifacts_downloading_upload(tmp_path, monkeypatch):
    monkeypatch.setattr("src.service.asset_resolver.task_dir", lambda tid: tmp_path)

    (tmp_path / "source.mp4").write_bytes(b"user_uploaded_video")
    (tmp_path / "audio.wav").write_bytes(b"partial_audio")

    AssetResolver.cleanup_cancelled_artifacts("t1", current_step="DOWNLOADING", source_type="upload")

    assert (tmp_path / "source.mp4").exists()
    assert not (tmp_path / "audio.wav").exists()
