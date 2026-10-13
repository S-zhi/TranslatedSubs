"""前端可动态配置的人声分离设置 API 契约测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.handler import audio_settings
from src.handler import deps
from src.config.redis_store import ConfigUnavailable, RevisionConflict


@pytest.fixture
def audio_client(monkeypatch, tmp_path):
    """隔离 API token 与运行时设置，避免写入真实 data/.runtime-settings.json。"""
    values = {
        "vocal_separation_enabled": False,
        "vocal_separation_backend": "demucs",
        "vocal_separation_model": "htdemucs",
        "vocal_separation_threads": 1,
        "vocal_separation_timeout": 1800,
        "ffmpeg_bin": "ffmpeg",
    }
    monkeypatch.setattr(audio_settings, "settings", SimpleNamespace(**values))
    monkeypatch.setattr(deps, "settings", SimpleNamespace(api_token=None, db_path=tmp_path / "test.db"))
    monkeypatch.setattr(audio_settings, "_availability", lambda: (True, True, True, None))
    test_app = FastAPI()
    test_app.include_router(audio_settings.router)
    with TestClient(test_app) as client:
        yield client, values


def test_get_audio_settings_exposes_runtime_state(audio_client):
    client, _ = audio_client

    response = client.get("/api/settings/audio")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "backend": "demucs",
        "model": "htdemucs",
        "threads": 1,
        "timeout": 1800,
        "demucsInstalled": True,
        "ffmpegAvailable": True,
        "ready": True,
        "message": None,
    }


def test_put_audio_settings_allows_safe_fields_and_returns_state(audio_client, monkeypatch):
    client, values = audio_client
    seen = {}

    def fake_update(payload):
        seen.update(payload)
        values.update(payload)
        for key, value in payload.items():
            setattr(audio_settings.settings, key, value)
        return dict(payload)

    monkeypatch.setattr(audio_settings, "update_runtime_settings", fake_update)

    response = client.put(
        "/api/settings/audio",
        json={"enabled": True, "model": "mdx_q", "threads": 4, "timeout": 3600},
    )

    assert response.status_code == 200
    assert seen == {
        "vocal_separation_enabled": True,
        "vocal_separation_model": "mdx_q",
        "vocal_separation_threads": 4,
        "vocal_separation_timeout": 3600,
    }
    assert response.json()["enabled"] is True
    assert response.json()["model"] == "mdx_q"
    assert response.json()["threads"] == 4
    assert response.json()["timeout"] == 3600


@pytest.mark.parametrize(
    "payload",
    [
        {"enabled": True, "model": "spleeter", "threads": 1, "timeout": 1800},
        {"enabled": True, "model": "htdemucs", "threads": 0, "timeout": 1800},
        {"enabled": True, "model": "htdemucs", "threads": 9, "timeout": 1800},
        {"enabled": True, "model": "htdemucs", "threads": 1, "timeout": 59},
        {"enabled": True, "model": "htdemucs", "threads": 1, "timeout": 86401},
    ],
)
def test_put_audio_settings_rejects_invalid_values(audio_client, payload):
    client, _ = audio_client

    response = client.put("/api/settings/audio", json=payload)

    assert response.status_code == 422


def test_put_audio_settings_does_not_accept_backend_or_command(audio_client, monkeypatch):
    client, _ = audio_client
    response = client.put(
        "/api/settings/audio",
        json={
            "enabled": True,
            "model": "htdemucs",
            "threads": 1,
            "timeout": 1800,
            "backend": "shell",
            "command": "rm -rf /",
        },
    )

    assert response.status_code == 422


def test_put_audio_settings_requires_api_token_when_configured(audio_client, monkeypatch):
    client, _ = audio_client
    monkeypatch.setattr(deps, "settings", SimpleNamespace(api_token="secret"))
    monkeypatch.setattr(audio_settings, "update_runtime_settings", lambda payload: payload)

    assert client.put(
        "/api/settings/audio",
        json={"enabled": True, "model": "htdemucs", "threads": 1, "timeout": 1800},
    ).status_code == 401

    response = client.put(
        "/api/settings/audio",
        headers={"X-API-Token": "secret"},
        json={"enabled": True, "model": "htdemucs", "threads": 1, "timeout": 1800},
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("error", "status"),
    [(ConfigUnavailable("down"), 503), (RevisionConflict("stale"), 409)],
)
def test_put_audio_settings_maps_redis_failures(audio_client, monkeypatch, error, status):
    def raise_error(_payload):
        raise error

    monkeypatch.setattr(audio_settings, "update_runtime_settings", raise_error)
    client, _ = audio_client
    response = client.put(
        "/api/settings/audio",
        json={"enabled": True, "model": "htdemucs", "threads": 1, "timeout": 1800},
    )
    assert response.status_code == status
