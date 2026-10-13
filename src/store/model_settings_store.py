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

    def update(
        self, values: dict[str, dict[str, Any]], *, expected_revision: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        for kind in values:
            if kind not in KINDS:
                raise KeyError(kind)

        def apply(document: dict[str, Any]) -> None:
            models = document.setdefault("models", {})
            for kind, patch in values.items():
                current = copy.deepcopy(models.get(kind) or _default_for(kind))
                current["_runtimeManaged"] = True
                old_models = list(current.get("models") or [])
                models_updated = isinstance(patch.get("models"), list)
                for key, value in patch.items():
                    if key == "apiKey":
                        if value:
                            current[key] = str(value)
                    elif key == "models" and isinstance(value, list):
                        current["models"] = self._merge_models(old_models, value)
                    else:
                        current[key] = copy.deepcopy(value)

                if models_updated:
                    current_candidates = [
                        item for item in current.get("models", [])
                        if item.get("isCurrent")
                    ]
                    if len(current_candidates) > 1:
                        selected_id = current_candidates[-1].get("id")
                        for item in current["models"]:
                            item["isCurrent"] = item.get("id") == selected_id
                active = next(
                    (item for item in current.get("models", []) if item.get("isCurrent")),
                    None,
                )
                if not models_updated and old_models:
                    active = active or old_models[0]
                    for key in _MODEL_FIELDS:
                        if key in patch:
                            active[key] = patch[key]
                    current["models"] = old_models
                if models_updated and active:
                    for key in _MODEL_FIELDS:
                        if key in active:
                            current[key] = active[key]
                    if active.get("apiKey"):
                        current["apiKey"] = active["apiKey"]
                submitted_key = patch.get("apiKey")
                if submitted_key:
                    selected_card = next(
                        (item for item in current.get("models", []) if item.get("isCurrent")),
                        None,
                    )
                    if selected_card is not None:
                        selected_card["apiKey"] = str(submitted_key)
                validate_models({kind: current})
                models[kind] = current

        provider = self._provider or get_config_provider()
        provider.mutate(apply, expected_revision=expected_revision)
        return self.get_all()

    @staticmethod
    def _merge_models(
        old: list[dict[str, Any]], new: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_id = {str(item.get("id")): dict(item) for item in old if item.get("id")}
        for item in new:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "") or "model-" + uuid.uuid4().hex[:10]
            merged = dict(by_id.get(model_id, {}))
            for key, value in item.items():
                if key == "apiKey" and not value:
                    continue
                merged[key] = copy.deepcopy(value)
            merged["id"] = model_id
            by_id[model_id] = merged
        return list(by_id.values())

    def create_model(
        self, kind: str, payload: dict[str, Any], *, expected_revision: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if kind not in KINDS:
            raise KeyError(kind)
        item = dict(payload)
        item["id"] = f"{kind}-" + uuid.uuid4().hex[:10]
        item.setdefault("name", item.get("model") or item["id"])
        item.setdefault("enabled", True)
        candidate = _default_for(kind)
        candidate["_runtimeManaged"] = True
        candidate["models"] = [item]
        for key in _MODEL_FIELDS:
            if key in item:
                candidate[key] = item[key]
        validate_models({kind: candidate})

        def apply(document: dict[str, Any]) -> None:
            models = document.setdefault("models", {})
            current = copy.deepcopy(models.get(kind) or _default_for(kind))
            current["_runtimeManaged"] = True
            cards = self._merge_models(list(current.get("models") or []), [item])
            for card in cards:
                card["isCurrent"] = card.get("id") == item["id"]
            current["models"] = cards
            for key in _MODEL_FIELDS:
                if key in item:
                    current[key] = item[key]
            if item.get("apiKey"):
                current["apiKey"] = item["apiKey"]
            models[kind] = current

        provider = self._provider or get_config_provider()
        provider.mutate(apply, expected_revision=expected_revision)
        config = self.get(kind) or _default_for(kind)
        saved = next(card for card in config["models"] if card["id"] == item["id"])
        return saved, config


_MODEL_FIELDS = (
    "name", "provider", "model", "baseUrl", "enabled", "temperature",
    "maxTokens", "timeout", "concurrency", "language", "device", "threads",
    "wordTimestamps", "voice", "speed", "format",
)
