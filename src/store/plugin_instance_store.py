"""Plugin instances in the revisioned, aggregate configuration document."""
from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from typing import Any

from src.config.config_repository import ConfigSnapshot, get_config_provider
from src.config.plugin_schema import (
    PLUGIN_SCHEMA_VERSION,
    PLUGIN_STAGES,
    default_plugin_section,
    validate_plugin_section,
)
from src.config.redis_store import RevisionConflict


class PluginInstanceConflict(RevisionConflict):
    """An instance cannot be removed or disabled while it is in use."""


class PluginInstanceStore:
    """Read instances from one snapshot and mutate them using global CAS."""

    def __init__(self, provider=None, snapshot: ConfigSnapshot | None = None, manager=None):
        self._provider = provider
        self._snapshot = snapshot
        self._manager = manager

    def _read(self) -> ConfigSnapshot:
        return self._snapshot or (self._provider or get_config_provider()).read()

    @staticmethod
    def _section(snapshot: ConfigSnapshot) -> dict[str, Any]:
        section = snapshot.values.get("plugins", default_plugin_section())
        validate_plugin_section(section, document=snapshot.values)
        return copy.deepcopy(section)

    def list(self) -> list[dict[str, Any]]:
        return list(self._section(self._read())["byId"].values())

    def get(self, instance_id: str) -> dict[str, Any] | None:
        return self._section(self._read())["byId"].get(instance_id)

    def defaults(self) -> dict[str, str]:
        return self._section(self._read())["defaults"]

    def _plugin_manager(self):
        if self._manager is None:
            from src.plugins import get_plugin_manager

            return get_plugin_manager()
        return self._manager

    def _validate_registered(self, record: dict[str, Any]) -> None:
        manager = self._plugin_manager()
        entry = next((item for item in manager.catalog() if (
            item["plugin_id"] == record["plugin_id"]
            and item["implementation_version"] == record["implementation_version"]
        )), None)
        if entry is None:
            raise ValueError("plugin implementation is not registered")
        if entry["stage"] != record["stage"]:
            raise ValueError("plugin implementation does not match the instance stage")
        if entry.get("config_schema_version", 1) != record["config_schema_version"]:
            raise ValueError("plugin configuration schema version does not match")
        if (record.get("model_ref") or {}).get("kind") == "whisper":
            if set(record["config"]) & {"backend", "service_url"}:
                raise ValueError("绑定 Whisper 模型卡后不能另行覆盖转写后端或服务地址")
        bindings = entry.get("bindings", [])
        if record["model_ref"] is not None and record["model_ref"]["kind"] not in bindings:
            raise ValueError("plugin does not support the selected model binding")
        if record["engine_ref"] is not None and "engine" not in bindings:
            raise ValueError("plugin does not support an engine binding")
        record["config"] = manager.validate_config(
            record["plugin_id"], record["implementation_version"], record["config"],
            purpose=None, partial=True,
        )

    def resolve(
        self, selection: Mapping[str, str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Resolve all stage choices without falling back from an invalid choice."""
        if selection is not None and not isinstance(selection, Mapping):
            raise ValueError("plugin selection must be a stage-to-instance mapping")
        chosen = dict(selection or {})
        if set(chosen) - PLUGIN_STAGES:
            raise ValueError("plugin selection contains an unsupported stage")
        snapshot = self._read()
        section = self._section(snapshot)
        result = {}
        for stage, default_id in section["defaults"].items():
            instance_id = chosen.get(stage, default_id)
            if not isinstance(instance_id, str) or not instance_id:
                raise ValueError("plugin selection must contain instance identifiers")
            record = section["byId"].get(instance_id)
            if record is None:
                raise ValueError("selected plugin instance does not exist")
            if record["stage"] != stage or not record["enabled"]:
                raise ValueError("selected plugin instance is disabled or has a different stage")
            self._validate_registered(record)
            result[stage] = record
        validate_plugin_section(section, document=snapshot.values)
        return result

    def _mutate(self, callback, expected_revision: int) -> ConfigSnapshot:
        if self._snapshot is not None:
            raise ValueError("a snapshot-backed plugin instance store is read-only")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        def apply(document: dict[str, Any]) -> None:
            callback(document)
            document.setdefault("meta", {})["plugins_configured"] = True

        return (self._provider or get_config_provider()).mutate(
            apply, expected_revision=expected_revision,
        )

    @staticmethod
    def _editable_section(document: dict[str, Any]) -> dict[str, Any]:
        if "plugins" not in document:
            document["plugins"] = default_plugin_section()
        return document["plugins"]

    def create(
        self, payload: Mapping[str, Any], *, expected_revision: int,
    ) -> tuple[dict[str, Any], ConfigSnapshot]:
        if not isinstance(payload, Mapping):
            raise ValueError("plugin instance must be an object")
        record = copy.deepcopy(dict(payload))
        record.setdefault("id", "plugin-" + uuid.uuid4().hex)
        instance_id = record["id"]
        if not isinstance(instance_id, str):
            raise ValueError("plugin instance id must be a string")
        record.setdefault("config_schema_version", PLUGIN_SCHEMA_VERSION)
        record.setdefault("enabled", True)
        record.setdefault("config", {})
        record.setdefault("model_ref", None)
        record.setdefault("engine_ref", None)

        def apply(document: dict[str, Any]) -> None:
            section = self._editable_section(document)
            if instance_id in section["byId"]:
                raise PluginInstanceConflict("插件实例 ID 已存在")
            section["byId"][instance_id] = record
            validate_plugin_section(section, document=document)
            self._validate_registered(record)
            validate_plugin_section(section, document=document)

        snapshot = self._mutate(apply, expected_revision)
        return copy.deepcopy(snapshot.values["plugins"]["byId"][instance_id]), snapshot

    def update(
        self, instance_id: str, payload: Mapping[str, Any], *, expected_revision: int,
    ) -> tuple[dict[str, Any], ConfigSnapshot]:
        """Merge instance fields; an explicit config replaces the override map."""
        if not isinstance(payload, Mapping):
            raise ValueError("plugin instance must be an object")
        patch = copy.deepcopy(dict(payload))

        def apply(document: dict[str, Any]) -> None:
            section = self._editable_section(document)
            current = section["byId"].get(instance_id)
            if current is None:
                raise KeyError(instance_id)
            if "id" in patch and patch["id"] != instance_id:
                raise ValueError("plugin instance id cannot be changed")
            if "stage" in patch and patch["stage"] != current["stage"]:
                raise ValueError("plugin instance stage cannot be changed")
            if patch.get("enabled") is False and instance_id in section["defaults"].values():
                raise PluginInstanceConflict("默认插件实例不能停用，请先切换此阶段的默认实例")
            record = {**current, **patch}
            section["byId"][instance_id] = record
            validate_plugin_section(section, document=document)
            self._validate_registered(record)
            validate_plugin_section(section, document=document)

        snapshot = self._mutate(apply, expected_revision)
        return copy.deepcopy(snapshot.values["plugins"]["byId"][instance_id]), snapshot

    def delete(self, instance_id: str, *, expected_revision: int) -> ConfigSnapshot:
        def apply(document: dict[str, Any]) -> None:
            section = self._editable_section(document)
            if instance_id not in section["byId"]:
                raise KeyError(instance_id)
            if instance_id in section["defaults"].values():
                raise PluginInstanceConflict("默认插件实例不能删除，请先切换此阶段的默认实例")
            del section["byId"][instance_id]
            validate_plugin_section(section, document=document)

        return self._mutate(apply, expected_revision)

    def set_defaults(
        self, defaults: Mapping[str, str], *, expected_revision: int,
    ) -> ConfigSnapshot:
        """Atomically merge selected stage defaults, retaining all other stages."""
        if not isinstance(defaults, Mapping) or set(defaults) - PLUGIN_STAGES:
            raise ValueError("plugin defaults must be a mapping of supported stages")
        patch = dict(defaults)

        def apply(document: dict[str, Any]) -> None:
            section = self._editable_section(document)
            section["defaults"].update(patch)
            validate_plugin_section(section, document=document)
            for instance_id in patch.values():
                self._validate_registered(section["byId"][instance_id])
            validate_plugin_section(section, document=document)

        return self._mutate(apply, expected_revision)
