"""任务存储（SQLite）单测。"""

from __future__ import annotations

import time

import pytest

from src.store import (
    RESOURCE_STATUS_AVAILABLE,
    RESOURCE_STATUS_MISSING,
    TaskRecord,
    TaskStore,
)


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "app.db")


def _create(store, **over):
    fields = dict(
        url="http://x/v",
        source_lang="auto",
        target_lang="zh-CN",
        mode="mono",
        burn="hard",
        model="small",
        engine="deepseek",
    )
    fields.update(over)
    return store.create(**fields)


def test_create_defaults(store):
    rec = _create(store)
    assert rec.id.startswith("task_")
    assert rec.status == "PENDING"
    assert rec.progress == 0
    assert rec.created_at > 0
    assert rec.created_at == rec.updated_at


def test_is_cancelling_defaults_to_false_and_round_trips(store):
    rec = _create(store)
    assert rec.is_cancelling == 0
    updated = store.update(rec.id, is_cancelling=1)
    assert updated.is_cancelling == 1
    assert TaskStore(store.db_path).get(rec.id).is_cancelling == 1



def test_get_roundtrip(store):
    rec = _create(store)
    got = store.get(rec.id)
    assert got is not None
    assert got == rec  # dataclass 相等


def test_create_defaults_source_type_url(store):
    rec = _create(store)
    assert rec.source_type == "url"


def test_create_upload_persists_source_type_and_title(store):
    rec = _create(store, source_type="upload", title="my clip")
    got = store.get(rec.id)
    assert got.source_type == "upload"
    assert got.title == "my clip"


def test_get_missing(store):
    assert store.get("nope") is None


def test_list_newest_first(store):
    a = _create(store)
    time.sleep(0.002)
    b = _create(store)
    ids = [r.id for r in store.list()]
    assert ids[0] == b.id and ids[1] == a.id


def test_list_pagination_limit_and_offset(store):
    recs = []
    for _ in range(5):
        recs.append(_create(store))
        time.sleep(0.001)

    # 全部正序的倒排列表 (最新到最老): recs[4], recs[3], recs[2], recs[1], recs[0]
    expected_ids = [r.id for r in reversed(recs)]

    p1 = store.list(limit=2, offset=0)
    assert [r.id for r in p1] == expected_ids[:2]

    p2 = store.list(limit=2, offset=2)
    assert [r.id for r in p2] == expected_ids[2:4]

    p3 = store.list(limit=2, offset=4)
    assert [r.id for r in p3] == expected_ids[4:]


def test_list_cursor_before_and_after(store):
    recs = []
    for _ in range(5):
        recs.append(_create(store))
        time.sleep(0.002)

    # recs: [r0, r1, r2, r3, r4], store.list() 返回 [r4, r3, r2, r1, r0]
    mid = recs[2]

    # before_id: 早于 r2 (更早创建) -> [r1, r0]
    older = store.list(before_id=mid.id)
    assert [r.id for r in older] == [recs[1].id, recs[0].id]

    # after_id: 晚于 r2 (更晚创建) -> [r4, r3]
    newer = store.list(after_id=mid.id)
    assert [r.id for r in newer] == [recs[4].id, recs[3].id]

    # 不存在的 id 游标返回空
    assert store.list(before_id="task_nonexistent") == []
    assert store.list(after_id="task_nonexistent") == []


def test_update_partial(store):
    rec = _create(store)
    before = rec.updated_at
    time.sleep(0.002)
    updated = store.update(rec.id, status="TRANSCRIBING", progress=42, current_step="TRANSCRIBING")
    assert updated.status == "TRANSCRIBING"
    assert updated.progress == 42
    assert updated.current_step == "TRANSCRIBING"
    assert updated.updated_at > before
    # url 等未传字段保持不变
    assert updated.url == rec.url


def test_update_outputs_and_title(store):
    rec = _create(store)
    updated = store.update(
        rec.id, status="SUCCESS", progress=100,
        title="My Video", output_video="/d/output.mp4", output_subtitle="/d/translated.srt",
    )
    assert updated.title == "My Video"
    assert updated.output_video == "/d/output.mp4"
    assert updated.output_subtitle == "/d/translated.srt"


def test_update_ignores_unknown_field(store):
    rec = _create(store)
    updated = store.update(rec.id, bogus="x", progress=5)
    assert updated.progress == 5
    assert not hasattr(updated, "bogus")


def test_update_missing_returns_none(store):
    assert store.update("nope", progress=1) is None


def test_delete(store):
    rec = _create(store)
    assert store.delete(rec.id) is True
    assert store.get(rec.id) is None
    assert store.delete(rec.id) is False


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "app.db"
    s1 = TaskStore(path)
    rec = _create(s1)
    s2 = TaskStore(path)  # 新实例读同一文件
    assert s2.get(rec.id) == rec


