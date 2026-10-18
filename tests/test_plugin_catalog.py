import pytest

from src.plugin_system import PluginError, PluginManifest
from src.plugin_system.manager import PluginManager
from src.plugin_system.registry import PluginRegistry
from src.plugins import get_plugin_manager


class LightweightFactory:
    stage = "translate"

    def __init__(self):
        self.checked = []

    def create_session(self, config, context):
        raise AssertionError("catalog/preflight must not create a session")

    def preflight(self, config, **options):
        self.checked.append((dict(config), options))


def _manager(factory):
    registry = PluginRegistry()
    registry.register(PluginManifest("test.translator", "translate", operations=frozenset({"translate"})), factory)
    return PluginManager(registry)


def test_catalog_and_preflight_do_not_create_sessions():
    factory = LightweightFactory()
    manager = _manager(factory)
    assert manager.catalog()[0]["plugin_id"] == "test.translator"
    manager.preflight("test.translator", "1", operation="translate", purpose="translate",
                      config={"batch_size": 4}, source_lang="en", target_lang="ja")
    assert factory.checked[0][0] == {"batch_size": 4}
    assert factory.checked[0][1]["target_lang"] == "ja"


def test_preflight_rejects_wrong_stage_missing_identity_and_legacy_hook():
    factory = LightweightFactory()
    manager = _manager(factory)
    with pytest.raises(PluginError, match="operation"):
        manager.preflight("test.translator", "1", operation="tts", purpose="tts", config={})
    with pytest.raises(PluginError) as failure:
        manager.preflight("missing", "1", operation="translate", purpose="translate", config={})
    assert failure.value.code == "plugin_unavailable"
    factory.preflight = None
    with pytest.raises(PluginError, match="preflight"):
        manager.preflight("test.translator", "1", operation="translate", purpose="translate", config={})


def test_builtin_catalog_declares_only_non_sensitive_editable_fields():
    catalog = get_plugin_manager().catalog()
    assert len(catalog) == 7
    fields = {item["stage"]: item["config_schema"] for item in catalog}
    assert "api_key" not in fields["translate"]
    assert "engine_id" not in fields["translate"]
    assert "timeout_seconds" in fields["translate"]
    assert "service_url" in fields["transcribe"]


@pytest.mark.parametrize("config", [{"api_key": "secret"}, {"socket_timeout": True},
                                    {"proxy": "https://user:password@example.test"},
                                    {"socket_timeout": -2}])
def test_invalid_instance_overrides_are_rejected(config):
    with pytest.raises(PluginError):
        get_plugin_manager().validate_config("builtin.ytdlp", "1", config)
