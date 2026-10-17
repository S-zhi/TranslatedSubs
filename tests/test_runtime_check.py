import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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


def test_check_writable_directory_creates_directory(tmp_path):
    path = tmp_path / "new" / "nested"

    assert runtime_check._check_writable_directory(path) == "writable"
    assert path.is_dir()


def test_check_writable_directory_reports_not_writable(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "access", lambda path, mode: False)

    assert runtime_check._check_writable_directory(tmp_path) == "not_writable"


def test_check_writable_directory_reports_unavailable(tmp_path):
    path = tmp_path / "file"
    path.write_text("not a directory", encoding="utf-8")

    assert runtime_check._check_writable_directory(path) == "unavailable"
    assert runtime_check._check_writable_directory(path / "nested") == "unavailable"


@pytest.mark.parametrize("directory", ["data_directory", "database_directory"])
@pytest.mark.parametrize("status", ["writable", "not_writable", "unavailable"])
def test_health_ready_reports_directory_status(monkeypatch, tmp_path, directory, status):
    from src.handler.health import router

    current = _settings(tmp_path)
    monkeypatch.setattr(runtime_check, "settings", current)
    _ready(monkeypatch)
    paths = {
        "data_directory": current.data_dir,
        "database_directory": current.db_path.parent,
    }
    target = paths[directory]
    if status == "unavailable":
        target.write_text("not a directory", encoding="utf-8")
    elif status == "not_writable":
        access = os.access
        monkeypatch.setattr(
            os, "access", lambda path, mode: False if path == target else access(path, mode),
        )

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    assert response.status_code == 200
    result = response.json()
    ready = status == "writable"
    for name, path in paths.items():
        assert result["checks"][name] == (status if name == directory else "writable")
        assert path.is_dir() is (status != "unavailable" or name != directory)
    assert result["ok"] is ready
    assert result["initialized"] is ready
    for capability in ("download", "full_pipeline", "soft_burn"):
        assert result["capabilities"][capability] is ready
    if ready:
        assert result["missing"] == []
        assert result["agent_action"] == "continue"
    else:
        hint = "数据目录可写权限" if directory == "data_directory" else "数据库目录可写权限"
        assert len(result["missing"]) == 1
        assert hint in result["missing"][0]
        assert result["agent_action"] == "ask_user_to_configure"


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
