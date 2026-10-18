from __future__ import annotations

import copy

import pytest

from src.config.config_repository import MemoryConfigProvider, _validate_document
from src.config.plugin_schema import default_plugin_section, validate_plugin_section
from src.config.redis_store import RevisionConflict
from src.config.schema import default_document
from src.plugin_system.errors import PluginError
from src.plugins import get_plugin_manager
from src.store.plugin_instance_store import PluginInstanceConflict, PluginInstanceStore


def _payload(**overrides):
    return {
        "name": "下载备用实例",
        "stage": "download",
        "plugin_id": "builtin.ytdlp",
        "implementation_version": "1",
        "config": {"retries": 2},
        **overrides,
    }


def _create(store, **overrides):
    return store.create(_payload(**overrides), expected_revision=store._read().revision)


def test_default_section_and_legacy_reads_are_detached_without_writes():
    document = default_document()
    del document["plugins"]
    _validate_document(document)
    provider = MemoryConfigProvider(document)
    store = PluginInstanceStore(provider=provider)
    before = provider.read()
    assert store.defaults() == default_plugin_section()["defaults"]
    assert len(store.list()) == 7
    first = store.list()[0]
    first["config"]["retries"] = 99
    first["name"] = "changed"
    defaults = store.defaults()
    defaults.clear()
    assert store.get("default_download")["config"] == {}
    assert len(store.defaults()) == 7
    assert provider.read() == before
    assert "plugins" not in provider.read().values
    assert store.get("missing") is None
    item, saved = _create(store)
    assert item["id"] in saved.values["plugins"]["byId"]
    assert saved.values["meta"]["plugins_configured"] is True
    assert len(store.list()) == 8


def test_create_update_delete_use_global_revision_and_detached_records():
    provider = MemoryConfigProvider()
    store = PluginInstanceStore(provider=provider)
    item, saved = _create(store)
    instance_id = item["id"]
    item["config"]["retries"] = 80
    assert store.get(instance_id)["config"] == {"retries": 2}
    provider.mutate(lambda document: document["business"].update({"debug": True}))
    with pytest.raises(RevisionConflict):
        store.update(instance_id, {"name": "stale"}, expected_revision=saved.revision)
    current = provider.read()
    updated, saved = store.update(
        instance_id, {"name": "new", "config": {"socket_timeout": 12}},
        expected_revision=current.revision,
    )
    assert saved.revision == current.revision + 1
    assert updated["config"] == {"socket_timeout": 12}
    assert saved.values["business"]["debug"] is True
    deleted = store.delete(instance_id, expected_revision=saved.revision)
    assert deleted.revision == saved.revision + 1
    assert store.get(instance_id) is None


def test_resolve_is_snapshot_bound_and_never_falls_back():
    provider = MemoryConfigProvider()
    store = PluginInstanceStore(provider=provider)
    item, saved = _create(store)
    frozen = PluginInstanceStore(snapshot=saved)
    resolved = frozen.resolve({"download": item["id"]})
    assert len(resolved) == 7
    assert resolved["download"]["config"] == {"retries": 2}
    store.update(item["id"], {"config": {"retries": 8}}, expected_revision=saved.revision)
    assert frozen.resolve({"download": item["id"]})["download"]["config"] == {"retries": 2}
    resolved["download"]["config"]["retries"] = 0
    assert frozen.get(item["id"])["config"] == {"retries": 2}
    store.update(item["id"], {"enabled": False}, expected_revision=store._read().revision)
    for selection in (
        {"download": item["id"]}, {"download": "missing"},
        {"download": None}, {"download": "default_render"}, {"unknown": item["id"]},
    ):
        with pytest.raises(ValueError):
            store.resolve(selection)
    with pytest.raises(ValueError, match="read-only"):
        frozen.update(item["id"], {"name": "changed"}, expected_revision=saved.revision)
    with pytest.raises(ValueError, match="read-only"):
        frozen.delete(item["id"], expected_revision=saved.revision)


def test_defaults_are_complete_and_partial_updates_preserve_other_stages():
    store = PluginInstanceStore(provider=MemoryConfigProvider())
    item, saved = _create(store)
    original = store.defaults()
    changed = store.set_defaults({"download": item["id"]}, expected_revision=saved.revision)
    assert store.defaults() == {**original, "download": item["id"]}
    for action in (
        lambda: store.delete(item["id"], expected_revision=changed.revision),
        lambda: store.update(item["id"], {"enabled": False}, expected_revision=changed.revision),
    ):
        with pytest.raises(PluginInstanceConflict):
            action()
    for defaults in ({"download": "missing"}, {"download": "default_render"}, {"download": None}):
        with pytest.raises(ValueError):
            store.set_defaults(defaults, expected_revision=changed.revision)
    unchanged = store.set_defaults({}, expected_revision=changed.revision)
    assert unchanged.values["plugins"]["defaults"] == {**original, "download": item["id"]}
    assert len(store.defaults()) == 7


