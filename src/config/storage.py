"""存储路径策略。

每个任务一个目录：data/{task_id}/，所有中间产物与成品都落在里面。
好处：断点续跑只需检查文件是否存在；清理任务只需删一个目录；
DB 只存相对信息，不依赖绝对路径历史。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO, Iterator

from .config import settings

# 流水线各阶段的标准产物文件名（stem，不含扩展名的固定基名）
SOURCE_VIDEO_STEM = "source"      # 下载的原始视频 source.mp4
AUDIO_FILENAME = "audio.wav"      # 提取的音频
ORIGINAL_SRT = "original.srt"     # 识别出的原文字幕
TRANSLATED_SRT = "translated.srt"  # 翻译后的字幕
OUTPUT_VIDEO = "output.mp4"       # 旧版烧录成品（仅用于读取兼容）
OUTPUT_VIDEO_HARD = "output_hard.mp4"
OUTPUT_VIDEO_SOFT = "output_soft.mp4"
OUTPUT_VIDEO_NAMES = (OUTPUT_VIDEO_HARD, OUTPUT_VIDEO_SOFT, OUTPUT_VIDEO)


def output_video_filename(mode: str) -> str:
    """Return the mode-specific output filename."""
    if mode == "hard":
        return OUTPUT_VIDEO_HARD
    if mode == "soft":
        return OUTPUT_VIDEO_SOFT
    raise ValueError(f"unknown burn mode: {mode}")


def _safe_name(name: str) -> str:
    """Normalize an artifact name without allowing a path to escape its task."""
    value = str(name or "")
    if not value or value in {".", ".."}:
        raise ValueError("artifact name is required")
    # PureWindowsPath matters when a Windows-produced value is read on POSIX.
    if PurePosixPath(value).name != value or PureWindowsPath(value).name != value:
        raise ValueError("artifact name must be a single file name")
    return value


def artifact_name(value: str | Path) -> str:
    """Return the portable filename component of a local or Windows path."""
    raw = str(value)
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    name = windows.name if windows.name != raw else posix.name
    return _safe_name(name)


@dataclass(frozen=True)
class ArtifactRef:
    """Portable reference to a task artifact.

    Only ``task_id`` and ``name`` cross service boundaries.  A concrete path is
    resolved by the owning store, so moving the data root or changing platform
    does not invalidate persisted references.
    """

    task_id: str
    name: str
    store: "ArtifactStore"

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _safe_name(self.name))

    @property
    def path(self) -> Path:
        return self.store.task_dir(self.task_id) / self.name

    def exists(self) -> bool:
        return self.path.is_file()

    def open(self, mode: str = "rb", **kwargs) -> BinaryIO:
        return self.path.open(mode, **kwargs)

    def delete(self, *, missing_ok: bool = True) -> None:
        self.path.unlink(missing_ok=missing_ok)


class ArtifactStore:
    """Filesystem adapter for task resources.

    The rest of the application depends on this small object instead of
    concatenating ``data_dir / task_id / filename`` throughout the codebase.
    A future object-store adapter can implement the same reference contract.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser()

    def task_dir(self, task_id: str, *, create: bool = False) -> Path:
        task = _safe_name(task_id)
        directory = self.root / task
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    def artifact(self, task_id: str, name: str) -> ArtifactRef:
        return ArtifactRef(task_id=task_id, name=name, store=self)

    def iter_task_files(self, task_id: str) -> Iterator[Path]:
        directory = self.task_dir(task_id)
        if not directory.is_dir():
            return iter(())
        return iter(directory.iterdir())


def artifact_store(root: Path | str | None = None) -> ArtifactStore:
    """Create a store for an explicit root, or the configured default root."""
    return ArtifactStore(settings.data_dir if root is None else root)


def task_dir(task_id: str) -> Path:
    """返回任务目录路径（不保证存在）。"""
    return artifact_store().task_dir(task_id)


def ensure_task_dir(task_id: str) -> Path:
    """返回任务目录路径，并确保已创建。"""
    return artifact_store().task_dir(task_id, create=True)


def artifacts_present(task_id: str, *, data_dir: Path, need_subtitle: bool) -> bool:
    """判断一个成功任务的产物是否还都在磁盘上。

    - 完整流水线（need_subtitle=True）：output.mp4 与 translated.srt 都必须在。
    - 仅下载模式（need_subtitle=False）：source.* 至少存在一个。

    与 task_dir 一样不抛异常；目录不存在视为资源丢失。
    该函数用于服务启动 / 下载兜底场景：发现资源缺失就把状态降级为 MISSING，
    避免给用户暴露已经失效的下载链接。
    """
    d = ArtifactStore(data_dir).task_dir(task_id)
    if not d.is_dir():
        return False
    if need_subtitle:
        return any((d / name).is_file() for name in OUTPUT_VIDEO_NAMES) and (d / TRANSLATED_SRT).is_file()
    return any(d.glob(f"{SOURCE_VIDEO_STEM}.*"))
