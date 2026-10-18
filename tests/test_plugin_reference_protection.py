from __future__ import annotations

import copy

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.config.config_repository import MemoryConfigProvider, set_config_provider
from src.config.schema import default_document
from src.handler import deps, srt
from src.handler.app import create_app
from src.store.model_settings_store import ModelSettingsStore
from src.store.plugin_instance_store import PluginInstanceConflict, PluginInstanceStore
from src.store.task_store import TaskStore
from src.store.translation_engine_store import TranslationEngineStore


def _bound_document(*, enabled=True):
    document = default_document()
    document["models"]["whisper"] = {
        "provider": "local", "model": "local:current", "enabled": True,
        "models": [
            {"id": "current", "provider": "local", "model": "local:current", "isCurrent": True},
            {"id": "bound", "provider": "local", "model": "local:small", "isCurrent": False},
        ],
    }
    document["models"]["llm"] = {"models": [{"id": "llm", "model": "chat", "apiKey": "keep-secret"}]}
    document["engines"]["byId"]["engine"] = {
        "id": "engine", "name": "Remote", "api_type": "openai_compatible",
        "base_url": "https://example.com", "model": "chat", "api_key": "keep-secret",
    }
    instances = document["plugins"]["byId"]
    for instance_id, stage, ref in (
        ("bound-whisper", "transcribe", {"model_ref": {"kind": "whisper", "card_id": "bound"}}),
        ("bound-llm", "translate", {"model_ref": {"kind": "llm", "card_id": "llm"}}),
        ("bound-engine", "translate", {"engine_ref": {"id": "engine"}}),
    ):
        instances[instance_id] = {
            **copy.deepcopy(instances[f"default_{stage}"]), "id": instance_id,
            "enabled": enabled, **ref,
        }
    return document


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("kind,card_id", [("whisper", "bound"), ("llm", "llm")])
def test_model_update_callback_rejects_removing_a_bound_card_atomically(enabled, kind, card_id):
    provider = MemoryConfigProvider(_bound_document(enabled=enabled))
    store = ModelSettingsStore(provider=provider)
    before = provider.read()
    candidate = copy.deepcopy(before.values["models"][kind])
    candidate["models"] = [card for card in candidate["models"] if card["id"] != card_id]
    with pytest.raises(PluginInstanceConflict, match="引用"):
        store.update({kind: candidate}, prepared={kind: candidate}, expected_revision=before.revision)
    assert provider.read() == before
    instances = PluginInstanceStore(provider=provider)
    binding_id = "bound-whisper" if kind == "whisper" else "bound-llm"
    instances.update(binding_id, {"model_ref": None}, expected_revision=before.revision)
    store.update({kind: candidate}, prepared={kind: candidate}, expected_revision=provider.read().revision)
    assert all(card["id"] != card_id for card in store.get(kind)["models"])


@pytest.mark.parametrize("enabled", [True, False])
def test_engine_delete_callback_preserves_engine_secret_while_bound(enabled):
    provider = MemoryConfigProvider(_bound_document(enabled=enabled))
    store = TranslationEngineStore(provider=provider)
    before = provider.read()
    with pytest.raises(PluginInstanceConflict, match="引用"):
        store.delete("engine")
    assert provider.read() == before
    assert store.get("engine").api_key == "keep-secret"
    PluginInstanceStore(provider=provider).update(
        "bound-engine", {"engine_ref": None}, expected_revision=before.revision,
    )
    assert store.delete("engine") is True
    assert store.get("engine") is None


def test_delete_checks_latest_callback_document_after_concurrent_binding():
    document = _bound_document()
    binding = document["plugins"]["byId"].pop("bound-engine")
    class BindingProvider(MemoryConfigProvider):
        armed = False
        def mutate(self, callback, expected_revision=None):
            if self.armed:
                self.armed = False
                super().mutate(lambda values: values["plugins"]["byId"].update({"bound-engine": binding}))
            return super().mutate(callback, expected_revision=expected_revision)
    provider = BindingProvider(document)
    store = TranslationEngineStore(provider=provider)
    provider.armed = True
    with pytest.raises(PluginInstanceConflict):
        store.delete("engine")
    assert store.get("engine").api_key == "keep-secret"
    assert provider.read().values["plugins"]["byId"]["bound-engine"] == binding


def test_bound_engine_delete_uses_app_conflict_response(tmp_path):
    provider = MemoryConfigProvider(_bound_document())
    set_config_provider(provider)
    app = create_app()
    store = TranslationEngineStore(provider=provider)
    app.dependency_overrides[deps.get_translation_engine_store] = lambda: store
    client = TestClient(app)
    try:
        response = client.delete("/api/settings/translation-engines/engine")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "PLUGIN_INSTANCE_CONFLICT"
        assert "绑定" in response.json()["detail"]["message"]
        assert "keep-secret" not in response.text
        assert store.get("engine").api_key == "keep-secret"
    finally:
        client.close()


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("method,suffix", [("DELETE", ""), ("POST", "/check")])
def test_bound_non_current_whisper_assets_cannot_be_deleted_or_cleaned(
    tmp_path, monkeypatch, enabled, method, suffix,
):
    provider = MemoryConfigProvider(_bound_document(enabled=enabled))
    set_config_provider(provider)
    task_store = TaskStore(tmp_path / "tasks.db")
    app = create_app()
    app.dependency_overrides[deps.get_store] = lambda: task_store
    called = []
    def asset_operation(name):
        called.append(name)
        return {"name": name, "status": "NOT_INSTALLED"}
    monkeypatch.setattr(srt.model_manager, "delete", asset_operation)
    monkeypatch.setattr(srt.model_manager, "check", asset_operation)
    client = TestClient(app)
    try:
        response = client.request(method, f"/api/srt/local-models/small{suffix}")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "MODEL_IN_USE"
        assert called == []
        assert client.request(method, f"/api/srt/local-models/unbound{suffix}").status_code == 200
        assert called == ["unbound"]
    finally:
        client.close()


def test_whisper_binding_resolution_uses_one_snapshot_and_inherited_fields(tmp_path):
    document = _bound_document()
    document["models"]["whisper"]["provider"] = "local"
    document["models"]["whisper"]["models"][1].pop("provider")
    class CountingProvider(MemoryConfigProvider):
        reads = 0
        def read(self):
            self.reads += 1
            return super().read()
    provider = CountingProvider(document)
    set_config_provider(provider)
    store = TaskStore(tmp_path / "tasks.db")
    with pytest.raises(HTTPException) as failure:
        srt._ensure_model_not_in_use("small", store)
    assert failure.value.status_code == 409
    assert provider.reads == 1
