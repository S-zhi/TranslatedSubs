from __future__ import annotations

import pytest

from src.config import config
from src.config.loader import ConfigError


def test_secret_file_rejects_non_secret_settings(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SUBTRANS_WORKERS=3\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="不允许的配置键"):
        config._parse_env_lines(env_path)


def test_process_environment_rejects_legacy_app_keys_but_allows_auxiliary_keys(monkeypatch):
    monkeypatch.setenv("SUBTRANS_WORKERS", "3")
    with pytest.raises(ConfigError, match="SUBTRANS_WORKERS"):
        config._validate_process_environment()
    monkeypatch.delenv("SUBTRANS_WORKERS")

    monkeypatch.setenv("SUBTRANS_MCP_PORT", "3001")
    monkeypatch.setenv("SUBTRANS_DRIVE_TIMEOUT", "600")
    monkeypatch.setenv("SUBTRANS_API_BASE_URL", "http://127.0.0.1:8000")
    config._validate_process_environment()
