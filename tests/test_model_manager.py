from __future__ import annotations

import io
import sys
import types
from pathlib import Path

import pytest

from src.service import model_manager as models


class TempModelManager(models.LocalModelManager):
    def __init__(self, root: Path) -> None:
        self._test_root = root
        super().__init__()

    @property
    def root(self) -> Path:
        self._test_root.mkdir(parents=True, exist_ok=True)
        return self._test_root


def test_import_publishes_only_after_validation(monkeypatch, tmp_path):
    manager = TempModelManager(tmp_path / "models")
    source = tmp_path / "pytorch_model.bin"
    source.write_bytes(b"source stays intact")
    checked = []

    def validate(path):
        checked.append(path)
        assert (path / "pytorch_model.bin").read_bytes() == source.read_bytes()
        assert not (manager.root / "my-whisper").exists()

    monkeypatch.setattr(models, "validate_hf_model", validate)
    with source.open("rb") as weight:
        result = manager.import_files("my-whisper", "My Whisper", [("pytorch_model.bin", weight)])

    assert result["status"] == "READY"
    assert result["format"] == "huggingface"
    assert len(checked) == 1
    assert (manager.root / "my-whisper" / "pytorch_model.bin").read_bytes() == source.read_bytes()
    assert source.read_bytes() == b"source stays intact"
    assert not list(manager.root.glob(".import-*"))


def test_import_rejects_unsafe_names_and_cleans_staging(tmp_path):
    manager = TempModelManager(tmp_path / "models")
    with pytest.raises(models.ModelValidationError, match="路径"):
        manager.import_files("valid-name", "", [("../pytorch_model.bin", io.BytesIO(b"data"))])
    with pytest.raises(models.ModelValidationError, match="不支持"):
        manager.import_files("valid-name", "", [("model.gguf", io.BytesIO(b"data"))])
    assert not list(manager.root.iterdir())


def test_invalid_import_and_recheck_clean_managed_copies(monkeypatch, tmp_path):
    manager = TempModelManager(tmp_path / "models")

    def fail(_path):
        raise models.ModelValidationError("wrong output")

    monkeypatch.setattr(models, "validate_hf_model", fail)
    with pytest.raises(models.ModelValidationError, match="wrong output"):
        manager.import_files("broken", "", [("pytorch_model.bin", io.BytesIO(b"data"))])
    assert not (manager.root / "broken").exists()

    monkeypatch.setattr(models, "validate_hf_model", lambda _path: None)
    manager.import_files("later-broken", "", [("pytorch_model.bin", io.BytesIO(b"data"))])
    monkeypatch.setattr(models, "validate_hf_model", fail)
    with pytest.raises(models.ModelValidationError, match="wrong output"):
        manager.check("later-broken")
    assert not (manager.root / "later-broken").exists()


def test_recheck_keeps_model_when_optional_dependencies_are_missing(monkeypatch, tmp_path):
    manager = TempModelManager(tmp_path / "models")
    monkeypatch.setattr(models, "validate_hf_model", lambda _path: None)
    manager.import_files("external", "", [("model.safetensors", io.BytesIO(b"data"))])

    def missing(_path):
        raise models.ModelDependencyError("install optional dependencies")

    monkeypatch.setattr(models, "validate_hf_model", missing)
    with pytest.raises(models.ModelDependencyError):
        manager.check("external")
    assert manager.is_ready("external")


def test_format_and_whisper_config_are_required(tmp_path):
    path = tmp_path / "model"
    path.mkdir()
    (path / "config.json").write_text('{"model_type":"bert"}', encoding="utf-8")
    with pytest.raises(models.ModelValidationError, match="Whisper"):
        models.validate_hf_model(path)

    (path / "config.json").write_text('{"model_type":"whisper"}', encoding="utf-8")
    (path / "preprocessor_config.json").write_text("{}", encoding="utf-8")
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    with pytest.raises(models.ModelValidationError, match="权重"):
        models.validate_hf_model(path)


