"""下载测试（probe）记录的 SQLite 存储。

与 task_store 共用同一个 db 文件（settings.db_path），开独立表 probe_records。
每次调用 /api/tasks/probe 都落一行，便于用户回看历史测试、排查"链接换格式
还是不可下载"等问题；历史只做"写入 / 列表 / 单条删除 / 一键清空"，不与任务表
产生外键耦合——probe 仅是只读探测，不下载、不入队。

字段设计上尽量对齐 ProbeResult，避免来回转换。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class ProbeRecord:
    """单次下载测试的持久化记录。"""

    id: str
    url: str
    ok: int                   # 0 / 1，对齐 SQLite 习惯
    title: Optional[str] = None
    extractor: Optional[str] = None
    duration: Optional[float] = None
    formats_count: int = 0
    webpage_url: Optional[str] = None
    reason: Optional[str] = None
    detail: Optional[str] = None
    created_at: int = 0       # epoch 毫秒
    url_hash: Optional[str] = None  # sha256(url)[:16]
    language: Optional[str] = None
    formats_json: Optional[str] = None
    qualities: Optional[str] = None
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None

    @property
    def available_qualities(self) -> list[str]:
        if not self.qualities:
            return []
        try:
            val = json.loads(self.qualities)
            if isinstance(val, list):
                return [str(x) for x in val]
        except Exception:
            pass
        return [q.strip() for q in self.qualities.split(",") if q.strip()]

    @property
    def parsed_formats(self) -> list[dict]:
        if not self.formats_json:
            return []
        try:
            val = json.loads(self.formats_json)
            if isinstance(val, list):
                return val
        except Exception:
            pass
        return []

    def to_dict(self) -> dict:
        return asdict(self)


_COLUMNS = list(ProbeRecord.__dataclass_fields__.keys())


def _now_ms() -> int:
    return int(time.time() * 1000)


def _calc_url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


class ProbeStore:
    """probe_records 表的增删改查。每次操作短连接，SQLite 自带文件锁。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # 与 TaskStore 保持一致：WAL + busy_timeout，便于多进程并发。
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS probe_records (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    title TEXT,
                    extractor TEXT,
                    duration REAL,
                    formats_count INTEGER NOT NULL DEFAULT 0,
                    webpage_url TEXT,
                    reason TEXT,
                    detail TEXT,
                    created_at INTEGER NOT NULL,
                    url_hash TEXT,
                    language TEXT,
                    formats_json TEXT,
                    qualities TEXT,
                    thumbnail TEXT,
                    uploader TEXT
                )
                """
            )
            # Schema 迁移：若已有旧表无某列则补充
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(probe_records)").fetchall()]
            if "url_hash" not in cols:
                conn.execute("ALTER TABLE probe_records ADD COLUMN url_hash TEXT")
                rows = conn.execute("SELECT id, url FROM probe_records WHERE url_hash IS NULL").fetchall()
                for r in rows:
                    conn.execute("UPDATE probe_records SET url_hash = ? WHERE id = ?", (_calc_url_hash(r["url"]), r["id"]))
            if "language" not in cols:
                conn.execute("ALTER TABLE probe_records ADD COLUMN language TEXT")
            if "formats_json" not in cols:
                conn.execute("ALTER TABLE probe_records ADD COLUMN formats_json TEXT")
            if "qualities" not in cols:
                conn.execute("ALTER TABLE probe_records ADD COLUMN qualities TEXT")
            if "thumbnail" not in cols:
                conn.execute("ALTER TABLE probe_records ADD COLUMN thumbnail TEXT")
            if "uploader" not in cols:
                conn.execute("ALTER TABLE probe_records ADD COLUMN uploader TEXT")

            # 列表默认按时间倒序，建索引避免大表全表扫
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_probe_records_created_at "
                "ON probe_records (created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_probe_records_url_hash "
                "ON probe_records (url_hash, created_at DESC)"
            )

    # ---------- 增 ----------
    def record(
        self,
        *,
        url: str,
        ok: bool,
        title: Optional[str] = None,
        extractor: Optional[str] = None,
        duration: Optional[float] = None,
        formats_count: int = 0,
        webpage_url: Optional[str] = None,
        reason: Optional[str] = None,
        detail: Optional[str] = None,
        language: Optional[str] = None,
        available_qualities: Optional[list[str]] = None,
        formats: Optional[list[dict]] = None,
        thumbnail: Optional[str] = None,
        uploader: Optional[str] = None,
    ) -> ProbeRecord:
        """写入或更新一条测试记录。若同 URL 在当前小时桶已存在，则覆盖更新最新结果。"""
        now = _now_ms()
        url_hash = _calc_url_hash(url)
        start_of_hour = (now // 3600000) * 3600000
        ok_val = 1 if ok else 0
        formats_json = json.dumps(formats, ensure_ascii=False) if formats else None
        qualities_str = json.dumps(available_qualities, ensure_ascii=False) if available_qualities else None

        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM probe_records WHERE url_hash = ? AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT 1",
                (url_hash, start_of_hour),
            ).fetchone()

            if row:
                rec_id = row["id"]
                conn.execute(
                    """
                    UPDATE probe_records
                    SET url = ?, ok = ?, title = ?, extractor = ?, duration = ?,
                        formats_count = ?, webpage_url = ?, reason = ?, detail = ?,
                        created_at = ?, language = ?, formats_json = ?, qualities = ?,
                        thumbnail = ?, uploader = ?
                    WHERE id = ?
                    """,
                    (
                        url,
                        ok_val,
                        title,
                        extractor,
                        duration,
                        formats_count,
                        webpage_url,
                        reason,
                        detail,
                        now,
                        language,
                        formats_json,
                        qualities_str,
                        thumbnail,
                        uploader,
                        rec_id,
                    ),
                )
            else:
                rec_id = "probe_" + uuid.uuid4().hex[:8]
                rec = ProbeRecord(
                    id=rec_id,
                    url=url,
                    ok=ok_val,
                    title=title,
                    extractor=extractor,
                    duration=duration,
                    formats_count=formats_count,
                    webpage_url=webpage_url,
                    reason=reason,
                    detail=detail,
                    created_at=now,
                    url_hash=url_hash,
                    language=language,
                    formats_json=formats_json,
                    qualities=qualities_str,
                    thumbnail=thumbnail,
                    uploader=uploader,
                )
                values = [getattr(rec, c) for c in _COLUMNS]
                placeholders = ", ".join(["?"] * len(_COLUMNS))
                conn.execute(
                    f"INSERT INTO probe_records ({', '.join(_COLUMNS)}) "
                    f"VALUES ({placeholders})",
                    values,
                )

        return ProbeRecord(
            id=rec_id,
            url=url,
            ok=ok_val,
            title=title,
            extractor=extractor,
            duration=duration,
            formats_count=formats_count,
            webpage_url=webpage_url,
            reason=reason,
            detail=detail,
            created_at=now,
            url_hash=url_hash,
            language=language,
            formats_json=formats_json,
            qualities=qualities_str,
            thumbnail=thumbnail,
            uploader=uploader,
        )

    # ---------- 查 ----------
    def list(self, limit: int = 50) -> List[ProbeRecord]:
        """按时间倒序列出最近 limit 条；limit<=0 表示不限制。"""
        # created_at is millisecond precision; rowid keeps same-ms writes newest-first.
        sql = "SELECT * FROM probe_records ORDER BY created_at DESC, rowid DESC"
        params: tuple = ()
        if limit and limit > 0:
            sql += " LIMIT ?"
            params = (int(limit),)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_record(r) for r in rows]

    def get(self, record_id: str) -> Optional[ProbeRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM probe_records WHERE id = ?", (record_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    # ---------- 删 ----------
    def delete(self, record_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM probe_records WHERE id = ?", (record_id,)
            )
            return cur.rowcount > 0

    def clear(self) -> int:
        """清空所有 probe 记录，返回删除的行数。"""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM probe_records")
            return cur.rowcount

    def cleanup_older_than_days(self, days: int) -> int:
        """清理早于 N 天前的测试记录，返回清理的行数。"""
        if days < 0:
            return 0
        cutoff = _now_ms() - days * 86400 * 1000
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM probe_records WHERE created_at < ?", (cutoff,))
            return cur.rowcount


def _row_to_record(row: sqlite3.Row) -> ProbeRecord:
    return ProbeRecord(**{c: row[c] for c in _COLUMNS})
