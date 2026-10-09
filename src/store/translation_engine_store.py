"""持久化翻译引擎配置。

引擎配置保存的是协议、地址、模型和密钥；API 层只返回脱敏后的视图。
当前应用是本地工作台，因此密钥与 SQLite 位于同一受限本地数据目录，业务代码
不会把密钥写入日志、任务记录或前端响应。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from src.core.translation_engines import validate_base_url


ENGINE_TYPES = ("openai_compatible", "anthropic_compatible")
AVAILABILITY = ("UNCONFIGURED", "UNKNOWN", "CHECKING", "AVAILABLE", "UNAVAILABLE")


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


class TranslationEngineStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self.reset_dangling_checking()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS translation_engines (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    api_type TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    model TEXT NOT NULL,
                    api_key TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    availability TEXT NOT NULL DEFAULT 'UNCONFIGURED',
                    last_checked_at INTEGER,
                    last_error TEXT,
                    api_key_rotated_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(translation_engines)")}
            if "api_key_rotated_at" not in columns:
                conn.execute("ALTER TABLE translation_engines ADD COLUMN api_key_rotated_at INTEGER")

    def reset_dangling_checking(self) -> int:
        """重置所有因服务异常退出/崩溃残留的 CHECKING 状态。"""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE translation_engines
                SET availability = CASE
                    WHEN api_key IS NOT NULL AND trim(api_key) != '' THEN 'UNKNOWN'
                    ELSE 'UNCONFIGURED'
                END
                WHERE availability = 'CHECKING'
                """
            )
            return cur.rowcount

    def list(self) -> List[TranslationEngine]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM translation_engines ORDER BY created_at ASC").fetchall()
        return [_row_to_engine(row) for row in rows]

    def ensure_default_deepseek(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
    ) -> TranslationEngine:
        """确保旧版 DeepSeek 兼容配置在高级设置中可见。

        使用固定的 ``deepseek`` ID，以兼容已有任务记录和旧版环境变量配置。
        只在记录不存在时写入，用户后续在 UI 中的自定义不会被启动流程覆盖。
        """
        existing = self.get("deepseek")
        if existing is not None:
            return existing

        now = _now_ms()
        normalized_key = api_key.strip() if api_key and api_key.strip() else None
        rec = TranslationEngine(
            id="deepseek",
            name="DeepSeek",
            api_type="openai_compatible",
            base_url=base_url.strip().rstrip("/"),
            model=model.strip(),
            api_key=normalized_key,
            enabled=1,
            availability="UNKNOWN" if normalized_key else "UNCONFIGURED",
            created_at=now,
            updated_at=now,
            api_key_rotated_at=now if normalized_key else None,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO translation_engines
                (id, name, api_type, base_url, model, api_key, enabled, availability,
                 last_checked_at, last_error, api_key_rotated_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rec.id, rec.name, rec.api_type, rec.base_url, rec.model, rec.api_key,
                rec.enabled, rec.availability, None, None, rec.api_key_rotated_at,
                rec.created_at, rec.updated_at),
            )
        return self.get("deepseek") or rec

    def get(self, engine_id: str) -> Optional[TranslationEngine]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM translation_engines WHERE id = ?", (engine_id,)).fetchone()
        return _row_to_engine(row) if row else None

    def create(
        self,
        *,
        name: str,
        api_type: str,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        enabled: bool = True,
    ) -> TranslationEngine:
        now = _now_ms()
        normalized_key = api_key.strip() if api_key and api_key.strip() else None
        validated_url = validate_base_url(base_url)
        rec = TranslationEngine(
            id="engine_" + uuid.uuid4().hex[:10],
            name=name.strip(), api_type=api_type, base_url=validated_url,
            model=model.strip(), api_key=normalized_key, enabled=int(enabled),
            availability="UNKNOWN" if normalized_key else "UNCONFIGURED",
            created_at=now, updated_at=now,
            api_key_rotated_at=now if normalized_key else None,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO translation_engines
                (id, name, api_type, base_url, model, api_key, enabled, availability,
                 last_checked_at, last_error, api_key_rotated_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rec.id, rec.name, rec.api_type, rec.base_url, rec.model, rec.api_key,
                rec.enabled, rec.availability, None, None, rec.api_key_rotated_at,
                rec.created_at, rec.updated_at),
            )
        return rec

    def update(self, engine_id: str, **fields) -> Optional[TranslationEngine]:
        rec = self.get(engine_id)
        if rec is None:
            return None
        allowed = {k: v for k, v in fields.items() if k in {
            "name", "api_type", "base_url", "model", "api_key", "enabled",
            "availability", "last_checked_at", "last_error",
        }}
        if "base_url" in allowed:
            validated_url = validate_base_url(str(allowed["base_url"]))
            allowed["base_url"] = validated_url

            old_p = urlparse(rec.base_url)
            new_p = urlparse(validated_url)
            old_origin = (old_p.scheme.lower(), old_p.netloc.lower())
            new_origin = (new_p.scheme.lower(), new_p.netloc.lower())

            if old_origin != new_origin:
                new_key = fields.get("api_key")
                raw_new_key = str(new_key).strip() if new_key else ""
                if not raw_new_key:
                    allowed["api_key"] = None
                    allowed["api_key_rotated_at"] = None
                    allowed["availability"] = "UNCONFIGURED"

        if "api_key" in allowed:
            raw_key = allowed["api_key"].strip() if allowed["api_key"] else ""
            if not raw_key:
                # Blank values mean "not supplied" for update. Keep the old
                # secret and avoid changing updated_at for a key-only update.
                if allowed.get("api_key") is not None:
                    del allowed["api_key"]
            else:
                allowed["api_key"] = raw_key
                if raw_key != rec.api_key:
                    allowed["api_key_rotated_at"] = _now_ms()
                    allowed.setdefault("availability", "UNKNOWN")
                else:
                    # Re-sending the same key is a no-op unless other fields change.
                    del allowed["api_key"]
        if not allowed:
            return rec
        allowed["updated_at"] = _now_ms()
        assignments = ", ".join(f"{key} = ?" for key in allowed)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE translation_engines SET {assignments} WHERE id = ?",
                (*allowed.values(), engine_id),
            )
        return self.get(engine_id)

    def delete(self, engine_id: str) -> bool:
        with self._connect() as conn:
            # Clear the secret before deleting the row so it is not retained in
            # SQLite freelists/WAL longer than necessary.
            conn.execute(
                "UPDATE translation_engines SET api_key = NULL, api_key_rotated_at = NULL WHERE id = ?",
                (engine_id,),
            )
            cur = conn.execute("DELETE FROM translation_engines WHERE id = ?", (engine_id,))
        return cur.rowcount > 0


def _row_to_engine(row: sqlite3.Row) -> TranslationEngine:
    return TranslationEngine(**dict(row))
