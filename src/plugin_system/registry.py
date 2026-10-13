"""Lightweight plugin factory registry."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from src.plugin_system.contracts import PluginFactory
from src.plugin_system.manifest import PluginManifest, validate_registration


@dataclass(frozen=True)
class RegisteredPlugin:
    manifest: PluginManifest
    factory: PluginFactory


class PluginRegistry:
    """Stores factories without constructing plugin sessions."""

    def __init__(self) -> None:
        self._plugins: dict[str, RegisteredPlugin] = {}
        self._lock = RLock()

    def register(self, manifest: PluginManifest, factory: PluginFactory) -> None:
        """Register one implementation, rejecting duplicate IDs and bad pairs."""
        with self._lock:
            validate_registration(manifest, factory, set(self._plugins))
            self._plugins[manifest.plugin_id] = RegisteredPlugin(manifest, factory)

    def resolve(
        self, plugin_id: str, implementation_version: str
    ) -> RegisteredPlugin:
        """Resolve an exact implementation version."""
        with self._lock:
            registered = self._plugins.get(plugin_id)
        if registered is None:
            raise KeyError(plugin_id)
        if registered.manifest.implementation_version != implementation_version:
            raise ValueError(
                "plugin implementation version does not match the requested version"
            )
        return registered

    def get_manifest(self, plugin_id: str) -> PluginManifest | None:
        with self._lock:
            registered = self._plugins.get(plugin_id)
            return registered.manifest if registered is not None else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._plugins)
