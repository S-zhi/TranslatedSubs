"""Redis-backed storage for LLM, Whisper, and TTS settings cards.

The legacy ``db_path`` argument remains accepted for callers that construct
stores directly. Settings are read and written only through the shared Redis
configuration provider; SQLite is not a runtime fallback.
"""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from typing import Any

from src.config.config_repository import ConfigSnapshot, get_config_provider
from src.config.model_schema import validate_models

KINDS = ("llm", "whisper", "tts")

DEFAULTS: dict[str, dict[str, Any]] = {
    "llm": {
        "enabled": True, "provider": "deepseek", "model": "deepseek-chat",
        "baseUrl": "https://api.deepseek.com/v1", "temperature": 0.3,
        "maxTokens": 4096, "timeout": 120, "concurrency": 2, "models": [],
    },
    "whisper": {
        "enabled": True, "provider": "local", "model": "local:small",
        "language": "auto", "device": "cpu", "threads": 4,
        "wordTimestamps": True, "models": [],
    },
    "tts": {
        "enabled": False, "provider": "kokoro", "model": "kokoro-v1",
        "baseUrl": "", "voice": "auto", "speed": 1, "format": "wav",
        "models": [],
    },
}

_READONLY_FIELDS = {"status", "runtimeManaged", "_runtimeManaged", "hasApiKey"}


def _strip_public_readonly(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_public_readonly(item) for key, item in value.items() if key not in _READONLY_FIELDS}
    if isinstance(value, list):
        return [_strip_public_readonly(item) for item in value]
    return value


def _default_for(kind: str) -> dict[str, Any]:
    value = copy.deepcopy(DEFAULTS[kind])
    value["_runtimeManaged"] = False
    if kind == "llm":
        value["apiKey"] = ""
        value["models"] = [{
            "id": "llm-deepseek", "name": "DeepSeek", "provider": "deepseek",
            "model": "deepseek-chat", "baseUrl": "https://api.deepseek.com/v1",
            "enabled": True, "isCurrent": True, "apiKey": "",
        }]
    return value


