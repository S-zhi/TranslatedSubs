import json

from src.config.config_repository import MemoryConfigProvider
from src.config.migrate_legacy import import_legacy_sources
from src.config.plugin_schema import default_plugin_section


def _env(tmp_path):
    path = tmp_path / "legacy.env"
    path.write_text("SUBTRANS_PIPELINE_PLUGINS='" + json.dumps({
        "download": {"plugin_id": "registered.custom", "implementation_version": "2"},
    }) + "'\n", encoding="utf-8")
    return path


def test_plugin_environment_migration_is_explicit_and_idempotent(tmp_path):
    provider = MemoryConfigProvider()
    source = _env(tmp_path)
    import_legacy_sources(env_file=source, provider=provider)
    snapshot = provider.read()
    section = snapshot.values["plugins"]
    assert section["byId"][section["defaults"]["download"]]["plugin_id"] == "registered.custom"
    assert "plugins_env" in snapshot.values["meta"]["legacy_sources_imported"]
    import_legacy_sources(env_file=source, provider=provider)
    assert provider.read().revision == snapshot.revision


def test_explicit_redis_plugin_choices_win_even_when_equal_to_builtins(tmp_path):
    provider = MemoryConfigProvider()
    provider.mutate(lambda document: document["meta"].update({"plugins_configured": True}))
    import_legacy_sources(env_file=_env(tmp_path), provider=provider)
    assert provider.read().values["plugins"] == default_plugin_section()
