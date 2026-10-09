from types import SimpleNamespace

import src.service.runtime_check as runtime_check


def _settings(tmp_path, *, deepseek_api_key=None, api_token=None):
    return SimpleNamespace(
        backend_dir=tmp_path,
        data_dir=tmp_path / "data",
        db_path=tmp_path / "state" / "app.db",
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        deepseek_api_key=deepseek_api_key,
        api_token=api_token,
        max_upload_mb=2048,
        max_video_minutes=180,
        pipeline_workers=2,
    )


def _mock_local_engine(
    monkeypatch, *, model_ready: bool, dependencies_ready: bool, enabled: bool = True,
    missing_dependencies=(),
):
    import src.handler.deps as deps

    monkeypatch.setattr(
        deps,
        "get_translation_engine_store",
        lambda: SimpleNamespace(
            get=lambda engine_id: SimpleNamespace(id=engine_id, enabled=enabled)
        ),
    )
    monkeypatch.setattr(
        runtime_check,
        "get_translation_model_manager",
        lambda: SimpleNamespace(
            is_ready=lambda: model_ready,
            dependency_status=lambda: {
                "ready": dependencies_ready,
                "missing": list(missing_dependencies),
            },
        ),
    )


def _mock_ready_local_engine(monkeypatch):
    _mock_local_engine(monkeypatch, model_ready=True, dependencies_ready=True)


def test_readiness_reports_fixed_config_location_and_missing_values(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    monkeypatch.delenv("SUBTRANS_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(runtime_check.shutil, "which", lambda _: None)
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: False)
    _mock_local_engine(
        monkeypatch,
        model_ready=False,
        dependencies_ready=False,
        missing_dependencies=("ctranslate2", "transformers"),
    )

    result = runtime_check.build_readiness()

    assert result["ok"] is False
    assert result["initialized"] is False
    assert result["config_file"] == str(tmp_path / ".env")
    assert "REPLICATE_API_TOKEN" not in result["missing"]
    assert "REPLICATE_API_TOKEN" not in result["required_environment"]
    assert any("本地翻译依赖缺失" in item for item in result["missing"])
    assert result["capabilities"]["download"] is False
    assert result["agent_action"] == "ask_user_to_configure"
    assert result["restart_required"] is False


def test_app_health_starts_without_local_optional_dependencies_and_readiness_names_them(
    monkeypatch, tmp_path
):
    """The API can start without CT2 packages, while readiness is explicit."""
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    # A legacy key is reported, but it cannot make the default local pipeline
    # ready when the local optional dependencies are absent.
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "legacy-deepseek-key")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(runtime_check.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)
    _mock_local_engine(
        monkeypatch,
        model_ready=True,
        dependencies_ready=False,
        missing_dependencies=("ctranslate2", "transformers", "sentencepiece"),
    )

    result = runtime_check.build_readiness()
    assert result["checks"]["local_translation_dependencies"] == "missing"
    assert result["checks"]["local_translation_model"] == "available"
    assert result["checks"]["translation_engine"] == "missing"
    assert result["checks"]["deepseek_api_key"] == "available"
    assert result["initialized"] is False
    assert result["restart_required"] is False
    assert any("本地翻译依赖缺失" in item for item in result["missing"])

    # Health is a lightweight liveness endpoint and must not import optional
    # model packages or fail merely because the local engine is not installed.
    from fastapi.testclient import TestClient
    from src.handler.app import app

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200


def test_deepseek_key_does_not_bypass_missing_local_model(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runtime_check,
        "settings",
        _settings(tmp_path, deepseek_api_key="legacy-deepseek-key"),
    )
    monkeypatch.setattr(runtime_check, "_configured_replicate_token", lambda: None)
    monkeypatch.setattr(runtime_check, "_check_binary", lambda _command: "available")
    monkeypatch.setattr(runtime_check, "_check_writable_directory", lambda _path: "writable")
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _module: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _command: True)
    _mock_local_engine(monkeypatch, model_ready=False, dependencies_ready=True)

    result = runtime_check.build_readiness()

    assert result["checks"]["deepseek_api_key"] == "available"
    assert result["checks"]["translation_engine"] == "missing"
    assert result["initialized"] is False
    assert result["restart_required"] is False
    assert any("本地翻译模型未就绪" in item for item in result["missing"])


