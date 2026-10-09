"""Tests for the optional, offline CTranslate2 translation path."""

from __future__ import annotations

import io
import importlib
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from src.core.srt_utils import parse_srt
from src.core.translator import TranslateError, translate_srt, translate_texts
from src.handler import tasks as task_routes
from src.handler.schemas import TaskCreate
from src.service.orchestrator import PipelineParams
from src.service.translation_model_manager import (
    ENGINE_ID,
    MODEL_REPO,
    MODEL_REVISION,
    MODEL_STATUS_DOWNLOADING,
    MODEL_STATUS_FAILED,
    MODEL_STATUS_READY,
    TranslationModelError,
    TranslationModelManager,
)
from src.store import DEFAULT_TRANSLATION_ENGINE_ID, TaskStore, TranslationEngineStore


class _FakeTokenizer:
    supported_language_codes = [">>cmn_Hans<<"]
    eos_token_id = 2
    model_max_length = 512

    def __init__(self):
        self.encoded = []

    def encode(self, text, add_special_tokens=True):
        self.encoded.append((text, add_special_tokens))
        return [len(text), 2]

    @staticmethod
    def convert_ids_to_tokens(ids):
        return [str(item) for item in ids]

    @staticmethod
    def convert_tokens_to_ids(tokens):
        return list(tokens)

    @staticmethod
    def decode(tokens, skip_special_tokens=True):
        return "译文" + str(tokens[0])


class _FakeTranslator:
    def __init__(self):
        self.batches = []

    def translate_batch(self, source_tokens, **kwargs):
        self.batches.append((source_tokens, kwargs))
        return [types.SimpleNamespace(hypotheses=[[f"translated-{row[0]}"]]) for row in source_tokens]


def _runtime_factory(_path):
    return _FakeTokenizer(), _FakeTranslator()


def _write_snapshot(path):
    for name in (
        "config.json",
        "pytorch_model.bin",
        "source.spm",
        "target.spm",
        "vocab.json",
        "tokenizer_config.json",
    ):
        (path / name).write_bytes(name.encode("utf-8"))


def _write_installed_model(path):
    path.mkdir(parents=True, exist_ok=True)
    for name in (
        "config.json", "model.bin", "shared_vocabulary.json", "source.spm",
        "target.spm", "vocab.json", "tokenizer_config.json",
    ):
        contents = b'["<unk>","hello"]' if "vocabulary.json" in name else b"x"
        (path / name).write_bytes(contents)


class _FakeConverter:
    def __init__(self, source_dir, calls):
        self.source_dir = source_dir
        self.calls = calls

    def convert(self, output_dir, *, quantization):
        self.calls.append((self.source_dir, quantization))
        output = Path(output_dir)
        assert not output.exists()
        output.mkdir()
        (output / "config.json").write_text("{}", encoding="utf-8")
        (output / "model.bin").write_bytes(b"int8-model")
        (output / "shared_vocabulary.json").write_bytes(b'["<unk>","hello"]')