@pytest.mark.parametrize("payload", [
    {"stage": "unknown"}, {"plugin_id": "unknown"},
    {"implementation_version": "99"}, {"config_schema_version": True},
    {"enabled": "false"}, {"id": "a/b"}, {"name": ""},
    {"config": {"retries": True}}, {"config": {"retries": -1}},
    {"config": {"retries": "2"}}, {"config": {"unknown": 1}},
    {"config": {"api_key": "private"}}, {"config": {"cookies_ref": "private"}},
    {"config": {"nested": {"x": 1}}}, {"config": {"socket_timeout": float("inf")}},
    {"config": {"proxy": "http://user:private@example.com"}},
    {"config": {"proxy": "https://example.com?api%5Fkey=private"}},
    {"config": {"proxy": "https://example.com?x-access-token=private"}},
    {"config": {"proxy": "https://example.com?X-Amz-Signature=private"}},
    {"config": {"proxy": "https://example.com#private"}},
    {"config": {"proxy": "file:///private"}},
    {"unexpected": "private"},
])
def test_invalid_creation_is_atomic_and_does_not_echo_secret_values(payload):
    provider = MemoryConfigProvider()
    store = PluginInstanceStore(provider=provider)
    before = provider.read()
    with pytest.raises((ValueError, PluginError)) as failure:
        store.create(_payload(**payload), expected_revision=before.revision)
    assert "private" not in str(failure.value)
    assert provider.read() == before


def test_registered_stage_and_factory_editable_schema_are_authoritative():
    store = PluginInstanceStore(provider=MemoryConfigProvider())
    with pytest.raises(ValueError, match="stage"):
        _create(store, plugin_id="builtin.whisper")
    with pytest.raises(PluginError):
        _create(store, config={"quality": "best"})
    item, _ = _create(store, config={"proxy": "socks5://127.0.0.1:1080"})
    assert item["config"]["proxy"] == "socks5://127.0.0.1:1080"


def test_bindings_validate_kind_existence_exclusivity_and_factory_support():
    document = default_document()
    document["models"] = {
        "llm": {"models": [{"id": "llm-card", "apiKey": "private-model-secret"}]},
        "whisper": {"models": [{"id": "whisper-card"}]},
        "tts": {"models": [{"id": "tts-card"}]},
    }
    document["engines"]["byId"]["engine-1"] = {
        "id": "engine-1", "name": "Remote", "base_url": "https://example.com",
        "model": "test-model", "api_type": "openai_compatible", "api_key": "private-engine-secret",
    }
    store = PluginInstanceStore(provider=MemoryConfigProvider(document))
    for stage, plugin_id, kind in (
        ("translate", "builtin.translation_engine", "llm"),
        ("transcribe", "builtin.whisper", "whisper"),
        ("tts", "builtin.kokoro", "tts"),
    ):
        item, _ = _create(
            store, stage=stage, plugin_id=plugin_id, config={},
            model_ref={"kind": kind, "card_id": f"{kind}-card"},
        )
        assert item["model_ref"]["kind"] == kind
        assert "private" not in repr(item)
    item, _ = _create(
        store, stage="translate", plugin_id="builtin.translation_engine", config={},
        engine_ref={"id": "engine-1"},
    )
    assert item["engine_ref"] == {"id": "engine-1"}
    invalid = (
        {"model_ref": {"kind": "tts", "card_id": "tts-card"}},
        {"model_ref": {"kind": "llm", "card_id": "missing"}},
        {"model_ref": {"kind": "llm", "card_id": "tts-card"}},
        {"engine_ref": {"id": "missing"}},
        {"engine_ref": {"id": "engine-1"}, "model_ref": {"kind": "llm", "card_id": "llm-card"}},
    )
    for refs in invalid:
        with pytest.raises(ValueError):
            _create(store, stage="translate", plugin_id="builtin.translation_engine", config={}, **refs)
    with pytest.raises(ValueError):
        _create(store, engine_ref={"id": "engine-1"})


def test_aggregate_validation_rejects_dangling_bindings_and_default_corruption():
    document = default_document()
    document["models"]["tts"] = {"models": [{"id": "tts-card"}]}
    document["plugins"]["byId"]["default_tts"]["model_ref"] = {"kind": "tts", "card_id": "tts-card"}
    provider = MemoryConfigProvider(document)
    before = provider.read()
    with pytest.raises(ValueError, match="does not exist"):
        provider.mutate(lambda values: values["models"]["tts"].update({"models": []}))
    assert provider.read() == before
    for broken in (
        {"schema_version": True}, {"defaults": {}}, {"extra": True}, {"byId": []},
    ):
        section = {**copy.deepcopy(document["plugins"]), **broken}
        with pytest.raises(ValueError):
            validate_plugin_section(section, document=document)


def test_catalog_and_instance_operations_do_not_create_plugin_sessions(monkeypatch):
    manager = get_plugin_manager()
    def fail_session(*args, **kwargs):
        pytest.fail("management requests must not create a runtime session")
    for registered in manager._registry.entries():
        monkeypatch.setattr(registered.factory, "create_session", fail_session)
    store = PluginInstanceStore(provider=MemoryConfigProvider(), manager=manager)
    assert len(manager.catalog()) == 7
    item, saved = _create(store)
    assert store.resolve({"download": item["id"]})["download"]["id"] == item["id"]
    store.update(item["id"], {"config": {"retries": 4}}, expected_revision=saved.revision)
