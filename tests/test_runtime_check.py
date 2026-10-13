from types import SimpleNamespace
import src.service.runtime_check as runtime_check


def _settings(tmp_path):
    return SimpleNamespace(
        backend_dir=tmp_path, data_dir=tmp_path / "data", db_path=tmp_path / "state" / "app.db",
        ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", deepseek_api_key=None, api_token=None,
        max_upload_mb=2048, max_video_minutes=180, pipeline_workers=2,
    )


def _ready(monkeypatch):
    import src.handler.deps as deps
    monkeypatch.setattr(deps, "get_translation_engine_store", lambda: SimpleNamespace(get=lambda _: SimpleNamespace(enabled=True)))
    monkeypatch.setattr(runtime_check, "get_translation_model_manager", lambda: SimpleNamespace(is_ready=lambda: True, dependency_status=lambda: {"ready": True, "missing": []}))
    monkeypatch.setattr(runtime_check.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)
    monkeypatch.setattr(runtime_check, "_check_writable_directory", lambda _: "writable")


def test_readiness_has_no_removed_backend_fields_or_external_check(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    _ready(monkeypatch)
    result = runtime_check.build_readiness()
    assert result["initialized"] is True
    assert not any("cloud" in key.lower() for key in result)
    assert not any("cloud" in key.lower() for key in result["checks"])


def test_readiness_reports_missing_local_translation(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    import src.handler.deps as deps
    monkeypatch.setattr(deps, "get_translation_engine_store", lambda: SimpleNamespace(get=lambda _: SimpleNamespace(enabled=True)))
    monkeypatch.setattr(runtime_check, "get_translation_model_manager", lambda: SimpleNamespace(is_ready=lambda: False, dependency_status=lambda: {"ready": True, "missing": []}))
    monkeypatch.setattr(runtime_check.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)
    monkeypatch.setattr(runtime_check, "_check_writable_directory", lambda _: "writable")
    result = runtime_check.build_readiness()
    assert result["initialized"] is False
    assert any("本地翻译模型未就绪" in item for item in result["missing"])


def test_readiness_reports_missing_local_dependencies(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    import src.handler.deps as deps
    monkeypatch.setattr(deps, "get_translation_engine_store", lambda: SimpleNamespace(get=lambda _: SimpleNamespace(enabled=True)))
    monkeypatch.setattr(runtime_check, "get_translation_model_manager", lambda: SimpleNamespace(is_ready=lambda: True, dependency_status=lambda: {"ready": False, "missing": ["ctranslate2"]}))
    monkeypatch.setattr(runtime_check.shutil, "which", lambda _: "/usr/bin/tool")
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)
    monkeypatch.setattr(runtime_check, "_check_writable_directory", lambda _: "writable")
    result = runtime_check.build_readiness()
    assert result["initialized"] is False
    assert result["checks"]["local_translation_dependencies"] == "missing"
    assert any("ctranslate2" in item for item in result["missing"])


def test_process_environment_key_is_ignored_and_does_not_bypass_missing_local_model(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    import src.handler.deps as deps
    monkeypatch.setattr(deps, "get_translation_engine_store", lambda: SimpleNamespace(get=lambda _: SimpleNamespace(enabled=True)))
    monkeypatch.setattr(runtime_check, "get_translation_model_manager", lambda: SimpleNamespace(is_ready=lambda: False, dependency_status=lambda: {"ready": True, "missing": []}))
    monkeypatch.setattr(runtime_check.shutil, "which", lambda _: "/usr/bin/tool")
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)
    monkeypatch.setattr(runtime_check, "_check_writable_directory", lambda _: "writable")
    result = runtime_check.build_readiness()
    assert result["checks"]["deepseek_api_key"] == "missing"
    assert result["checks"]["translation_engine"] == "missing"
    assert result["initialized"] is False


def test_readiness_allows_soft_burn_when_subtitle_filter_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    _ready(monkeypatch)
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: False)
    result = runtime_check.build_readiness()
    assert result["initialized"] is True
    assert result["ok"] is False
    assert result["capabilities"]["soft_burn"] is True
    assert result["agent_action"] == "use_soft_burn_or_install_libass"


def test_readiness_reports_saved_unified_key_without_returning_secret(monkeypatch, tmp_path):
    from src.store import ModelSettingsStore
    import src.handler.deps as deps

    current = _settings(tmp_path)
    monkeypatch.setattr(runtime_check, "settings", current)
    ModelSettingsStore(current.db_path).update({
        "llm": {
            "provider": "openai_compatible",
            "model": "saved-model",
            "baseUrl": "https://example.test/v1",
            "apiKey": "sk-readiness-secret",
            "models": [{
                "id": "llm-deepseek",
                "provider": "openai_compatible",
                "model": "saved-model",
                "baseUrl": "https://example.test/v1",
                "isCurrent": True,
            }],
        },
    })
    monkeypatch.setattr(deps, "get_translation_engine_store", lambda: SimpleNamespace(get=lambda _: SimpleNamespace(enabled=True)))
    monkeypatch.setattr(runtime_check, "get_translation_model_manager", lambda: SimpleNamespace(is_ready=lambda: True, dependency_status=lambda: {"ready": True, "missing": []}))
    monkeypatch.setattr(runtime_check, "_check_binary", lambda _: "available")
    monkeypatch.setattr(runtime_check, "_check_writable_directory", lambda _: "writable")
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)

    result = runtime_check.build_readiness()

    assert result["checks"]["deepseek_api_key"] == "available"
    assert result["model_settings"]["llm"]["hasApiKey"] is True
    assert result["effective"]["llm"]["source"] == "unified"
    assert "sk-readiness-secret" not in str(result)


def test_default_whisper_status_matches_get_without_activating_settings(monkeypatch, tmp_path):
    import dataclasses
    from fastapi.testclient import TestClient
    from src.config import settings as base_settings
    from src.handler.app import app
    from src.handler import deps, model_settings as model_routes
    from src.service.model_manager import model_manager
    from src.store import ModelSettingsStore

    current = _settings(tmp_path)
    monkeypatch.setattr(runtime_check, "settings", current)
    monkeypatch.setattr(model_routes, "settings", dataclasses.replace(base_settings, db_path=current.db_path))
    monkeypatch.setattr(deps, "settings", dataclasses.replace(base_settings, db_path=current.db_path))
    monkeypatch.setattr(model_manager, "list_models", lambda: [{"name": "small", "status": "NOT_INSTALLED"}])
    monkeypatch.setattr(model_manager, "is_ready", lambda _name: False)
    _ready(monkeypatch)
    with TestClient(app) as client:
        public = client.get("/api/settings/models").json()["whisper"]
    readiness = runtime_check.build_readiness()
    effective = readiness["effective"]["whisper"]
    assert public["runtimeManaged"] is False
    assert public["status"] == "NOT_INSTALLED"
    assert public["model"] == effective["model"] == "local:small"
    assert public["status"] == readiness["model_settings"]["whisper"]["status"]
    assert effective["ready"] is False
    assert ModelSettingsStore(current.db_path).get("whisper")["_runtimeManaged"] is False