def test_model_install_failure_cleans_temporary_files_and_can_retry(tmp_path):
    started = threading.Event()
    release = threading.Event()
    calls = {"download": 0}
    conversion_calls = []

    def download(*, repo_id, revision, allow_patterns, local_dir):
        calls["download"] += 1
        assert repo_id == MODEL_REPO
        assert revision == MODEL_REVISION
        assert "pytorch_model.bin" in allow_patterns
        assert "README.md" not in allow_patterns
        path = Path(local_dir)
        if calls["download"] == 1:
            (path / "partial.bin").write_bytes(b"download-progress")
            started.set()
            assert release.wait(timeout=3)
            raise RuntimeError("network interrupted")
        _write_snapshot(path)
        return str(path)

    manager = TranslationModelManager(
        tmp_path / "models" / "opus-mt-en-zh",
        snapshot_download_fn=download,
        converter_factory=lambda source: _FakeConverter(source, conversion_calls),
        runtime_factory=_runtime_factory,
    )
    try:
        manager.start_download()
        assert started.wait(timeout=3)
        in_progress = manager.status()
        assert in_progress["model_status"] == MODEL_STATUS_DOWNLOADING
        assert in_progress["installed_bytes"] >= len(b"download-progress")
        release.set()
        manager._future.result(timeout=3)

        failed = manager.status()
        assert failed["model_status"] == MODEL_STATUS_FAILED
        assert "network interrupted" in failed["model_error"]
        assert list((tmp_path / "models").iterdir()) == []

        manager.start_download()
        manager._future.result(timeout=3)
        assert manager.status()["model_status"] == MODEL_STATUS_READY
        assert manager.is_ready()
        assert manager.status()["installed_bytes"] > 0
        assert len(conversion_calls) == 1
        assert conversion_calls[0][1] == "int8"
        tokenizer, translator = manager._get_runtime()
        assert manager._get_runtime() == (tokenizer, translator)
        manager.validate_offline()
    finally:
        release.set()
        manager._executor.shutdown(wait=True)


def test_runtime_uses_local_tokenizer_and_cpu_int8(monkeypatch, tmp_path):
    captured = {}

    class MarianTokenizer:
        @staticmethod
        def from_pretrained(path, *, local_files_only):
            captured["tokenizer"] = (path, local_files_only)
            return _FakeTokenizer()

    def translator(path, *, device, compute_type, inter_threads, intra_threads):
        captured["translator"] = (
            path, device, compute_type, inter_threads, intra_threads,
        )
        return _FakeTranslator()

    ctranslate2 = types.ModuleType("ctranslate2")
    ctranslate2.Translator = translator
    transformers = types.ModuleType("transformers")
    transformers.MarianTokenizer = MarianTokenizer
    monkeypatch.setitem(sys.modules, "ctranslate2", ctranslate2)
    monkeypatch.setitem(sys.modules, "transformers", transformers)

    model_dir = tmp_path / "model"
    manager = TranslationModelManager(model_dir)
    tokenizer, engine = manager._load_runtime(model_dir)
    assert isinstance(tokenizer, _FakeTokenizer)
    assert isinstance(engine, _FakeTranslator)
    assert captured["tokenizer"] == (str(model_dir), True)
    assert captured["translator"] == (str(model_dir), "cpu", "int8", 1, 4)
    manager._executor.shutdown(wait=True)


def test_runtime_cpu_thread_counts_can_be_overridden(monkeypatch, tmp_path):
    captured = {}

    class MarianTokenizer:
        @staticmethod
        def from_pretrained(path, *, local_files_only):
            return _FakeTokenizer()

    def translator(path, *, device, compute_type, inter_threads, intra_threads):
        captured["threads"] = (inter_threads, intra_threads)
        return _FakeTranslator()

    ctranslate2 = types.ModuleType("ctranslate2")
    ctranslate2.Translator = translator
    transformers = types.ModuleType("transformers")
    transformers.MarianTokenizer = MarianTokenizer
    monkeypatch.setitem(sys.modules, "ctranslate2", ctranslate2)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setenv("SUBTRANS_LOCAL_CT2_INTER_THREADS", "2")
    monkeypatch.setenv("SUBTRANS_LOCAL_CT2_INTRA_THREADS", "6")

    manager = TranslationModelManager(tmp_path / "model")
    manager._load_runtime(tmp_path / "model")
    assert captured["threads"] == (2, 6)
    manager._executor.shutdown(wait=True)