# ---------- resource_status 字段（issue #22） ----------

def test_resource_status_default_available(store):
    """新创建的任务默认是 AVAILABLE（资源还在）。"""
    rec = _create(store)
    assert rec.resource_status == RESOURCE_STATUS_AVAILABLE


def test_resource_status_round_trip(store):
    """resource_status 写入后可读回，模拟服务重启场景。"""
    rec = _create(store)
    updated = store.update(rec.id, resource_status=RESOURCE_STATUS_MISSING, error="资源已删除")
    assert updated.resource_status == RESOURCE_STATUS_MISSING
    assert updated.error == "资源已删除"

    # 重新打开 store 模拟重启
    s2 = TaskStore(store.db_path)
    got = s2.get(rec.id)
    assert got is not None
    assert got.resource_status == RESOURCE_STATUS_MISSING
    assert got.error == "资源已删除"


def test_resource_status_migration_adds_column(tmp_path):
    """旧库没有 resource_status 列时，初始化应补上并默认 AVAILABLE。"""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    # 旧版表结构（没有 resource_status、source_type、need_subtitle 之外最新列的子集）
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            source_lang TEXT NOT NULL,
            target_lang TEXT NOT NULL,
            mode TEXT NOT NULL,
            burn TEXT NOT NULL,
            model TEXT NOT NULL,
            engine TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'url',
            need_subtitle INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            progress INTEGER NOT NULL,
            current_step TEXT,
            title TEXT,
            error TEXT,
            output_video TEXT,
            output_subtitle TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks (id, url, source_lang, target_lang, mode, burn, model, engine, "
        "status, progress, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "task_legacy1", "http://x/v", "auto", "zh-CN", "mono", "hard", "small", "deepseek",
            "SUCCESS", 100, 0, 0,
        ),
    )
    conn.commit()
    conn.close()

    # 触发迁移
    s = TaskStore(db_path)
    rec = s.get("task_legacy1")
    assert rec is not None
    assert rec.resource_status == RESOURCE_STATUS_AVAILABLE
    assert rec.status == "SUCCESS"  # 其它字段保持不变
    assert rec.tts_enabled == 0
    assert rec.tts_status == "DISABLED"


def test_downgrade_audit_round_trip(store):
    rec = _create(store)
    now = int(time.time() * 1000)
    updated = store.update(
        rec.id,
        resource_status=RESOURCE_STATUS_MISSING,
        downgrade_reason="USER_CLEANED",
        downgraded_at=now,
    )
    assert updated.downgrade_reason == "USER_CLEANED"
    assert updated.downgraded_at == now

    reopened = TaskStore(store.db_path).get(rec.id)
    assert reopened.downgrade_reason == "USER_CLEANED"
    assert reopened.downgraded_at == now


def test_downgrade_audit_migration_adds_columns(tmp_path):
    import sqlite3

    db_path = tmp_path / "legacy_audit.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, url TEXT NOT NULL, source_lang TEXT NOT NULL,
            target_lang TEXT NOT NULL, mode TEXT NOT NULL, burn TEXT NOT NULL,
            model TEXT NOT NULL, engine TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'url',
            need_subtitle INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL, progress INTEGER NOT NULL,
            current_step TEXT, title TEXT, error TEXT,
            output_video TEXT, output_subtitle TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            resource_status TEXT NOT NULL DEFAULT 'AVAILABLE', error_code TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks (id, url, source_lang, target_lang, mode, burn, model, engine, "
        "status, progress, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("task_old", "http://x", "auto", "zh-CN", "mono", "hard", "small", "deepseek", "SUCCESS", 100, 0, 0),
    )
    conn.commit()
    conn.close()

    rec = TaskStore(db_path).get("task_old")
    assert rec.downgrade_reason is None
    assert rec.downgraded_at is None


def test_create_if_no_recent_active_reuses_running_task(store):
    fields = dict(url="http://x/v", source_lang="auto", target_lang="zh-CN", mode="mono", burn="hard", model="small", engine="deepseek")
    first, created = store.create_if_no_recent_active(**fields)
    second, created_again = store.create_if_no_recent_active(**{**fields, "source_lang": "en"})
    assert created is True
    assert created_again is False
    assert second.id == first.id


def test_create_if_no_recent_active_allows_terminal_task(store):
    first = _create(store)
    store.update(first.id, status="SUCCESS")
    second, created = store.create_if_no_recent_active(
        url=first.url, source_lang="auto", target_lang="zh-CN", mode="mono",
        burn="hard", model="small", engine="deepseek",
    )
    assert created is True
    assert second.id != first.id