class ModelSettingsStore:
    """Read and mutate model settings in the shared config aggregate."""

    def __init__(self, db_path: Path | str | None = None, *, provider=None,
                 snapshot: ConfigSnapshot | None = None):
        # Retained only for source compatibility and explicit task-store
        # association; model configuration itself is always Redis-backed.
        self.db_path = Path(db_path) if db_path is not None else None
        self._provider = provider
        self._snapshot = snapshot

    def _read(self) -> ConfigSnapshot:
        return self._snapshot or (self._provider or get_config_provider()).read()

    def get_all(self) -> dict[str, dict[str, Any]]:
        models = self._read().values.get("models", {})
        return {
            kind: copy.deepcopy(models.get(kind) or _default_for(kind))
            for kind in KINDS
        }

    def get(self, kind: str) -> dict[str, Any] | None:
        if kind not in KINDS:
            return None
        return self.get_all()[kind]

    def prepare(self, values: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Prepare a complete candidate set without persisting any changes."""
        return self.prepare_versioned(values)[0]

    def prepare_versioned(
        self, values: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], int]:
        """Prepare candidates and return the config revision they were based on."""
        snapshot = self._read()
        current = {
            kind: copy.deepcopy(snapshot.values.get("models", {}).get(kind) or _default_for(kind))
            for kind in KINDS
        }
        return self._prepare_values(values, current), snapshot.revision

    def _prepare_values(
        self, values: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        for kind, raw_patch in values.items():
            if kind not in KINDS:
                raise KeyError(kind)
            patch = _strip_public_readonly(raw_patch)
            if not patch:
                continue
            merged = dict(current[kind])
            merged["_runtimeManaged"] = True
            old_models = list(merged.get("models") or [])
            models_updated = isinstance(patch.get("models"), list)
            for key, value in patch.items():
                if key == "apiKey":
                    if value:
                        merged[key] = str(value)
                elif key == "models" and isinstance(value, list):
                    merged["models"] = self._merge_models(old_models, value)
                else:
                    merged[key] = value
            if models_updated:
                current_candidates = [item for item in merged.get("models", []) if item.get("isCurrent")]
                if len(current_candidates) > 1:
                    selected_id = current_candidates[-1].get("id")
                    for item in merged["models"]:
                        item["isCurrent"] = item.get("id") == selected_id
            active = next((item for item in merged.get("models", []) if item.get("isCurrent")), None)
            if not models_updated and old_models:
                merged["models"] = old_models
                active = next((item for item in merged["models"] if item.get("isCurrent")), None)
                active = active or (merged["models"][0] if merged["models"] else None)
                for key in ("name", "provider", "model", "baseUrl", "enabled", "temperature", "maxTokens", "timeout", "concurrency", "language", "device", "threads", "wordTimestamps", "voice", "speed", "format"):
                    if key in patch:
                        active[key] = patch[key]
            if models_updated and active:
                for key in ("name", "provider", "model", "baseUrl", "enabled", "temperature", "maxTokens", "timeout", "concurrency", "language", "device", "threads", "wordTimestamps", "voice", "speed", "format"):
                    if key in active:
                        merged[key] = active[key]
                if active.get("apiKey"):
                    merged["apiKey"] = active["apiKey"]
            if kind == "whisper" and models_updated and active:
                if "enabled" in patch:
                    active["enabled"] = patch["enabled"]
                merged["enabled"] = active.get("enabled", merged.get("enabled", True))
            submitted_key = patch.get("apiKey")
            if submitted_key:
                selected_card = next(
                    (item for item in merged.get("models", []) if item.get("isCurrent")),
                    None,
                )
                if selected_card is not None:
                    selected_card["apiKey"] = str(submitted_key)
            current[kind] = merged
        return current

    def update(
        self,
        values: dict[str, dict[str, Any]],
        *,
        prepared: dict[str, dict[str, Any]] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        for kind in values:
            if kind not in KINDS:
                raise KeyError(kind)

        current: dict[str, dict[str, Any]] = {}

        def apply(document: dict[str, Any]) -> None:
            models = document.setdefault("models", {})
            if prepared is not None and expected_revision is not None:
                candidates = prepared
            else:
                base = {
                    kind: copy.deepcopy(models.get(kind) or _default_for(kind))
                    for kind in KINDS
                }
                candidates = self._prepare_values(values, base)
            for kind in values:
                current[kind] = copy.deepcopy(candidates[kind])
                models[kind] = copy.deepcopy(current[kind])

        provider = self._provider or get_config_provider()
        provider.mutate(apply, expected_revision=expected_revision)
        return current

    @staticmethod
    def _merge_models(
        old: list[dict[str, Any]], new: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_id = {str(item.get("id")): dict(item) for item in old if item.get("id")}
        selected_id = None
        for item in new:
            if not isinstance(item, dict):
                continue
            item = _strip_public_readonly(item)
            model_id = str(item.get("id") or "")
            if not model_id:
                model_id = "model-" + uuid.uuid4().hex[:10]
            merged = dict(by_id.get(model_id, {}))
            for key, value in item.items():
                if key == "apiKey" and not value:
                    continue
                merged[key] = copy.deepcopy(value)
            merged["id"] = model_id
            by_id[model_id] = merged
            if item.get("isCurrent"):
                selected_id = model_id
        if selected_id:
            for item in by_id.values():
                item["isCurrent"] = item.get("id") == selected_id
        return list(by_id.values())

    def prepare_create(self, kind: str, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        item, current, _revision = self.prepare_create_versioned(kind, payload)
        return item, current

    def prepare_create_versioned(
        self, kind: str, payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], int]:
        if kind not in KINDS:
            raise KeyError(kind)
        snapshot = self._read()
        current = copy.deepcopy(snapshot.values.get("models", {}).get(kind) or _default_for(kind))
        current["_runtimeManaged"] = True
        item = _strip_public_readonly(payload)
        # Creation IDs are owned by the server; clients update an existing
        # card through PUT with its persisted ID instead.
        item["id"] = f"{kind}-" + uuid.uuid4().hex[:10]
        item.setdefault("name", item.get("model") or item["id"])
        item.setdefault("enabled", True)
        models = self._merge_models(list(current.get("models") or []), [item])
        for model in models:
            model["isCurrent"] = model.get("id") == item["id"]
        current["models"] = models
        for key in _MODEL_FIELDS:
            if key in item:
                current[key] = item[key]
        if item.get("apiKey"):
            current["apiKey"] = item["apiKey"]
        validate_models({kind: current})
        saved = next(model for model in current["models"] if model["id"] == item["id"])
        return saved, current, snapshot.revision

    def create_model(
        self, kind: str, payload: dict[str, Any], *, expected_revision: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        saved, candidate, revision = self.prepare_create_versioned(kind, payload)
        self.update(
            {kind: candidate}, prepared={kind: candidate},
            expected_revision=expected_revision if expected_revision is not None else revision,
        )
        return saved, candidate


_MODEL_FIELDS = (
    "name", "provider", "model", "baseUrl", "enabled", "temperature",
    "maxTokens", "timeout", "concurrency", "language", "device", "threads",
    "wordTimestamps", "voice", "speed", "format",
)