def test_validation_loads_offline_and_checks_asr_output(monkeypatch, tmp_path):
    path = tmp_path / "model"
    path.mkdir()
    (path / "config.json").write_text('{"model_type":"whisper"}', encoding="utf-8")
    (path / "preprocessor_config.json").write_text("{}", encoding="utf-8")
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    (path / "model.safetensors").write_bytes(b"weights")
    calls = []

    class SafeFile:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def keys(self):
            return ["model.encoder.conv1.weight"]

    monkeypatch.setitem(sys.modules, "safetensors", types.SimpleNamespace(safe_open=lambda *_args, **_kwargs: SafeFile()))

    class Processor:
        tokenizer = object()
        feature_extractor = object()

        @classmethod
        def from_pretrained(cls, model_path, **kwargs):
            calls.append(("processor", model_path, kwargs))
            return cls()

    class Model:
        @classmethod
        def from_pretrained(cls, model_path, **kwargs):
            calls.append(("model", model_path, kwargs))
            return cls(), {"missing_keys": [], "mismatched_keys": []}

    output = {"text": "hello", "chunks": [{"text": "hello", "timestamp": (0.0, 1.0)}]}

    def pipeline(_task, **_kwargs):
        def recognize(audio, **kwargs):
            assert audio["sampling_rate"] == 16000
            assert kwargs["return_timestamps"] is True
            return output
        return recognize

    monkeypatch.setitem(sys.modules, "transformers", types.SimpleNamespace(
        WhisperProcessor=Processor, WhisperForConditionalGeneration=Model, pipeline=pipeline,
    ))
    models.validate_hf_model(path)
    assert all(kwargs["local_files_only"] is True and kwargs["trust_remote_code"] is False for _, _, kwargs in calls)
    output["chunks"] = []
    with pytest.raises(models.ModelValidationError, match="时间戳"):
        models.validate_hf_model(path)


def test_delete_ready_model(tmp_path):
    manager = TempModelManager(tmp_path / "models")
    model_dir = manager.root / "small"
    model_dir.mkdir()
    (model_dir / ".subtrans-ready").touch()
    assert manager.delete("small")["status"] == "NOT_INSTALLED"
    assert not model_dir.exists()


def test_official_download_publishes_only_after_cpu_check(monkeypatch, tmp_path):
    manager = TempModelManager(tmp_path / "models")
    observed = {}

    def snapshot_download(*, repo_id, revision, local_dir):
        observed["repo"] = repo_id
        observed["revision"] = revision
        path = Path(local_dir)
        (path / "model.bin").write_bytes(b"weights")
        (path / "config.json").write_text("{}", encoding="utf-8")

    def whisper_model(path, **kwargs):
        observed["kwargs"] = kwargs
        assert not (manager.root / "tiny.en").exists()
        assert (Path(path) / "model.bin").exists()
        return object()

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=snapshot_download))
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=whisper_model))
    manager._states["tiny.en"] = {"status": "DOWNLOADING", "phase": "queued"}
    manager._download_official("tiny.en")

    assert observed["repo"] == "Systran/faster-whisper-tiny.en"
    assert observed["kwargs"] == {"device": "cpu", "compute_type": "int8", "local_files_only": True}
    assert manager.is_ready("tiny.en")
    assert manager.delete("tiny.en")["status"] == "NOT_INSTALLED"


def test_failed_official_download_cleans_staging(monkeypatch, tmp_path):
    manager = TempModelManager(tmp_path / "models")

    def snapshot_download(*, local_dir, **_kwargs):
        (Path(local_dir) / "model.bin").write_bytes(b"incomplete")

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=snapshot_download))
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=lambda *_args, **_kwargs: object()))
    manager._states["small"] = {"status": "DOWNLOADING", "phase": "queued"}
    manager._download_official("small")

    assert manager.list_models()[4]["status"] == "ERROR"
    assert not (manager.root / "small").exists()
    assert not list(manager.root.glob(".download-*"))
