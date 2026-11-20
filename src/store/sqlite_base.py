"""SQLite 存储的共用底座。

ProbeStore 与 TaskStore 原来各写了一份：建目录、开短连接（WAL +
busy_timeout）、毫秒时间戳、url 的 sha256 短哈希。改一处要改两处，
所以只留一份在这里，两个 store 都继承它。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path


def now_ms() -> int:
    """当前时间的 epoch 毫秒。"""
    return int(time.time() * 1000)


def calc_url_hash(url: str) -> str:
    """url 的 sha256 前 16 位十六进制字符，用作去重键。"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


class SqliteStore:
    """SQLite 单文件存储的共用部分：建目录、开短连接、建表钩子。

    子类只负责自己的建表 SQL 与增删改查；连接行为（WAL +
    busy_timeout，便于多进程并发）由这里统一管。
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # WAL + busy_timeout，便于多进程并发。
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        """建表与轻量迁移，由子类实现各自的表结构。"""
        raise NotImplementedError
