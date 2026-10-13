from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config.config_repository import MemoryConfigProvider, set_config_provider
from src.config.redis_store import ConfigUnavailable
from src.handler.business_settings import router
from src.handler import deps


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_runtime_settings_read_patch_and_secret_mask(memory_config_provider):
    set_config_provider(MemoryConfigProvider())
    with _client() as client:
        initial = client.get("/api/settings/runtime")
        assert initial.status_code == 200
        revision = initial.json()["revision"]

        changed = client.patch(
            "/api/settings/runtime",
            headers={"If-Match": str(revision)},
            json={"pipeline_workers": 4, "api_token": "private-token"},
        )
        assert changed.status_code == 200
        assert changed.json()["values"]["pipeline_workers"] == 4
        assert "api_token" not in changed.json()["values"]
        assert changed.json()["hasSecrets"]["api_token"] is True


def test_runtime_settings_conflict_and_validation(memory_config_provider):
    provider = MemoryConfigProvider()
    set_config_provider(provider)
    with _client() as client:
        assert client.patch("/api/settings/runtime", headers={"If-Match": "0"}, json={"debug": True}).status_code == 409
        assert client.patch("/api/settings/runtime", json={"unknown": 1}).status_code == 422
        assert client.patch("/api/settings/runtime", json={"api_token": 2}).status_code == 422
        assert client.patch("/api/settings/runtime", json={"pipeline_workers": None}).status_code == 200
        assert provider.read().values["business"].get("pipeline_workers") is None
        for invalid in (
            {"pipeline_workers": 0},
            {"transcriber_timeout": -5},
            {"vocal_separation_model": "unsupported"},
        ):
            before = provider.read().revision
            assert client.patch("/api/settings/runtime", json=invalid).status_code == 422
            assert provider.read().revision == before


def test_runtime_settings_redis_down_returns_503(monkeypatch):
    class DownProvider:
        def read(self):
            raise ConfigUnavailable("redis down")
        def mutate(self, *_args, **_kwargs):
            raise ConfigUnavailable("redis down")
    set_config_provider(DownProvider())
    with _client() as client:
        assert client.get("/api/settings/runtime").status_code == 503
        assert client.patch("/api/settings/runtime", json={"debug": True}).status_code == 503
