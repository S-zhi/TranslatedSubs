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
        assert response.json()["llm"]["hasApiKey"] is True
        assert "apiKey" not in response.json()["llm"]

        updated = client.put("/api/settings/models", json={"llm": {"model": "deepseek-reasoner", "apiKey": ""}})
        assert updated.status_code == 200
        assert updated.json()["llm"]["model"] == "deepseek-reasoner"
        assert updated.json()["llm"]["hasApiKey"] is True

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


def test_whisper_status_is_live_readonly_and_save_requires_ready_model(tmp_path, monkeypatch):
    from src.handler import model_settings as routes

    monkeypatch.setattr(routes, "settings", type("S", (), {
        "db_path": tmp_path / "models.db", "deepseek_api_key": "",
    })())
    monkeypatch.setattr("src.handler.deps.settings", type("S", (), {
        "api_token": None, "db_path": tmp_path / "models.db",
    })())
    inventory = [{"name": "small", "status": "READY"}]
    monkeypatch.setattr(routes.model_manager, "list_models", lambda: inventory)
    monkeypatch.setattr(routes.model_manager, "is_ready", lambda name: name == "small" and inventory[0]["status"] == "READY")
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        readonly = client.put("/api/settings/models", json={"whisper": {
            "status": "READY", "runtimeManaged": True, "_runtimeManaged": True,
            "hasApiKey": True,
        }})
        assert readonly.status_code == 200
        assert _store().get("whisper")["_runtimeManaged"] is False
        card_ids_only = client.put("/api/settings/models", json={"whisper": {
            "models": [{"id": "legacy-card", "status": "READY", "runtimeManaged": True}],
        }})
        assert card_ids_only.status_code == 200
        assert _store().get("whisper")["_runtimeManaged"] is False
        before = client.get("/api/settings/models").json()["whisper"]
        assert before["status"] == "READY"
        assert before["runtimeManaged"] is False
        assert before["models"] == []

        payload = {"whisper": {
            "provider": "local", "model": "local:small", "enabled": True,
            "models": [{"id": "whisper-small", "name": "Small", "provider": "local",
                        "model": "local:small", "enabled": True, "isCurrent": True,
                        "status": "NOT_INSTALLED", "runtimeManaged": False,
                        "_runtimeManaged": False, "hasApiKey": False}],
            "status": "NOT_INSTALLED", "runtimeManaged": False, "_runtimeManaged": False,
        }}
        saved = client.put("/api/settings/models", json=payload)
        assert saved.status_code == 200
        assert saved.json()["whisper"]["status"] == "READY"
        assert saved.json()["whisper"]["runtimeManaged"] is True
        assert saved.json()["whisper"]["models"][0]["status"] == "READY"

        inventory[0]["status"] = "ERROR"
        assert client.get("/api/settings/models").json()["whisper"]["status"] == "ERROR"

        prior = routes._store().get_all()
        failed = client.put("/api/settings/models", json={
            "llm": {"model": "partial"}, "tts": {"voice": "af_maple"}, "whisper": {
                "provider": "local", "model": "local:small", "enabled": True,
        }})
        assert failed.status_code == 409
        assert failed.json()["detail"]["code"] == "MODEL_NOT_READY"
        assert routes._store().get_all() == prior

        readonly = client.put("/api/settings/models", json={"whisper": {
            "status": "READY", "runtimeManaged": False, "_runtimeManaged": False,
        }})
        assert readonly.status_code == 200
        assert routes._store().get("whisper")["_runtimeManaged"] is True


def test_whisper_post_candidate_inherits_current_fields_and_validates_effective_card(tmp_path, monkeypatch):
    from src.handler import model_settings as routes

    monkeypatch.setattr(routes, "settings", type("S", (), {
        "db_path": tmp_path / "models.db", "deepseek_api_key": "",
    })())
    monkeypatch.setattr("src.handler.deps.settings", type("S", (), {
        "api_token": None, "db_path": tmp_path / "models.db",
    })())
    monkeypatch.setattr(routes.model_manager, "is_ready", lambda _name: False)
    app.dependency_overrides.clear()
    store = _store()
    store.update({"whisper": {
        "provider": "local", "model": "local:ready",
        "models": [{"id": "old", "provider": "local", "model": "local:ready", "isCurrent": True}],
    }})
    with TestClient(app) as client:
        before = store.get("whisper")
        # The omitted provider/model inherit from settings, so the newly
        # activated card must be checked against that actual effective model.
        response = client.post("/api/settings/models/whisper", json={"name": "new card"})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "MODEL_NOT_READY"
        assert store.get("whisper") == before
        unsupported = client.post("/api/settings/models/whisper", json={
            "provider": "replicate", "model": "cloud-model", "enabled": True,
        })
        assert unsupported.status_code == 422
        assert store.get("whisper") == before

        no_current = client.put("/api/settings/models", json={"whisper": {
            "enabled": True, "provider": "local", "model": "local:ready",
            "models": [{"id": "old", "provider": "local", "model": "local:ready", "isCurrent": False}],
        }})
        assert no_current.status_code == 422
        assert store.get("whisper") == before


def test_invalid_kind_type_rejects_whole_put(tmp_path, monkeypatch):
    from src.handler import model_settings as routes

    monkeypatch.setattr(routes, "settings", type("S", (), {
        "db_path": tmp_path / "models.db", "deepseek_api_key": "",
    })())
    monkeypatch.setattr("src.handler.deps.settings", type("S", (), {
        "api_token": None, "db_path": tmp_path / "models.db",
    })())
    app.dependency_overrides.clear()
    store = _store()
    before = store.get_all()
    with TestClient(app) as client:
        response = client.put("/api/settings/models", json={
            "llm": {"model": "partial-change"}, "whisper": "invalid",
        })
    assert response.status_code == 422
    assert store.get_all() == before


