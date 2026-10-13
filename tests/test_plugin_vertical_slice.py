from pathlib import Path
from types import SimpleNamespace

import pytest

from src.plugin_system.context import FakeContext
from src.plugin_system.contracts import TranslateRequest, TranslateResult
from src.plugin_system.errors import PluginCancelled, PluginError
from src.plugin_system.manager import PluginManager
from src.plugin_system.registry import PluginRegistry
from src.plugins.builtin import translator_engine
from src.store.translation_engine_store import LOCAL_TRANSLATION_MODEL


_VALID_SRT = "1\n00:00:00,000 --> 00:00:01,000\ntranslated\n"


def _config():
    return {
        "engine_id": "local-engine",
        "api_type": "local_ct2",
        "base_url": "",
        "model": LOCAL_TRANSLATION_MODEL,
        "credential_ref": None,
        "batch_size": 7,
        "local_batch_size": 13,
        "timeout_seconds": 42,
    }


class _TrackingFactory:
    """Track the real A5 session and optionally inject a close failure."""

    stage = translator_engine.factory.stage

    def __init__(self, close_error: Exception | None = None):
        self.close_error = close_error
        self.sessions = []

    def create_session(self, config, context):
        session = translator_engine.factory.create_session(config, context)
        original_close = session.close

        def close():
            original_close()
            if self.close_error is not None:
                raise self.close_error

        session.close = close
        self.sessions.append(session)
        return session


def _manager(factory: _TrackingFactory) -> PluginManager:
    registry = PluginRegistry()
    registry.register(translator_engine.factory.manifest, factory)
    return PluginManager(registry)


def _request(task_dir: Path) -> TranslateRequest:
    source = task_dir / "original.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\nsource\n", encoding="utf-8")
    return TranslateRequest(
        stage="translate",
        operation="translate",
        purpose="translate",
        original_srt=source,
        output_srt=task_dir / "translated.srt",
        source_lang="en",
        target_lang="zh-CN",
        mode="bilingual",
    )


def _fake_success(task_dir: Path):
    def translate(*args, **kwargs):
        output = task_dir / "translated.srt"
        output.write_text(_VALID_SRT, encoding="utf-8")
        kwargs["on_progress"](SimpleNamespace(percent=42.0))
        return translator_engine.translator.TranslateResult(output, 1, True)

    return translate


def _run(manager, task_dir, context):
    return manager.run(
        translator_engine.PLUGIN_ID,
        "1",
        _request(task_dir),
        _config(),
        context,
    )


def test_manager_real_translation_adapter_success_validates_and_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        translator_engine.translator, "translate_srt", _fake_success(tmp_path)
    )
    context = FakeContext(task_id="task-g1", task_dir=tmp_path, purpose="translate")
    factory = _TrackingFactory()
    result = _run(_manager(factory), tmp_path, context)

    assert result == TranslateResult(tmp_path / "translated.srt", 1, True)
    assert (tmp_path / "translated.srt").read_text(encoding="utf-8") == _VALID_SRT
    assert context.progress == [42.0]
    assert len(factory.sessions) == 1
    assert factory.sessions[0]._closed is True


def test_manager_preserves_adapter_error_and_cause_when_close_also_fails(
    tmp_path, monkeypatch
):
    failure = RuntimeError("provider failure")

    def fail_translation(*args, **kwargs):
        raise failure

    monkeypatch.setattr(translator_engine.translator, "translate_srt", fail_translation)
    factory = _TrackingFactory(close_error=RuntimeError("close failure"))
    with pytest.raises(PluginError) as raised:
        _run(
            _manager(factory), tmp_path,
            FakeContext(task_id="task-g1", task_dir=tmp_path, purpose="translate"),
        )

    assert raised.value.code == "execution_error"
    assert raised.value.__cause__ is failure
    assert isinstance(raised.value.cleanup_error, RuntimeError)
    assert factory.sessions[0]._closed is True


def test_manager_propagates_cancellation_and_closes_session(tmp_path, monkeypatch):
    context = FakeContext(task_id="task-g1", task_dir=tmp_path, purpose="translate")

    def cancel_during_translation(*args, **kwargs):
        context.cancelled = True
        kwargs["cancel_check"]()

    monkeypatch.setattr(
        translator_engine.translator, "translate_srt", cancel_during_translation
    )
    factory = _TrackingFactory(close_error=RuntimeError("close failure"))
    with pytest.raises(PluginCancelled):
        _run(_manager(factory), tmp_path, context)
    assert factory.sessions[0]._closed is True


def test_manager_reports_cleanup_error_after_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        translator_engine.translator, "translate_srt", _fake_success(tmp_path)
    )
    factory = _TrackingFactory(close_error=RuntimeError("close failure"))
    with pytest.raises(PluginError) as raised:
        _run(
            _manager(factory), tmp_path,
            FakeContext(task_id="task-g1", task_dir=tmp_path, purpose="translate"),
        )
    assert raised.value.code == "cleanup_error"
    assert (tmp_path / "translated.srt").is_file()
    assert factory.sessions[0]._closed is True
