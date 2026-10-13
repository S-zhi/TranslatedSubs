from __future__ import annotations

from fastapi.testclient import TestClient

from src.handler.app import app
from src.handler.model_settings import _store


def test_model_settings_defaults_and_masked_key(tmp_path, monkeypatch):
    monkeypatch.setattr("src.handler.model_settings.settings", type("S", (), {
        "db_path": tmp_path / "models.db", "deepseek_api_key": "secret",
    })())
    result = _store().get_all()
    assert set(result) == {"llm", "whisper", "tts"}
    assert result["llm"]["models"][0]["id"] == "llm-deepseek"
    assert not result["llm"]["models"][0]["apiKey"]
    assert result["tts"]["voice"] == "auto"


def test_model_settings_routes_partial_update_and_create(tmp_path, monkeypatch):
    monkeypatch.setattr("src.handler.model_settings.settings", type("S", (), {
        "db_path": tmp_path / "models.db", "deepseek_api_key": "secret",
    })())
    monkeypatch.setattr("src.handler.deps.settings", type("S", (), {"api_token": None, "db_path": tmp_path / "models.db"})())
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        response = client.get("/api/settings/models")
        assert response.status_code == 200
        assert response.json()["llm"]["hasApiKey"] is False
        assert "apiKey" not in response.json()["llm"]

        updated = client.put("/api/settings/models", json={"llm": {"model": "deepseek-reasoner", "apiKey": ""}})
        assert updated.status_code == 200
        assert updated.json()["llm"]["model"] == "deepseek-reasoner"
        assert updated.json()["llm"]["hasApiKey"] is False

        created = client.post("/api/settings/models/tts", json={
            "id": "client-supplied-id", "name": "Demo", "provider": "kokoro",
            "model": "kokoro-v1", "voice": "af_heart", "apiKey": "private",
        })
        assert created.status_code == 201
        assert created.json()["model"]["id"].startswith("tts-")
        assert created.json()["model"]["hasApiKey"] is True
        assert "apiKey" not in created.json()["model"]
        assert "api_key" not in created.json()["model"]


def test_create_model_masks_both_api_key_spellings(tmp_path, monkeypatch):
    monkeypatch.setattr("src.handler.model_settings.settings", type("S", (), {
        "db_path": tmp_path / "models.db", "deepseek_api_key": "",
    })())
    monkeypatch.setattr("src.handler.deps.settings", type("S", (), {"api_token": None, "db_path": tmp_path / "models.db"})())
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        response = client.post("/api/settings/models/tts", json={
            "name": "Private", "provider": "kokoro", "model": "kokoro-v1",
            "apiKey": "camel-secret", "api_key": "snake-secret",
        })

    assert response.status_code == 201
    body = response.json()
    assert body["model"]["hasApiKey"] is True
    assert "apiKey" not in body["model"] and "api_key" not in body["model"]
    assert body["config"]["hasApiKey"] is True
    assert all("apiKey" not in model and "api_key" not in model for model in body["config"]["models"])


def test_update_model_masks_both_api_key_spellings_on_get(tmp_path, monkeypatch):
    monkeypatch.setattr("src.handler.model_settings.settings", type("S", (), {
        "db_path": tmp_path / "models.db", "deepseek_api_key": "",
    })())
    monkeypatch.setattr("src.handler.deps.settings", type("S", (), {"api_token": None, "db_path": tmp_path / "models.db"})())
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        updated = client.put("/api/settings/models", json={"llm": {
            "model": "updated-model", "apiKey": "camel-secret", "api_key": "snake-secret",
            "models": [{
                "id": "llm-deepseek", "name": "Updated", "provider": "deepseek",
                "model": "updated-model", "isCurrent": True,
                "apiKey": "camel-secret", "api_key": "snake-secret",
            }],
        }})
        response = client.get("/api/settings/models")

    assert updated.status_code == 200
    updated_llm = updated.json()["llm"]
    assert updated_llm["hasApiKey"] is True
    assert "apiKey" not in updated_llm and "api_key" not in updated_llm
    assert all("apiKey" not in model and "api_key" not in model for model in updated_llm["models"])
    assert response.status_code == 200
    llm = response.json()["llm"]
    assert llm["hasApiKey"] is True
    assert "apiKey" not in llm and "api_key" not in llm
    assert llm["models"][0]["hasApiKey"] is True
    assert "apiKey" not in llm["models"][0] and "api_key" not in llm["models"][0]


def test_validate_does_not_save_and_rejects_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr("src.handler.model_settings.settings", type("S", (), {
        "db_path": tmp_path / "models.db", "deepseek_api_key": "",
    })())
    monkeypatch.setattr("src.handler.deps.settings", type("S", (), {"api_token": None, "db_path": tmp_path / "models.db"})())
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        response = client.post("/api/settings/models/whisper/validate", json={"provider": "replicate"})
        assert response.status_code == 200
        assert response.json()["ok"] is False
        assert "Replicate" in response.json()["message"]
        saved = client.get("/api/settings/models").json()["whisper"]
        assert saved["provider"] == "local"