def test_model_settings_write_endpoints_require_token(tmp_path, monkeypatch):
    from src.handler import deps, model_settings as routes

    configured = type("S", (), {"db_path": tmp_path / "models.db", "deepseek_api_key": ""})()
    monkeypatch.setattr(routes, "settings", configured)
    monkeypatch.setattr(deps, "settings", type("S", (), {
        "api_token": "settings-token", "db_path": tmp_path / "models.db",
        "deepseek_api_key": "", "deepseek_base_url": "https://api.deepseek.com/v1",
        "deepseek_model": "deepseek-chat",
    })())
    app.dependency_overrides.clear()
    store = routes._store()
    before = store.get_all()
    with TestClient(app) as client:
        put = client.put("/api/settings/models", json={"llm": {"model": "changed"}})
        post = client.post("/api/settings/models/tts", json={"provider": "local"})
        validate = client.post("/api/settings/models/whisper/validate", json={"provider": "local", "model": "tiny"})
        assert put.status_code == 401
        assert post.status_code == 401
        assert validate.status_code == 401
        assert store.get_all() == before
        valid = client.post("/api/settings/models/whisper/validate", json={"provider": "replicate"}, headers={"X-API-Token": "settings-token"})
        assert valid.status_code == 200 and valid.json()["ok"] is False
        assert store.get_all() == before


def test_whisper_uses_active_card_for_validation_and_preserves_disabled_master(tmp_path, monkeypatch):
    from src.handler import model_settings as routes

    monkeypatch.setattr(routes, "settings", type("S", (), {
        "db_path": tmp_path / "models.db", "deepseek_api_key": "",
    })())
    monkeypatch.setattr("src.handler.deps.settings", type("S", (), {
        "api_token": None, "db_path": tmp_path / "models.db",
    })())
    monkeypatch.setattr(routes.model_manager, "is_ready", lambda name: name == "A")
    monkeypatch.setattr(routes.model_manager, "list_models", lambda: [
        {"name": "A", "status": "READY"}, {"name": "B", "status": "NOT_INSTALLED"},
    ])
    app.dependency_overrides.clear()
    store = _store()
    before = store.get_all()
    with TestClient(app) as client:
        missing_active = client.put("/api/settings/models", json={"whisper": {
            "enabled": True, "provider": "local", "model": "local:A",
            "models": [{"id": "a", "provider": "local", "model": "local:A"},
                       {"id": "b", "provider": "local", "model": "local:B", "isCurrent": True}],
        }})
        assert missing_active.status_code == 409
        assert store.get_all() == before

        unsupported_active = client.put("/api/settings/models", json={"whisper": {
            "enabled": True, "provider": "local", "model": "local:A",
            "models": [{"id": "a", "provider": "local", "model": "local:A"},
                       {"id": "b", "provider": "replicate", "model": "cloud", "isCurrent": True}],
        }})
        assert unsupported_active.status_code == 422
        assert store.get_all() == before

        disabled = client.put("/api/settings/models", json={"whisper": {
            "enabled": False, "provider": "local", "model": "local:B",
            "models": [{"id": "b", "provider": "local", "model": "local:B",
                        "enabled": True, "isCurrent": True}],
        }})
        assert disabled.status_code == 200
        assert disabled.json()["whisper"]["enabled"] is False
        assert store.get("whisper")["models"][0]["enabled"] is False

        selected_earlier = client.put("/api/settings/models", json={"whisper": {
            "enabled": True, "provider": "local", "model": "local:A",
            "models": [{"id": "a", "provider": "local", "model": "local:A", "isCurrent": True},
                       {"id": "b", "provider": "local", "model": "local:B", "isCurrent": False}],
        }})
        assert selected_earlier.status_code == 200
        cards = store.get("whisper")["models"]
        assert [card["id"] for card in cards if card.get("isCurrent")] == ["a"]


def test_enabled_whisper_rejects_empty_cards_atomically_but_disabled_keeps_cards(tmp_path, monkeypatch):
    from src.handler import model_settings as routes

    monkeypatch.setattr(routes, "settings", type("S", (), {
        "db_path": tmp_path / "models.db", "deepseek_api_key": "",
    })())
    monkeypatch.setattr("src.handler.deps.settings", type("S", (), {
        "api_token": None, "db_path": tmp_path / "models.db",
    })())
    app.dependency_overrides.clear()
    store = _store()
    store.update({"whisper": {
        "enabled": True, "provider": "local", "model": "local:kept",
        "models": [{"id": "kept", "provider": "local", "model": "local:kept", "enabled": True, "isCurrent": True}],
    }})
    before = store.get_all()
    with TestClient(app) as client:
        rejected = client.put("/api/settings/models", json={"whisper": {
            "enabled": True, "provider": "local", "model": "local:kept", "models": [],
        }})
        assert rejected.status_code == 422
        assert store.get_all() == before

        disabled = client.put("/api/settings/models", json={"whisper": {
            "enabled": False, "provider": "local", "model": "local:kept", "models": [],
        }})
        assert disabled.status_code == 200
        saved = store.get("whisper")
        assert saved["enabled"] is False
        assert len(saved["models"]) == 1
        assert saved["models"][0]["isCurrent"] is True
