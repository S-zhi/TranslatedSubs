"""Pipeline plugin host integration."""

from __future__ import annotations

from functools import lru_cache

from src.plugin_system.manager import PluginManager
from src.plugin_system.registry import PluginRegistry
from src.plugins.builtin import factories


@lru_cache(maxsize=1)
def get_plugin_manager() -> PluginManager:
    """Build the production manager with lightweight built-in factories."""
    registry = PluginRegistry()
    for factory in factories():
        registry.register(factory.manifest, factory)
    return PluginManager(registry)


__all__ = ["get_plugin_manager"]
