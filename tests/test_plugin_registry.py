import pytest

from src.plugin_system.contracts import PluginConfig
from src.plugin_system.manifest import PluginManifest
from src.plugin_system.registry import PluginRegistry
from tests.plugin_fakes import FakeFactory


def _manifest(plugin_id="builtin.translate", stage="translate", version="1", capabilities=frozenset()):
    operation = "subtitle" if stage == "render" else stage
    operations = frozenset({"subtitle", "dub_mux"}) if stage == "render" else frozenset({operation})
    if stage == "download":
        operations = frozenset({"download"})
    return PluginManifest(
        plugin_id=plugin_id,
        stage=stage,
        implementation_version=version,
        operations=operations,
        capabilities=capabilities,
    )


def test_register_is_lazy_and_resolve_requires_exact_version():
    registry = PluginRegistry()
    factory = FakeFactory("translate")
    manifest = _manifest()

    registry.register(manifest, factory)

    assert len(registry) == 1
    assert factory.created == []
    assert registry.resolve("builtin.translate", "1").factory is factory
    assert registry.get_manifest("builtin.translate") == manifest
    with pytest.raises(ValueError, match="version"):
        registry.resolve("builtin.translate", "2")
    with pytest.raises(KeyError):
        registry.resolve("missing", "1")


def test_register_rejects_duplicate_id_and_factory_stage_mismatch():
    registry = PluginRegistry()
    manifest = _manifest()
    registry.register(manifest, FakeFactory("translate"))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(manifest, FakeFactory("translate"))
    with pytest.raises(ValueError, match="does not match"):
        registry.register(_manifest(plugin_id="wrong.stage"), FakeFactory("download"))
