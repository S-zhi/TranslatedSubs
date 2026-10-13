"""字幕编辑相关路由（业务域：subtitle_editor）。

提供：
  - GET   /api/tasks/{id}/subtitles       解析 original / translated 为结构化 JSON
  - PUT   /api/tasks/{id}/subtitles       保存编辑后的 SRT（覆盖或写入版本文件）
  - POST  /api/tasks/{id}/subtitles/burn  基于当前 SRT 重新烧录成品视频

仅复用 src.core.srt_utils 和 src.core.subtitle_burner，不新建存储 helper。
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from pathlib import Path
from contextlib import contextmanager
from typing import Generator, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from src.config import (
    ORIGINAL_SRT,
    OUTPUT_VIDEO,
    OUTPUT_VIDEO_NAMES,
    SOURCE_VIDEO_STEM,
    TRANSLATED_SRT,
    task_dir,
)
from src.core.srt_utils import Subtitle, parse_srt, read_srt_content, write_srt
from src.core.subtitle_burner import BurnError, burn_subtitles
from src.handler.deps import get_store, require_api_token
from src.store import TaskStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["subtitle-editor"])

# 编辑保存的并发写：每个 task_id 一把锁，避免边写边读竞态
class _LockEntry:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ref_count = 0


_write_locks: dict[str, _LockEntry] = {}
_write_locks_guard = threading.Lock()
_MAX_WRITE_LOCKS = 1000


def _prune_unlocked_locks_locked() -> None:
    """清理无活性绑定的锁条目（ref_count <= 0）。必须在持有 _write_locks_guard 时调用。"""
    to_remove = [tid for tid, entry in _write_locks.items() if entry.ref_count <= 0]
    for tid in to_remove:
        _write_locks.pop(tid, None)


@contextmanager
def task_write_lock(task_id: str) -> Generator[None, None, None]:
    """RAII 风格任务写锁上下文管理器，安全维护 ref_count 并在无活跃引用时自动清除。"""
    with _write_locks_guard:
        entry = _write_locks.get(task_id)
        if entry is None:
            if len(_write_locks) >= _MAX_WRITE_LOCKS:
                _prune_unlocked_locks_locked()
            entry = _LockEntry()
            _write_locks[task_id] = entry
        entry.ref_count += 1

    try:
        with entry.lock:
            yield
    finally:
        with _write_locks_guard:
            entry.ref_count -= 1
            if entry.ref_count <= 0:
                _write_locks.pop(task_id, None)


def _lock_for(task_id: str) -> threading.Lock:
    """为兼容性保留，获取或创建任务的底层 Lock 对象。"""
    with _write_locks_guard:
        entry = _write_locks.get(task_id)
        if entry is None:
            if len(_write_locks) >= _MAX_WRITE_LOCKS:
                _prune_unlocked_locks_locked()
            entry = _LockEntry()
            _write_locks[task_id] = entry
        return entry.lock


def release_lock(task_id: str) -> None:
    """清理进程内锁字典 _write_locks 中的任务锁（若无活跃持有者），防止内存泄漏。"""
    with _write_locks_guard:
        entry = _write_locks.get(task_id)
        if entry is not None and entry.ref_count <= 0:
            _write_locks.pop(task_id, None)


def _require(store: TaskStore, task_id: str):
    rec = store.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return rec


_Locale = Literal["original", "translated"]
_LOCALE_TO_FILENAME = {"original": ORIGINAL_SRT, "translated": TRANSLATED_SRT}


# ---------- Pydantic 模型 ----------

class SubtitleEntry(BaseModel):
    """前端编辑用单条字幕。id 用于让前端在编辑期稳定引用某一行。"""

    id: str = Field(default_factory=lambda: "sub_" + uuid.uuid4().hex[:8])
    index: int = Field(ge=1)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, v: float, info):
        start = info.data.get("start")
        if start is not None and v < start:
            # 允许前端边界场景：end == start 或 end < start 不合法，直接 422
            raise ValueError("end 必须不小于 start")
        return v


class SubtitleDocument(BaseModel):
    """PUT 请求体：一条 locale 的全部字幕。"""

    locale: _Locale
    entries: List[SubtitleEntry] = Field(min_length=1)
    # 可选：写入版本文件而不是覆盖。例 "v2" -> original.v2.srt
    version: Optional[str] = None

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,16}", v):
            raise ValueError("version 仅支持字母/数字/点/下划线/连字符，长度 1-16")
        return v


class SubtitleUpdateOut(BaseModel):
    ok: bool = True
    taskId: str
    locale: _Locale
    path: str  # 落盘的文件名（相对任务目录）
    count: int


class SubtitlesOut(BaseModel):
    taskId: str
    title: Optional[str] = None
    burn: str
    hasOriginal: bool
    hasTranslated: bool
    original: List[SubtitleEntry] = Field(default_factory=list)
    translated: List[SubtitleEntry] = Field(default_factory=list)


class ReburnOut(BaseModel):
    ok: bool = True
    taskId: str
    mode: str
    outputPath: Optional[str] = None  # 同步路径；后台模式下可能为 None


# ---------- 辅助 ----------

def _read_locale_entries(d: Path, locale: _Locale) -> tuple[bool, List[SubtitleEntry]]:
    """读取任务目录对应 locale 的 SRT，返回 (是否存在, 结构化条目)。"""
    filename = _LOCALE_TO_FILENAME[locale]
    path = d / filename
    if not path.exists():
        return False, []
    subs = parse_srt(path)
    return True, [_to_entry(s, i) for i, s in enumerate(subs)]


def _to_entry(s: Subtitle, fallback_index: int) -> SubtitleEntry:
    return SubtitleEntry(
        id="sub_" + uuid.uuid4().hex[:8],
        index=s.index if s.index > 0 else fallback_index + 1,
        start=round(float(s.start), 3),
        end=round(float(s.end), 3),
        text=s.text,
    )


def _resolve_target_path(d: Path, locale: _Locale, version: Optional[str]) -> tuple[Path, str]:
    """根据 locale + version 决定最终落盘路径。返回 (绝对路径, 相对文件名)。"""
    base = _LOCALE_TO_FILENAME[locale]
    if version is None:
        return d / base, base
    stem = Path(base).stem
    suffix = Path(base).suffix
    fname = f"{stem}.{version}{suffix}"
    return d / fname, fname


# ---------- 路由 ----------

@router.get("/{task_id}/subtitles", response_model=SubtitlesOut, dependencies=[Depends(require_api_token)])
def get_subtitles(task_id: str, store: TaskStore = Depends(get_store)) -> SubtitlesOut:
    """读取任务当前 ORIGINAL_SRT / TRANSLATED_SRT，解析为结构化 JSON 供前端编辑。

    没有生成的 locale 返回空数组 + hasX=False，前端据此隐藏对应面板。
    """
    rec = _require(store, task_id)
    d = task_dir(task_id)
    has_o, originals = _read_locale_entries(d, "original")
    has_t, translateds = _read_locale_entries(d, "translated")

    if not has_o and not has_t:
        raise HTTPException(
            status_code=409,
            detail="该任务尚未生成字幕，请先运行一次完整流水线",
        )

    return SubtitlesOut(
        taskId=task_id,
        title=rec.title,
        burn=rec.burn,
        hasOriginal=has_o,
        hasTranslated=has_t,
        original=originals,
        translated=translateds,
    )


@router.put("/{task_id}/subtitles", response_model=SubtitleUpdateOut, dependencies=[Depends(require_api_token)])
def save_subtitles(
    task_id: str,
    body: SubtitleDocument,
    store: TaskStore = Depends(get_store),
) -> SubtitleUpdateOut:
    """把编辑后的字幕写回 SRT。

    默认覆盖 `original.srt` / `translated.srt`；
    当 `version` 给出时（如 "v2"）写入 `original.v2.srt`，原文件保留。
    """
    rec = _require(store, task_id)
    d = task_dir(task_id)
    if not d.exists():
        raise HTTPException(status_code=409, detail="任务目录尚未生成")

    with task_write_lock(task_id):
        target_path, rel_name = _resolve_target_path(d, body.locale, body.version)

        # 校验、读取现有编码与写回必须处于同一临界区，避免并发保存时基于过期文件状态写入。
        if body.version is not None and rel_name in {ORIGINAL_SRT, TRANSLATED_SRT, *OUTPUT_VIDEO_NAMES}:
            raise HTTPException(status_code=400, detail="非法的版本文件名")

        sorted_entries = sorted(body.entries, key=lambda e: (e.start, e.index))
        subs = [
            Subtitle(
                index=e.index,
                start=float(e.start),
                end=float(e.end),
                text=e.text,
            )
            for e in sorted_entries
        ]

        encoding = "utf-8-sig"
        if target_path.exists():
            try:
                _, encoding = read_srt_content(target_path)
            except Exception:
                pass

        # 版本文件：若已存在则直接覆盖（用户主动保存即确认）；保留原始编码或默认 utf-8-sig
        write_srt(subs, target_path, encoding=encoding)

    logger.info(
        "字幕已保存: task=%s locale=%s version=%s count=%d -> %s",
        task_id, body.locale, body.version or "-", len(subs), rel_name,
    )

    return SubtitleUpdateOut(
        ok=True,
        taskId=task_id,
        locale=body.locale,
        path=rel_name,
        count=len(subs),
    )


@router.post("/{task_id}/subtitles/burn", response_model=ReburnOut, dependencies=[Depends(require_api_token)])
def reburn_subtitles(
    task_id: str,
    body: Optional[dict] = None,
    store: TaskStore = Depends(get_store),
) -> ReburnOut:
    """基于任务当前 TRANSLATED_SRT 重新烧录 output.mp4，复用 burn_subtitles。

    请求体可选：
      { "mode": "hard" | "soft" }   不传则按任务原始 burn 模式
    """
    rec = _require(store, task_id)
    d = task_dir(task_id)
    if not d.exists():
        raise HTTPException(status_code=409, detail="任务目录尚未生成")

    srt_path = d / TRANSLATED_SRT
    if not srt_path.exists():
        raise HTTPException(
            status_code=409,
            detail="译文字幕尚未生成，无法烧录。请先完成翻译或手动上传字幕。",
        )

    # 源视频：source.* 任意扩展名
    sources = sorted(d.glob(f"{SOURCE_VIDEO_STEM}.*"))
    if not sources:
        raise HTTPException(status_code=409, detail="找不到源视频 source.*")
    video_path = sources[0]

    mode = (body or {}).get("mode") or rec.burn
    if mode not in ("hard", "soft"):
        raise HTTPException(status_code=400, detail=f"非法的 burn 模式: {mode}")

    try:
        result = burn_subtitles(video_path, srt_path, task_id, mode=mode)
    except BurnError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    out_name = result.output_path.name
    # 把 DB 里的 output_video 同步刷新，方便前端通过 /download 立即拿到新版本
    store.update(task_id, output_video=result.output_path.name)
    return ReburnOut(ok=True, taskId=task_id, mode=mode, outputPath=out_name)
