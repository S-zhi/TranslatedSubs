from pathlib import Path

import pytest

from src.config.storage import ArtifactStore, artifact_name


def test_artifact_ref_resolves_against_store_root(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    ref = store.artifact("task_1", "output.mp4")

    ref.path.parent.mkdir(parents=True)
    ref.path.write_bytes(b"video")

    assert ref.exists()
    assert ref.path == tmp_path / "task_1" / "output.mp4"
    assert ref.path.read_bytes() == b"video"


def test_artifact_names_are_portable_and_confined_to_one_file():
    assert artifact_name(r"C:\data\task_1\output.mp4") == "output.mp4"
    assert artifact_name("/data/task_1/output.mp4") == "output.mp4"

    with pytest.raises(ValueError):
        ArtifactStore("/tmp/data").artifact("task_1", "../output.mp4")
    with pytest.raises(ValueError):
        ArtifactStore("/tmp/data").artifact("task_1", r"sub\output.mp4")
