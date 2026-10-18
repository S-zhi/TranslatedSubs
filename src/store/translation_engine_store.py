"""Redis-backed translation engine records.

The legacy ``db_path`` constructor argument remains accepted for callers that
also own a SQLite task store. Engine configuration and credentials themselves
are read and written only through the shared Redis configuration provider.
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

from src.config.config_repository import ConfigSnapshot, get_config_provider
from src.config.model_schema import validate_engines
from src.store.plugin_instance_store import PluginInstanceConflict

ENGINE_TYPES = ("openai_compatible", "anthropic_compatible", "local_ct2")
AVAILABILITY = ("UNCONFIGURED", "UNKNOWN", "CHECKING", "AVAILABLE", "UNAVAILABLE")
LOCAL_TRANSLATION_ENGINE_ID = "local-opus-en-zh"
DEFAULT_TRANSLATION_ENGINE_ID = LOCAL_TRANSLATION_ENGINE_ID
LOCAL_TRANSLATION_ENGINE_NAME = "本地 CPU 英译中"
LOCAL_TRANSLATION_MODEL = "Helsinki-NLP/opus-mt-en-zh"


@dataclass
class TranslationEngine:
    id: str
    name: str
    api_type: str
    base_url: str
    model: str
    api_key: Optional[str] = None
    enabled: int = 1
    availability: str = "UNCONFIGURED"
    last_checked_at: Optional[int] = None
    last_error: Optional[str] = None
    api_key_rotated_at: Optional[int] = None
    created_at: int = 0
    updated_at: int = 0

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key and self.api_key.strip())


def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_engine(value: dict | None) -> TranslationEngine | None:
    if not value:
        return None
    fields = {field: value.get(field) for field in TranslationEngine.__dataclass_fields__}
    return TranslationEngine(**fields)


class TranslationEngineStore:
    def __init__(self, db_path: Path | str | None = None, *, provider=None,
                 snapshot: ConfigSnapshot | None = None):
        # Retained for source compatibility only. No SQLite file is opened.
        self.db_path = Path(db_path) if db_path is not None else None
        self._provider = provider
        self._snapshot = snapshot
        if snapshot is None:
            self.reset_dangling_checking()

    def _read(self) -> ConfigSnapshot:
        return self._snapshot or (self._provider or get_config_provider()).read()

    def _engines(self) -> dict[str, dict]:
        by_id = self._read().values.get("engines", {}).get("byId", {})
        return copy.deepcopy(by_id)

    def _mutate(self, callback, *, expected_revision: int | None = None) -> ConfigSnapshot:
        return (self._provider or get_config_provider()).mutate(
            callback, expected_revision=expected_revision,
        )

    def reset_dangling_checking(self) -> int:
        snapshot = self._read()
        values = snapshot.values
        by_id = values.get("engines", {}).get("byId", {})
        stale = [engine_id for engine_id, item in by_id.items()
                 if item.get("availability") == "CHECKING"]
        if not stale:
            return 0

        def apply(document: dict) -> None:
            records = document.setdefault("engines", {}).setdefault("byId", {})
            for engine_id in stale:
                item = records.get(engine_id)
                if item and item.get("availability") == "CHECKING":
                    item["availability"] = (
                        "UNKNOWN" if str(item.get("api_key") or "").strip()
                        else "UNCONFIGURED"
                    )

        self._mutate(apply, expected_revision=snapshot.revision)
        return len(stale)

    def list(self) -> List[TranslationEngine]:
        records = self._engines()
        return [engine for value in records.values() if (engine := _to_engine(value))]

    def ensure_default_deepseek(
        self, *, api_key: Optional[str] = None,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
    ) -> TranslationEngine:
        """Ensure the legacy ID exists without importing environment secrets.

        ``api_key`` is accepted for call compatibility but deliberately ignored;
        keys must be entered through the Redis-backed settings API.
        """
        now = _now_ms()
        record = {
            "id": "deepseek", "name": "DeepSeek", "api_type": "openai_compatible",
            "base_url": base_url.strip().rstrip("/"), "model": model.strip(),
            "api_key": None, "enabled": 1, "availability": "UNCONFIGURED",
            "last_checked_at": None, "last_error": None, "api_key_rotated_at": None,
            "created_at": now, "updated_at": now,
        }
        def apply(document: dict) -> None:
            by_id = document.setdefault("engines", {}).setdefault("byId", {})
            by_id.setdefault("deepseek", copy.deepcopy(record))
        self._mutate(apply)
        return self.get("deepseek") or TranslationEngine(**record)

    def ensure_local_ct2(self) -> TranslationEngine:
        now = _now_ms()
        record = {
            "id": LOCAL_TRANSLATION_ENGINE_ID, "name": LOCAL_TRANSLATION_ENGINE_NAME,
            "api_type": "local_ct2", "base_url": "", "model": LOCAL_TRANSLATION_MODEL,
            "api_key": None, "enabled": 1, "availability": "UNKNOWN",
            "last_checked_at": None, "last_error": None, "api_key_rotated_at": None,
            "created_at": now, "updated_at": now,
        }
        def apply(document: dict) -> None:
            by_id = document.setdefault("engines", {}).setdefault("byId", {})
            existing = by_id.get(LOCAL_TRANSLATION_ENGINE_ID)
            if existing is None:
                by_id[LOCAL_TRANSLATION_ENGINE_ID] = copy.deepcopy(record)
            else:
                existing.update({
                    "name": record["name"], "api_type": record["api_type"],
                    "base_url": "", "model": record["model"], "api_key": None,
                    "api_key_rotated_at": None,
                })
        self._mutate(apply)
        return self.get(LOCAL_TRANSLATION_ENGINE_ID) or TranslationEngine(**record)

    def get(self, engine_id: str) -> Optional[TranslationEngine]:
        return _to_engine(self._engines().get(engine_id))

    def create(
        self, *, name: str, api_type: str, base_url: str, model: str,
        api_key: Optional[str] = None, enabled: bool = True,
    ) -> TranslationEngine:
        now = _now_ms()
        normalized_key = api_key.strip() if api_key and api_key.strip() else None
        rec = TranslationEngine(
            id="engine_" + uuid.uuid4().hex[:10], name=name.strip(), api_type=api_type,
            base_url=base_url.strip().rstrip("/"), model=model.strip(),
            api_key=normalized_key, enabled=int(enabled),
            availability="UNKNOWN" if normalized_key else "UNCONFIGURED",
            created_at=now, updated_at=now,
            api_key_rotated_at=now if normalized_key else None,
        )
        validate_engines({rec.id: asdict(rec)})
        def apply(document: dict) -> None:
            document.setdefault("engines", {}).setdefault("byId", {})[rec.id] = asdict(rec)
        self._mutate(apply)
        return rec

    def update(self, engine_id: str, **fields) -> Optional[TranslationEngine]:
        allowed = {key: value for key, value in fields.items() if key in {
            "name", "api_type", "base_url", "model", "api_key", "enabled",
            "availability", "last_checked_at", "last_error",
        }}
        snapshot = self._read()
        existing = snapshot.values.get("engines", {}).get("byId", {}).get(engine_id)
        rec = _to_engine(existing)
        if rec is None:
            return None
        if "base_url" in allowed:
            allowed["base_url"] = str(allowed["base_url"]).strip().rstrip("/")
        if "api_key" in allowed:
            raw_key = allowed["api_key"].strip() if allowed["api_key"] else ""
            if not raw_key or raw_key == rec.api_key:
                del allowed["api_key"]
            else:
                allowed["api_key"] = raw_key
                allowed["api_key_rotated_at"] = _now_ms()
                allowed.setdefault("availability", "UNKNOWN")
        if not allowed:
            return rec
        allowed["updated_at"] = _now_ms()
        candidate = asdict(rec)
        candidate.update(copy.deepcopy(allowed))
        validate_engines({engine_id: candidate})
        def apply(document: dict) -> None:
            current = document.setdefault("engines", {}).setdefault("byId", {}).get(engine_id)
            if current is None:
                return
            current.update(copy.deepcopy(allowed))
        self._mutate(apply, expected_revision=snapshot.revision)
        return self.get(engine_id)

    def delete(self, engine_id: str) -> bool:
        existed = self.get(engine_id) is not None
        if not existed:
            return False
        def apply(document: dict) -> None:
            instances = document.get("plugins", {}).get("byId", {}).values()
            if any(
                instance.get("engine_ref") is not None
                and instance["engine_ref"].get("id") == engine_id
                for instance in instances
            ):
                raise PluginInstanceConflict("翻译引擎被插件实例引用，请先解除绑定")
            by_id = document.setdefault("engines", {}).setdefault("byId", {})
            item = by_id.get(engine_id)
            if item is not None:
                item["api_key"] = None
                item["api_key_rotated_at"] = None
                del by_id[engine_id]
        self._mutate(apply)
        return True
