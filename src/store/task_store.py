"""任务记录的 SQLite 存储。

单文件数据库，零服务进程；开启 WAL 以支持 API 进程读、Worker 进程写并发。
字段与前端契约对齐（status / progress / current_step / outputs / 时间戳）。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import List, Optional

# 与流水线状态机一致
STATUSES = (
    "PENDING",
    "DOWNLOADING",
    "EXTRACTING",
    "TRANSCRIBING",
    "TRANSLATING",
    "BURNING",
    "SYNTHESIZING",
    "DUBBING",
    "SUCCESS",
    "FAILED",
    "CANCELLED",
)

# 资源可用性：与流水线 status 解耦，专门描述"任务已经成功 / 失败，
# 但磁盘上的产物是否还在"。服务重启或运行中下载时若发现资源缺失，
# 会把 resource_status 置为 MISSING，避免继续暴露已失效的下载链接。
RESOURCE_STATUS_AVAILABLE = "AVAILABLE"
RESOURCE_STATUS_MISSING = "MISSING"
RESOURCE_STATUSES = (RESOURCE_STATUS_AVAILABLE, RESOURCE_STATUS_MISSING)

DOWNGRADE_REASON_USER_CLEANED = "USER_CLEANED"
DOWNGRADE_REASON_DISK_FAILURE = "DISK_FAILURE"
DOWNGRADE_REASON_VOLUME_MIGRATED = "VOLUME_MIGRATED"
DOWNGRADE_REASON_UNKNOWN = "UNKNOWN"
DOWNGRADE_REASONS = (
    DOWNGRADE_REASON_USER_CLEANED,
    DOWNGRADE_REASON_DISK_FAILURE,
    DOWNGRADE_REASON_VOLUME_MIGRATED,
    DOWNGRADE_REASON_UNKNOWN,
)


@dataclass
class TaskRecord:
    id: str
    url: str
    source_lang: str
    target_lang: str
    mode: str          # mono | bilingual
    burn: str          # hard | soft
    model: str         # whisper 模型
    engine: str        # 翻译引擎配置 ID；deepseek 为旧版兼容值
    url_hash: Optional[str] = None
    source_type: str = "url"  # url=在线链接下载 upload=本地上传视频
    task_origin: str = "web"  # web=网页创建 mcp=MCP 创建
    need_subtitle: int = 1  # 1=需要字幕(完整流水线) 0=仅下载视频
    status: str = "PENDING"
    progress: int = 0
    current_step: Optional[str] = None
    title: Optional[str] = None
    error: Optional[str] = None
    output_video: Optional[str] = None
    output_subtitle: Optional[str] = None
    created_at: int = 0   # epoch 毫秒
    updated_at: int = 0
    resource_status: str = RESOURCE_STATUS_AVAILABLE  # 产物文件是否在盘
    error_code: Optional[str] = None
    downgrade_reason: Optional[str] = None
    downgrade_errno: Optional[int] = None
    downgraded_at: Optional[int] = None
    is_cancelling: int = 0  # 1=取消清理进行中，0=未取消
    quality: str = "480p"  # 下载清晰度策略：best/1080p/720p/480p/360p/audio_only
    tts_enabled: int = 0
    tts_voice: str = "auto"
    original_voice_mode: str = "keep"
    tts_status: str = "DISABLED"
    tts_error: Optional[str] = None
    output_dubbed_video: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


_COLUMNS = list(TaskRecord.__dataclass_fields__.keys())


def _now_ms() -> int:
    return int(time.time() * 1000)


def _calc_url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _portable_artifact_name(value: Optional[str], task_id: str) -> Optional[str]:
    """Persist only a task-local filename, regardless of the host OS."""
    if not value:
        return None
    raw = str(value)
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    name = windows.name if windows.name != raw else posix.name
    if not name or name in {".", ".."}:
        return None
    parts = {part for part in (*posix.parts, *windows.parts) if part not in {"/", "\\"}}
    if (posix.is_absolute() or windows.is_absolute()) and task_id not in parts:
        return None
    return name


class TaskStore:
    """任务表的增删改查。每次操作开一个短连接，交给 SQLite 处理文件锁。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

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
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    url_hash TEXT,
                    source_lang TEXT NOT NULL,
                    target_lang TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    burn TEXT NOT NULL,
                    model TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'url',
                    task_origin TEXT NOT NULL DEFAULT 'web',
                    need_subtitle INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    current_step TEXT,
                    title TEXT,
                    error TEXT,
                    output_video TEXT,
                    output_subtitle TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    resource_status TEXT NOT NULL DEFAULT 'AVAILABLE',
                    error_code TEXT,
                    downgrade_reason TEXT,
                    downgrade_errno INTEGER,
                    downgraded_at INTEGER,
                    is_cancelling INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # 轻量迁移：给旧库补上后加的列
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            if "url_hash" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN url_hash TEXT")
                rows = conn.execute("SELECT id, url FROM tasks WHERE url_hash IS NULL").fetchall()
                conn.executemany(
                    "UPDATE tasks SET url_hash = ? WHERE id = ?",
                    [(_calc_url_hash(row["url"]), row["id"]) for row in rows],
                )
            if "need_subtitle" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN need_subtitle INTEGER NOT NULL DEFAULT 1")
            if "source_type" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN source_type TEXT NOT NULL DEFAULT 'url'")
            if "task_origin" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN task_origin TEXT NOT NULL DEFAULT 'web'")
            if "resource_status" not in cols:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN resource_status TEXT NOT NULL DEFAULT 'AVAILABLE'"
                )
            if "error_code" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN error_code TEXT")
            if "downgrade_reason" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN downgrade_reason TEXT")
            if "downgrade_errno" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN downgrade_errno INTEGER")
            if "is_cancelling" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN is_cancelling INTEGER NOT NULL DEFAULT 0")
            if "downgraded_at" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN downgraded_at INTEGER")
            if "quality" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN quality TEXT NOT NULL DEFAULT '480p'")
            for name, declaration in (
                ("tts_enabled", "INTEGER NOT NULL DEFAULT 0"),
                ("tts_voice", "TEXT NOT NULL DEFAULT 'auto'"),
                ("original_voice_mode", "TEXT NOT NULL DEFAULT 'keep'"),
                ("tts_status", "TEXT NOT NULL DEFAULT 'DISABLED'"),
                ("tts_error", "TEXT"),
            ):
                if name not in cols:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")
            if "output_dubbed_video" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN output_dubbed_video TEXT")
            # Older versions persisted absolute output paths. Keep only the
            # task-local name so the database survives volume and OS changes.
            for row in conn.execute(
                "SELECT id, output_video, output_subtitle FROM tasks "
                "WHERE output_video IS NOT NULL OR output_subtitle IS NOT NULL"
            ):
                video = _portable_artifact_name(row["output_video"], row["id"])
                subtitle = _portable_artifact_name(row["output_subtitle"], row["id"])
                if video != row["output_video"] or subtitle != row["output_subtitle"]:
                    conn.execute(
                        "UPDATE tasks SET output_video = ?, output_subtitle = ? WHERE id = ?",
                        (video, subtitle, row["id"]),
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_created_at "
                "ON tasks (created_at DESC, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_url_hash_status_created_at "
                "ON tasks (url_hash, status, created_at DESC)"
            )

    # ---------- 增 ----------
    def create(
        self,
        *,
        url: str,
        source_lang: str,
        target_lang: str,
        mode: str,
        burn: str,
        model: str,
        engine: str,
        source_type: str = "url",
        task_origin: str = "web",
        need_subtitle: bool = True,
        title: Optional[str] = None,
        quality: Optional[str] = "480p",
        tts_enabled: bool = False,
        tts_voice: str = "auto",
        original_voice_mode: str = "keep",
    ) -> TaskRecord:
        with self._connect() as conn:
            return self._insert(
                conn,
                url=url,
                source_lang=source_lang,
                target_lang=target_lang,
                mode=mode,
                burn=burn,
                model=model,
                engine=engine,
                source_type=source_type,
                task_origin=task_origin,
                need_subtitle=need_subtitle,
                title=title,
                quality=quality or "480p",
                tts_enabled=tts_enabled,
                tts_voice=tts_voice,
                original_voice_mode=original_voice_mode,
            )

    def create_if_no_recent_active(
        self,
        *,
        window_min: int = 10,
        **kwargs,
    ) -> tuple[TaskRecord, bool]:
        """Atomically create a URL task unless a recent active one exists."""
        url = kwargs["url"]
        url_hash = _calc_url_hash(url)
        cutoff = _now_ms() - window_min * 60 * 1000
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tasks WHERE url_hash = ? AND created_at >= ? "
                "AND task_origin = ? "
                "AND status IN ('PENDING', 'DOWNLOADING', 'EXTRACTING', 'TRANSCRIBING', 'TRANSLATING', 'BURNING', 'SYNTHESIZING', 'DUBBING') "
                "ORDER BY created_at DESC LIMIT 1",
                (url_hash, cutoff, kwargs.get("task_origin", "web")),
            ).fetchone()
            if row:
                return _row_to_record(row), False
            return self._insert(conn, **kwargs), True

    def _insert(self, conn: sqlite3.Connection, **kwargs) -> TaskRecord:
        now = _now_ms()
        rec = TaskRecord(
            id="task_" + uuid.uuid4().hex[:8],
            url=kwargs["url"],
            source_lang=kwargs["source_lang"],
            target_lang=kwargs["target_lang"],
            mode=kwargs["mode"],
            burn=kwargs["burn"],
            model=kwargs["model"],
            engine=kwargs["engine"],
            url_hash=_calc_url_hash(kwargs["url"]),
            source_type=kwargs.get("source_type", "url"),
            task_origin=kwargs.get("task_origin", "web"),
            need_subtitle=int(kwargs.get("need_subtitle", True)),
            title=kwargs.get("title"),
            quality=kwargs.get("quality") or "480p",
            tts_enabled=int(kwargs.get("tts_enabled", False)),
            tts_voice=kwargs.get("tts_voice", "auto"),
            original_voice_mode=kwargs.get("original_voice_mode", "keep"),
            tts_status="PENDING" if kwargs.get("tts_enabled", False) else "DISABLED",
            status="PENDING",
            progress=0,
            created_at=now,
            updated_at=now,
        )
        placeholders = ", ".join(["?"] * len(_COLUMNS))
        conn.execute(
            f"INSERT INTO tasks ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
            [getattr(rec, c) for c in _COLUMNS],
        )
        return rec

    # ---------- 查 ----------
    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_record(row) if row else None

    def list(
        self,
        limit: int = 0,
        offset: int = 0,
        before_id: Optional[str] = None,
        after_id: Optional[str] = None,
        task_origin: Optional[str] = None,
    ) -> List[TaskRecord]:
        where_clauses = []
        params = []

        if before_id:
            before_rec = self.get(before_id)
            if before_rec is None:
                return []
            where_clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([before_rec.created_at, before_rec.created_at, before_rec.id])

        if after_id:
            after_rec = self.get(after_id)
            if after_rec is None:
                return []
            where_clauses.append("(created_at > ? OR (created_at = ? AND id > ?))")
            params.extend([after_rec.created_at, after_rec.created_at, after_rec.id])

        if task_origin:
            where_clauses.append("task_origin = ?")
            params.append(task_origin)

        sql = "SELECT * FROM tasks"
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        if after_id and not before_id:
            sql += " ORDER BY created_at ASC, id ASC"
        else:
            sql += " ORDER BY created_at DESC, id DESC"

        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
            if offset and offset > 0:
                sql += " OFFSET ?"
                params.append(int(offset))
        elif offset and offset > 0:
            sql += " LIMIT -1 OFFSET ?"
            params.append(int(offset))

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        records = [_row_to_record(r) for r in rows]
        if after_id and not before_id:
            records.reverse()
        return records

    # ---------- 改 ----------
    def update(self, task_id: str, **fields) -> Optional[TaskRecord]:
        allowed = {k: v for k, v in fields.items() if k in _COLUMNS and k != "id"}
        if not allowed:
            return self.get(task_id)
        allowed["updated_at"] = _now_ms()
        assignments = ", ".join(f"{k} = ?" for k in allowed)
        values = list(allowed.values()) + [task_id]
        with self._connect() as conn:
            cur = conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", values)
            if cur.rowcount == 0:
                return None
        return self.get(task_id)

    # ---------- 删 ----------
    def delete(self, task_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return cur.rowcount > 0


def _row_to_record(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(**{c: row[c] for c in _COLUMNS})
