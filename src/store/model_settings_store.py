"""SQLite persistence for the three model settings cards.

The table deliberately stores the complete configuration as JSON.  This keeps
the settings contract extensible while preserving secrets server side; the
HTTP layer is responsible for masking ``apiKey`` before returning values.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

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


def _now_ms() -> int:
    return int(time.time() * 1000)


class ModelSettingsStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self):
        import sqlite3

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS model_settings (
                    kind TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )"""
            )

    def _ensure_defaults(self) -> None:
        with self._connect() as conn:
            rows = {row[0] for row in conn.execute("SELECT kind FROM model_settings")}
            for kind, config in DEFAULTS.items():
                if kind not in rows:
                    value = dict(config)
                    # Defaults preserve the historic task behavior until a user
                    # explicitly saves a unified model card.  The marker is
                    # internal and never exposed by the API.
                    value["_runtimeManaged"] = False
                    if kind == "llm":
                        value["apiKey"] = ""
                        value["models"] = [{
                            "id": "llm-deepseek", "name": "DeepSeek", "provider": "deepseek",
                            "model": "deepseek-chat", "baseUrl": "https://api.deepseek.com/v1",
                            "enabled": True, "isCurrent": True, "apiKey": "",
                        }]
                    conn.execute(
                        "INSERT INTO model_settings(kind, config_json, updated_at) VALUES (?, ?, ?)",
                        (kind, json.dumps(value, ensure_ascii=False), _now_ms()),
                    )

    def get_all(self) -> dict[str, dict[str, Any]]:
        self._ensure_defaults()
        with self._connect() as conn:
            rows = conn.execute("SELECT kind, config_json FROM model_settings").fetchall()
        return {row["kind"]: json.loads(row["config_json"]) for row in rows}

    def get(self, kind: str) -> dict[str, Any] | None:
        return self.get_all().get(kind)

    def prepare(self, values: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Prepare a complete candidate set without persisting any changes."""
        current = self.get_all()
        for kind, raw_patch in values.items():
            if kind not in KINDS:
                raise KeyError(kind)
            patch = _strip_public_readonly(raw_patch)
            if not patch:
                continue
            merged = dict(current[kind])
            merged["_runtimeManaged"] = True
            old_models = list(merged.get("models") or [])
            models_updated = bool(patch.get("models")) if isinstance(patch.get("models"), list) else False
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
            current[kind] = merged
        return current

    def update(self, values: dict[str, dict[str, Any]], *, prepared: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
        current = prepared or self.prepare(values)
        with self._connect() as conn:
            for kind in values:
                if kind not in KINDS:
                    raise KeyError(kind)
                merged = current[kind]
                if merged.get("_runtimeManaged") is False:
                    continue
                conn.execute(
                    "UPDATE model_settings SET config_json = ?, updated_at = ? WHERE kind = ?",
                    (json.dumps(merged, ensure_ascii=False), _now_ms(), kind),
                )
        return current

    @staticmethod
    def _merge_models(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
                merged[key] = value
            merged["id"] = model_id
            by_id[model_id] = merged
            if item.get("isCurrent"):
                selected_id = model_id
        if selected_id:
            for item in by_id.values():
                item["isCurrent"] = item.get("id") == selected_id
        return list(by_id.values())

    def prepare_create(self, kind: str, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if kind not in KINDS:
            raise KeyError(kind)
        current = self.get_all()[kind]
        current["_runtimeManaged"] = True
        item = _strip_public_readonly(payload)
        # Creation IDs are owned by the server; clients update an existing
        # card through PUT with its persisted ID instead.
        item["id"] = f"{kind}-" + uuid.uuid4().hex[:10]
        item.setdefault("name", item.get("model") or item["id"])
        item.setdefault("enabled", True)
        models = self._merge_models(list(current.get("models") or []), [item])
        current["models"] = models
        # A newly added card becomes the current model immediately.  The UI
        # reflects this in-memory and the persisted flag keeps it after reload.
        for model in models:
            model["isCurrent"] = model.get("id") == item["id"]
        for key in ("name", "provider", "model", "baseUrl", "enabled", "temperature", "maxTokens", "timeout", "concurrency"):
            if key in item:
                current[key] = item[key]
        for key in ("language", "device", "threads", "wordTimestamps", "voice", "speed", "format"):
            if key in item:
                current[key] = item[key]
        if item.get("apiKey"):
            current["apiKey"] = item["apiKey"]
        saved = next(model for model in current["models"] if model["id"] == item["id"])
        return saved, current

    def create_model(self, kind: str, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        saved, candidate = self.prepare_create(kind, payload)
        self.update({kind: candidate}, prepared={kind: candidate})
        return saved, candidate