def test_warmup_is_async_idempotent_and_loads_runtime_once(tmp_path):
    model_dir = tmp_path / "model"
    _write_installed_model(model_dir)
    started = threading.Event()
    release = threading.Event()
    loads = []

    manager = TranslationModelManager(model_dir, runtime_factory=_runtime_factory)
    manager.dependency_status = lambda: {"ready": True, "missing": []}

    def blocked_runtime(_path):
        loads.append(1)
        started.set()
        assert release.wait(timeout=3)
        return _FakeTokenizer(), _FakeTranslator()

    manager._runtime_factory = blocked_runtime
    try:
        before = time.monotonic()
        assert manager.start_warmup() is True
        assert time.monotonic() - before < 0.5
        assert started.wait(timeout=3)
        assert manager.start_warmup() is False
        release.set()
        manager._warm_future.result(timeout=3)
        assert loads == [1]
        assert manager.start_warmup() is False
    finally:
        release.set()
        manager._executor.shutdown(wait=True)


def test_warmup_does_not_wait_for_task_already_loading_runtime(tmp_path):
    model_dir = tmp_path / "model"
    _write_installed_model(model_dir)
    entered = threading.Event()
    release = threading.Event()

    manager = TranslationModelManager(model_dir, runtime_factory=_runtime_factory)
    manager.dependency_status = lambda: {"ready": True, "missing": []}

    def blocked_runtime(_path):
        entered.set()
        assert release.wait(timeout=3)
        return _FakeTokenizer(), _FakeTranslator()

    manager._runtime_factory = blocked_runtime
    task = threading.Thread(target=manager._get_runtime)
    task.start()
    try:
        assert entered.wait(timeout=3)
        started = time.monotonic()
        assert manager.start_warmup() is False
        assert time.monotonic() - started < 0.5
        assert manager._warm_future is None
    finally:
        release.set()
        task.join(timeout=3)
        manager._executor.shutdown(wait=True)


def test_warmup_skips_missing_model_or_dependencies_without_download(tmp_path, monkeypatch):
    manager = TranslationModelManager(
        tmp_path / "model",
        snapshot_download_fn=lambda **_kwargs: pytest.fail("warmup must not download"),
    )
    try:
        assert manager.start_warmup() is False
        _write_installed_model(manager.model_dir)
        monkeypatch.setattr(
            manager,
            "dependency_status",
            lambda: {"ready": False, "missing": ["ctranslate2"]},
        )
        assert manager.start_warmup() is False
        assert manager._warm_future is None
    finally:
        manager._executor.shutdown(wait=True)


def test_warmup_failure_is_recorded_without_escaping(tmp_path):
    model_dir = tmp_path / "model"
    _write_installed_model(model_dir)
    manager = TranslationModelManager(model_dir)
    manager.dependency_status = lambda: {"ready": True, "missing": []}
    manager._get_runtime = lambda: (_ for _ in ()).throw(RuntimeError("warmup boom"))
    try:
        assert manager.start_warmup() is True
        manager._warm_future.result(timeout=3)
        status = manager.status()
        assert status["model_status"] == MODEL_STATUS_FAILED
        assert "warmup boom" in status["model_error"]
    finally:
        manager._executor.shutdown(wait=True)