def test_readiness_reports_capabilities_without_exposing_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "replicate-secret")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "deepseek-secret")
    _mock_ready_local_engine(monkeypatch)
    monkeypatch.setattr(
        runtime_check.shutil,
        "which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)
    monkeypatch.setattr(
        runtime_check,
        "query_replicate_balance",
        lambda: {"status": "unsupported", "authenticated": True},
    )

    result = runtime_check.build_readiness()

    assert result["ok"] is True
    assert result["initialized"] is True
    assert result["capabilities"] == {
        "download": True,
        "ffprobe_available": True,
        "full_pipeline": True,
        "hard_burn": True,
        "soft_burn": True,
        "max_concurrent_tasks": 2,
        "max_concurrent_downloads": 2,
    }
    assert result["limits"] == {
        "max_upload_mb": 2048,
        "max_video_minutes": 180,
    }
    assert result["agent_action"] == "continue"
    assert result["restart_required"] is False
    assert "replicate-secret" not in str(result)
    assert "deepseek-secret" not in str(result)


def test_readiness_guides_agent_to_soft_burn_when_libass_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "replicate-secret")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "deepseek-secret")
    _mock_ready_local_engine(monkeypatch)
    monkeypatch.setattr(
        runtime_check.shutil,
        "which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: False)
    monkeypatch.setattr(
        runtime_check,
        "query_replicate_balance",
        lambda: {"status": "unsupported", "authenticated": True},
    )

    result = runtime_check.build_readiness()

    assert result["ok"] is False
    assert result["initialized"] is True
    assert result["agent_action"] == "use_soft_burn_or_install_libass"
    assert result["restart_required"] is False
    assert any("FFmpeg subtitles 滤镜" in item for item in result["missing"])


def test_readiness_reports_invalid_replicate_token(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "invalid-token")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "deepseek-secret")
    _mock_ready_local_engine(monkeypatch)
    monkeypatch.setattr(
        runtime_check.shutil,
        "which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)
    monkeypatch.setattr(
        runtime_check,
        "query_replicate_balance",
        lambda: {
            "status": "error",
            "errorCode": "invalid_api_token",
            "message": "Replicate API Token 无效、已过期或没有访问权限",
        },
    )

    result = runtime_check.build_readiness()

    assert result["ok"] is True
    assert result["initialized"] is True
    assert result["checks"]["replicate_api_token"] == "invalid"
    assert "REPLICATE_API_TOKEN（Token 无效或已过期）" not in result["missing"]


def test_readiness_reports_cached_replicate_check_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "replicate-secret")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "deepseek-secret")
    _mock_ready_local_engine(monkeypatch)
    monkeypatch.setattr(
        runtime_check.shutil,
        "which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)
    monkeypatch.setattr(
        runtime_check,
        "query_replicate_balance",
        lambda: {
            "status": "unsupported",
            "authenticated": True,
            "checkedAt": 1700000000000,
            "cached": True,
        },
    )

    result = runtime_check.build_readiness()

    assert result["ok"] is True
    assert result["replicate_checked_at"] == 1700000000000
    assert result["replicate_cached"] is True
    assert result["checks"]["replicate_checked_at"] == 1700000000000
    assert result["checks"]["replicate_cached"] is True


def test_readiness_reports_api_token_required(monkeypatch, tmp_path):
    s = _settings(tmp_path, api_token="secret-token")
    monkeypatch.setattr(runtime_check, "settings", s)
    result = runtime_check.build_readiness()
    assert result["checks"]["api_token_required"] is True

    s_empty = _settings(tmp_path, api_token=None)
    monkeypatch.setattr(runtime_check, "settings", s_empty)
    result_empty = runtime_check.build_readiness()
    assert result_empty["checks"]["api_token_required"] is False


def test_readiness_handles_unavailable_replicate_check_gracefully(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "replicate-secret")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "deepseek-secret")
    _mock_ready_local_engine(monkeypatch)
    monkeypatch.setattr(
        runtime_check.shutil,
        "which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)
    monkeypatch.setattr(
        runtime_check,
        "query_replicate_balance",
        lambda: {
            "status": "unavailable",
            "errorCode": "http_429",
            "checkedAt": 1700000000000,
            "cached": False,
        },
    )

    result = runtime_check.build_readiness()

    assert result["ok"] is True
    assert result["initialized"] is True
    assert result["checks"]["replicate_api_token"] == "unavailable"
    assert result["agent_action"] == "continue"


def test_readiness_handles_replicate_query_exception_without_leaking_details(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "replicate-secret")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "deepseek-secret")
    _mock_ready_local_engine(monkeypatch)
    monkeypatch.setattr(runtime_check.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)

    def fail_query():
        raise RuntimeError("upstream secret details")

    monkeypatch.setattr(runtime_check, "query_replicate_balance", fail_query)
    result = runtime_check.build_readiness()
    assert result["ok"] is True
    assert result["initialized"] is True
    assert result["checks"]["replicate_api_token"] == "network_error"
    assert result["replicate_checked_at"] is not None
    assert result["replicate_cached"] is False
    assert "Replicate 服务暂时不可达，请检查网络连接" not in result["missing"]
    assert "upstream secret details" not in str(result)


def test_readiness_normalizes_non_dict_replicate_result(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_check, "settings", _settings(tmp_path))
    monkeypatch.setenv("REPLICATE_API_TOKEN", "replicate-secret")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "deepseek-secret")
    _mock_ready_local_engine(monkeypatch)
    monkeypatch.setattr(runtime_check.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(runtime_check.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(runtime_check, "has_subtitles_filter", lambda _: True)
    monkeypatch.setattr(runtime_check, "query_replicate_balance", lambda: ["invalid"])
    result = runtime_check.build_readiness()
    assert result["checks"]["replicate_api_token"] == "network_error"
    assert result["initialized"] is True
    assert result["replicate_checked_at"] is not None