def test_app_startup_submits_warmup_without_blocking_health(monkeypatch):
    app_module = importlib.import_module("src.handler.app")
    calls = []

    class FakeManager:
        def start_warmup(self):
            calls.append("start")
            return True

        def cancel_warmup(self):
            calls.append("cancel")
            return True

    monkeypatch.setattr(app_module, "get_translation_model_manager", lambda: FakeManager())
    monkeypatch.setattr(app_module, "get_store", lambda: object())
    monkeypatch.setattr(app_module.tasks, "scan_missing_terminal", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(app_module, "recover_interrupted_tasks", lambda: [])
    monkeypatch.setattr(app_module, "start_retention_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "get_probe_store", lambda: object())
    monkeypatch.setattr(app_module, "start_startup_probe", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module, "stop_startup_probe", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_executor", lambda **_kwargs: None)

    from fastapi.testclient import TestClient

    with TestClient(app_module.create_app()) as client:
        assert client.get("/api/health").status_code == 200

    assert calls == ["start", "cancel"]


def test_model_status_downgrades_when_files_disappear_or_smoke_fails(tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    for name in (
        "config.json", "model.bin", "shared_vocabulary.json", "source.spm", "target.spm",
        "vocab.json", "tokenizer_config.json",
    ):
        contents = b'["<unk>","hello"]' if "vocabulary.json" in name else b"x"
        (model_dir / name).write_bytes(contents)
    manager = TranslationModelManager(model_dir, runtime_factory=_runtime_factory)
    assert manager.status()["model_status"] == MODEL_STATUS_READY

    (model_dir / "model.bin").unlink()
    assert manager.is_ready() is False
    assert manager.status()["model_status"] == MODEL_STATUS_FAILED
    assert "不完整" in manager.status()["model_error"]
    manager._executor.shutdown(wait=True)


def test_runtime_load_failure_has_stable_missing_dependency_code(tmp_path):
    def missing_dependency(_path):
        raise ImportError("optional package absent")

    manager = TranslationModelManager(tmp_path / "model", runtime_factory=missing_dependency)
    with pytest.raises(TranslationModelError) as error:
        manager._load_runtime(tmp_path / "model")
    assert error.value.code == "missing_dependency"
    manager._executor.shutdown(wait=True)


@pytest.mark.parametrize("defect", ["missing_native_vocabulary", "empty_weight"])
def test_ready_model_is_downgraded_when_native_files_are_invalid(tmp_path, defect):
    model_dir = tmp_path / defect
    model_dir.mkdir()
    for name in (
        "config.json", "model.bin", "shared_vocabulary.json", "source.spm", "target.spm",
        "vocab.json", "tokenizer_config.json",
    ):
        contents = b'["<unk>","hello"]' if "vocabulary.json" in name else b"x"
        (model_dir / name).write_bytes(contents)
    manager = TranslationModelManager(model_dir, runtime_factory=_runtime_factory)
    assert manager.status()["model_status"] == MODEL_STATUS_READY

    if defect == "missing_native_vocabulary":
        (model_dir / "shared_vocabulary.json").unlink()
    else:
        (model_dir / "model.bin").write_bytes(b"")

    status = manager.status()
    assert status["model_status"] == MODEL_STATUS_FAILED
    assert manager.is_ready() is False
    assert "不完整" in status["model_error"]
    manager._executor.shutdown(wait=True)


def test_ready_model_accepts_separate_native_vocabularies(tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    for name in (
        "config.json", "model.bin", "source.spm", "target.spm", "vocab.json", "tokenizer_config.json",
        "source_vocabulary.json", "target_vocabulary.json",
    ):
        contents = b'["<unk>","hello"]' if "vocabulary.json" in name else b"x"
        (model_dir / name).write_bytes(contents)
    manager = TranslationModelManager(model_dir, runtime_factory=_runtime_factory)
    assert manager.status()["model_status"] == MODEL_STATUS_READY
    manager._executor.shutdown(wait=True)


def test_local_translation_preserves_language_prefix_eos_order_and_rejects_long_input(tmp_path):
    manager = TranslationModelManager(tmp_path / "model", runtime_factory=_runtime_factory)
    manager.model_dir.mkdir()
    for name in (
        "config.json", "model.bin", "shared_vocabulary.json", "source.spm", "target.spm",
        "vocab.json", "tokenizer_config.json",
    ):
        contents = b'["<unk>","hello"]' if "vocabulary.json" in name else b"x"
        (manager.model_dir / name).write_bytes(contents)
    try:
        results = manager.translate_texts(["one", "two words"])
        tokenizer, translator = manager._get_runtime()
        assert len(results) == 2
        assert tokenizer.encoded[0][0].startswith(">>cmn_Hans<< ")
        assert translator.batches[0][0][0][-1] == "2"
        assert translator.batches[0][1] == {
            "batch_type": "tokens",
            "max_batch_size": 2048,
            "beam_size": 2,
            "max_decoding_length": 512,
        }

        class TooLongTokenizer(_FakeTokenizer):
            model_max_length = 513

            def encode(self, text, add_special_tokens=True):
                return [1] * 513

        with pytest.raises(TranslationModelError) as error:
            manager._encode("long", TooLongTokenizer())
        assert error.value.code == "input_too_long"
        assert len(translator.batches) == 1
    finally:
        manager._executor.shutdown(wait=True)


def test_translation_core_local_batches_progress_and_cancel(monkeypatch):
    class FakeManager:
        def __init__(self):
            self.calls = []

        def translate_texts(self, batch, *, cancel_check=None):
            self.calls.append(list(batch))
            return [f"zh:{item}" for item in batch]

    manager = FakeManager()
    monkeypatch.setattr("src.core.translator.get_translation_model_manager", lambda: manager)
    monkeypatch.setattr(
        "src.core.translator.settings",
        types.SimpleNamespace(translate_batch_size=8, local_translate_batch_size=2),
    )
    progress = []
    translated = translate_texts(
        ["a", "b", "c"], "en", "zh-CN", engine_config={"api_type": "local_ct2"},
        on_batch=lambda done, total: progress.append((done, total)),
    )
    assert translated == ["zh:a", "zh:b", "zh:c"]
    assert manager.calls == [["a", "b"], ["c"]]
    assert progress == [(2, 3), (3, 3)]

    with pytest.raises(TranslateError) as error:
        translate_texts(["bonjour"], "fr", "zh-CN", engine_config={"api_type": "local_ct2"})
    assert error.value.code == "unsupported_language"

    def cancelled():
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        translate_texts(
            ["a"], "en", "zh", engine_config={"api_type": "local_ct2"}, cancel_check=cancelled,
        )


def test_translation_core_local_batch_defaults_to_sixteen(monkeypatch):
    class FakeManager:
        def __init__(self):
            self.calls = []

        def translate_texts(self, batch, *, cancel_check=None):
            self.calls.append(list(batch))
            return [f"zh:{item}" for item in batch]

    manager = FakeManager()
    monkeypatch.setattr("src.core.translator.get_translation_model_manager", lambda: manager)
    monkeypatch.setattr("src.core.translator.settings", types.SimpleNamespace(translate_batch_size=8))

    translated = translate_texts(
        [str(index) for index in range(17)],
        "en",
        "zh-CN",
        engine_config={"api_type": "local_ct2"},
    )

    assert len(translated) == 17
    assert [len(batch) for batch in manager.calls] == [16, 1]


def test_local_srt_bilingual_preserves_timestamps(monkeypatch, tmp_path):
    source = tmp_path / "source.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:02,500\nHello there.\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nGoodbye.\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "task"
    output_dir.mkdir()

    class FakeManager:
        def translate_texts(self, texts, *, cancel_check=None):
            return ["你好。", "再见。"]

    monkeypatch.setattr("src.core.translator.get_translation_model_manager", lambda: FakeManager())
    monkeypatch.setattr("src.core.translator.ensure_task_dir", lambda _task: output_dir)
    result = translate_srt(
        source, "task-local", "en", "zh-CN", mode="bilingual",
        engine_config={"api_type": "local_ct2"},
    )
    translated = parse_srt(result.srt_path)
    original = parse_srt(source)
    assert result.bilingual is True
    assert [(item.start, item.end) for item in translated] == [(item.start, item.end) for item in original]
    assert [item.text for item in translated] == ["Hello there.\n你好。", "Goodbye.\n再见。"]


def test_local_engine_creates_url_and_upload_tasks_without_api_key(monkeypatch, tmp_path):
    ready = True

    class FakeModelManager:
        def is_ready(self):
            return ready

    monkeypatch.setattr(task_routes, "get_translation_model_manager", lambda: FakeModelManager())
    monkeypatch.setattr(task_routes, "_ensure_local_model_ready", lambda _model: None)
    queued = []
    monkeypatch.setattr(task_routes, "enqueue_pipeline", queued.append)
    db = tmp_path / "tasks.db"
    task_store = TaskStore(db)
    engine_store = TranslationEngineStore(db)
    engine = engine_store.ensure_local_ct2()
    assert engine.api_key is None

    url_result = task_routes.create_task(
        TaskCreate(
            url="https://example.com/video.mp4", sourceLang="en", targetLang="zh-CN",
            model="replicate:tiny", engine=ENGINE_ID,
        ),
        store=task_store,
        engines=engine_store,
        task_origin="web",
    )
    assert url_result.engine == ENGINE_ID

    monkeypatch.setattr(task_routes, "task_dir", lambda task_id: tmp_path / "uploads" / task_id)
    monkeypatch.setattr(task_routes, "probe_duration", lambda *_args: 10)
    upload_result = task_routes.create_upload_task(
        file=UploadFile(filename="clip.mp4", file=io.BytesIO(b"video")),
        sourceLang="en", targetLang="zh", mode="bilingual", burn="soft",
        model="replicate:tiny", engine=ENGINE_ID, needSubtitle=True,
        ttsEnabled=False, ttsVoice="auto", originalVoiceMode="keep",
        store=task_store, engines=engine_store,
    )
    assert upload_result.engine == ENGINE_ID
    assert len(queued) == 2


def test_translation_engine_dependency_seeds_fixed_local_card(monkeypatch, tmp_path):
    from src.handler import deps

    monkeypatch.setattr(
        deps,
        "settings",
        types.SimpleNamespace(
            db_path=tmp_path / "settings.db",
            deepseek_api_key="",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_model="deepseek-chat",
        ),
    )
    monkeypatch.setattr(deps, "_translation_engine_store", None)
    try:
        store = deps.get_translation_engine_store()
        local = store.get(ENGINE_ID)
        assert local is not None
        assert local.api_type == "local_ct2"
        assert local.name == "本地 CPU 英译中"
        assert local.model == MODEL_REPO
        assert local.base_url == ""
        assert local.api_key is None
        assert local.availability == "UNKNOWN"
    finally:
        deps.reset_singletons()


def test_local_engine_is_the_default_for_api_and_pipeline():
    body = TaskCreate(url="https://example.com/video.mp4")
    assert body.sourceLang == "en"
    assert body.targetLang == "zh-CN"
    assert body.engine == DEFAULT_TRANSLATION_ENGINE_ID
    params = PipelineParams(task_id="t1", url=body.url, source_lang=body.sourceLang, target_lang=body.targetLang)
    assert params.engine == DEFAULT_TRANSLATION_ENGINE_ID


def test_missing_local_dependency_is_rejected_before_task_enqueue(monkeypatch, tmp_path):
    class MissingDependencyManager:
        def is_ready(self):
            return True

        def dependency_status(self):
            return {"ready": False, "missing": ["ctranslate2"]}

    monkeypatch.setattr(task_routes, "get_translation_model_manager", lambda: MissingDependencyManager())
    store = TranslationEngineStore(tmp_path / "tasks.db")
    store.ensure_local_ct2()
    with pytest.raises(HTTPException) as error:
        task_routes._ensure_translation_engine(DEFAULT_TRANSLATION_ENGINE_ID, True, store)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "LOCAL_TRANSLATION_DEPENDENCY_MISSING"
    assert "ctranslate2" in error.value.detail["message"]


def test_local_engine_rejects_unsupported_languages_and_unready_model(monkeypatch, tmp_path):
    class ReadyManager:
        def is_ready(self):
            return False

    monkeypatch.setattr(task_routes, "get_translation_model_manager", lambda: ReadyManager())
    store = TranslationEngineStore(tmp_path / "tasks.db")
    engine = store.ensure_local_ct2()
    with pytest.raises(HTTPException) as unsupported:
        task_routes._ensure_supported_translation_languages(ENGINE_ID, True, "auto", "zh-CN")
    assert unsupported.value.status_code == 422
    with pytest.raises(HTTPException) as not_ready:
        task_routes._ensure_translation_engine(ENGINE_ID, True, store)
    assert not_ready.value.status_code == 409
    assert not_ready.value.detail["code"] == "LOCAL_TRANSLATION_MODEL_NOT_READY"


def test_local_validation_is_offline_smoke(tmp_path, monkeypatch):
    from src.handler import translation_engines as engine_routes

    class FakeManager:
        def __init__(self):
            self.validated = 0

        def validate_offline(self):
            self.validated += 1

        def status(self):
            return {"model_status": MODEL_STATUS_READY, "installed_bytes": 12, "model_error": None}

        def is_ready(self):
            return True

    manager = FakeManager()
    monkeypatch.setattr(engine_routes, "get_translation_model_manager", lambda: manager)
    store = TranslationEngineStore(tmp_path / "engines.db")
    local = store.ensure_local_ct2()
    checked = engine_routes._validate_engine(local.id, store)
    assert checked.available is True
    assert checked.availability == "AVAILABLE"
    assert manager.validated == 1
    out = engine_routes._out(local)
    assert out.hasApiKey is False
    assert out.modelStatus == MODEL_STATUS_READY
    assert out.installedBytes == 12
    assert out.supportedSourceLanguages == ["en"]
    assert out.supportedTargetLanguages == ["zh-CN", "zh"]


def test_local_engine_download_endpoint_returns_accepted_status(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.handler.app import app
    from src.handler.deps import get_translation_engine_store, reset_singletons
    from src.handler import translation_engines as engine_routes

    class FakeManager:
        def __init__(self):
            self.started = 0

        def start_download(self):
            self.started += 1

        def status(self):
            return {"model_status": MODEL_STATUS_DOWNLOADING, "installed_bytes": 87, "model_error": None}

        def is_ready(self):
            return False

    manager = FakeManager()
    store = TranslationEngineStore(tmp_path / "engines.db")
    store.ensure_local_ct2()
    reset_singletons()
    app.dependency_overrides[get_translation_engine_store] = lambda: store
    monkeypatch.setattr(engine_routes, "get_translation_model_manager", lambda: manager)
    try:
        with TestClient(app) as client:
            accepted = client.post(f"/api/settings/translation-engines/{ENGINE_ID}/download")
            assert accepted.status_code == 202
            payload = accepted.json()
            assert payload["modelStatus"] == MODEL_STATUS_DOWNLOADING
            assert payload["installedBytes"] == 87
            assert payload["hasApiKey"] is False
            assert payload["baseUrl"] == ""
            rejected = client.post("/api/settings/translation-engines/deepseek/download")
            assert rejected.status_code == 404
        assert manager.started == 1
    finally:
        app.dependency_overrides.clear()
        reset_singletons()


def test_readiness_accepts_enabled_ready_local_engine_without_deepseek_key(monkeypatch):
    from types import SimpleNamespace
    from src.handler import deps
    from src.service import runtime_check

    monkeypatch.delenv("SUBTRANS_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    from dataclasses import replace
    monkeypatch.setattr(runtime_check, "settings", replace(runtime_check.settings, deepseek_api_key=""))
    monkeypatch.setattr(runtime_check, "_configured_replicate_token", lambda: None)
    monkeypatch.setattr(runtime_check, "_check_binary", lambda _command: "available")
    monkeypatch.setattr(runtime_check, "_check_writable_directory", lambda _path: "writable")
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _module: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _command: True)
    monkeypatch.setattr(
        deps,
        "get_translation_engine_store",
        lambda: types.SimpleNamespace(get=lambda _engine_id: SimpleNamespace(enabled=1)),
    )
    monkeypatch.setattr(
        runtime_check,
        "get_translation_model_manager",
        lambda: types.SimpleNamespace(is_ready=lambda: True),
    )

    readiness = runtime_check.build_readiness()
    assert readiness["checks"]["deepseek_api_key"] == "missing"
    assert readiness["checks"]["local_translation_model"] == "available"
    assert readiness["initialized"] is True
