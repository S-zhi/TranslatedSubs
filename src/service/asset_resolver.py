from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Optional, Tuple

from src.config import (
    ArtifactStore,
    AUDIO_FILENAME,
    OUTPUT_VIDEO,
    OUTPUT_VIDEO_NAMES,
    output_video_filename,
    SOURCE_VIDEO_STEM,
    TRANSLATED_SRT,
    ORIGINAL_SRT,
    artifact_store,
    task_dir,
)

logger = logging.getLogger(__name__)


class ResourceState(str, Enum):
    AVAILABLE = "AVAILABLE"
    DELETED = "DELETED"
    UNREADABLE = "UNREADABLE"


@dataclass(frozen=True)
class ArtifactStatus:
    """结果 of a non-throwing artifact preflight."""

    state: ResourceState
    address: Optional[Path]
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.state == ResourceState.AVAILABLE and self.address is not None


@dataclass(frozen=True)
class ProcessingArtifact:
    """Domain entity for one pipeline artifact.

    The entity owns identity and storage operations. Callers can preflight it
    before exposing or consuming the file, while ``path`` remains available to
    legacy libraries through ``os.fspath``.
    """

    task_id: str
    name: str
    store: ArtifactStore

    @property
    def address(self) -> Path:
        return self.store.artifact(self.task_id, self.name).path

    @property
    def path(self) -> Path:
        return self.address

    def __fspath__(self) -> str:
        return os.fspath(self.address)

    def preflight(self) -> ArtifactStatus:
        state = AssetResolver.check_file_state(self.address)
        if state == ResourceState.AVAILABLE:
            return ArtifactStatus(state, self.address)
        if state == ResourceState.DELETED:
            return ArtifactStatus(state, None, f"资源缺失: {self.name}")
        return ArtifactStatus(state, self.address, f"资源不可读: {self.name}")

    def require(self) -> Path:
        result = self.preflight()
        if not result.available:
            raise ResourceError(result.reason, result.state)
        return result.address  # type: ignore[return-value]

    def exists(self) -> bool:
        return self.preflight().available

    def open(self, mode: str = "rb", **kwargs) -> BinaryIO:
        return self.address.open(mode, **kwargs)

    def write_bytes(self, data: bytes) -> Path:
        self.address.parent.mkdir(parents=True, exist_ok=True)
        self.address.write_bytes(data)
        return self.address

    def delete(self, *, missing_ok: bool = True) -> None:
        self.address.unlink(missing_ok=missing_ok)


class ResourceError(RuntimeError):
    """资源完整性校验或依赖中断异常"""
    def __init__(self, message: str, state: ResourceState):
        super().__init__(message)
        self.state = state


class AssetResolver:
    @staticmethod
    def artifact(task_id: str, name: str, *, store: Optional[ArtifactStore] = None) -> ProcessingArtifact:
        """Build a processing artifact without touching the filesystem."""
        if store is None:
            # Keep the resolver's injected task directory as the source of
            # truth for tests and deployments that provide a custom root.
            directory = task_dir(task_id)
            store = ArtifactStore(directory.parent)
        return ProcessingArtifact(
            task_id=task_id,
            name=name,
            store=store,
        )

    @classmethod
    def preflight(
        cls, task_id: str, name: str, *, store: Optional[ArtifactStore] = None
    ) -> ArtifactStatus:
        """Validate an artifact before it is consumed or exposed."""
        return cls.artifact(task_id, name, store=store).preflight()

    @classmethod
    def preflight_source(cls, task_id: str, *, store: Optional[ArtifactStore] = None) -> ArtifactStatus:
        """Preflight the first completed source video, excluding download parts."""
        storage = store or artifact_store()
        directory = storage.task_dir(task_id)
        if not directory.is_dir():
            return ArtifactStatus(ResourceState.DELETED, None, "源视频文件缺失，资源已删除")
        candidates = sorted(
            p for p in directory.glob(f"{SOURCE_VIDEO_STEM}.*") if not p.name.endswith(".part")
        )
        if not candidates:
            return ArtifactStatus(ResourceState.DELETED, None, "源视频文件缺失，资源已删除")
        return cls.artifact(task_id, candidates[0].name, store=storage).preflight()

    @staticmethod
    def check_file_state(path: Path) -> ResourceState:
        """检查物理文件的完整性状态。"""
        if not path.exists():
            return ResourceState.DELETED
        if not path.is_file():
            return ResourceState.UNREADABLE
        try:
            if path.stat().st_size == 0:
                return ResourceState.UNREADABLE
        except OSError:
            return ResourceState.UNREADABLE

        # Read permission check
        if not os.access(path, os.R_OK):
            return ResourceState.UNREADABLE

        return ResourceState.AVAILABLE

    @classmethod
    def resolve_source(cls, task_id: str) -> Tuple[ResourceState, Optional[Path], str]:
        """解析 source.*，返回 (状态, 路径, 友好错误消息)"""
        d = task_dir(task_id)
        if not d.is_dir():
            return ResourceState.DELETED, None, "源视频文件缺失，资源已删除"

        # yt-dlp 下载中会留下 source.<ext>.part；它不是可恢复流水线的完整源视频。
        source_paths = sorted(
            p for p in d.glob(f"{SOURCE_VIDEO_STEM}.*") if not p.name.endswith(".part")
        )
        if not source_paths:
            return ResourceState.DELETED, None, "源视频文件缺失，资源已删除"

        p = source_paths[0]
        state = cls.check_file_state(p)
        if state == ResourceState.DELETED:
            return ResourceState.DELETED, None, "源视频文件缺失，资源已删除"
        elif state == ResourceState.UNREADABLE:
            return ResourceState.UNREADABLE, p, f"源视频文件损坏或不可读: {p.name}"

        return ResourceState.AVAILABLE, p, ""

    @classmethod
    def resolve_audio(cls, task_id: str) -> Tuple[ResourceState, Optional[Path], str]:
        """解析 audio.wav"""
        d = task_dir(task_id)
        p = d / AUDIO_FILENAME
        state = cls.check_file_state(p)
        if state == ResourceState.DELETED:
            return ResourceState.DELETED, None, "音频文件缺失，资源已删除"
        elif state == ResourceState.UNREADABLE:
            return ResourceState.UNREADABLE, p, f"音频文件损坏或不可读: {AUDIO_FILENAME}"
        return ResourceState.AVAILABLE, p, ""

    @classmethod
    def resolve_original_srt(cls, task_id: str) -> Tuple[ResourceState, Optional[Path], str]:
        """解析 original.srt"""
        d = task_dir(task_id)
        p = d / ORIGINAL_SRT
        state = cls.check_file_state(p)
        if state == ResourceState.DELETED:
            return ResourceState.DELETED, None, "原文字幕文件缺失，资源已删除"
        elif state == ResourceState.UNREADABLE:
            return ResourceState.UNREADABLE, p, f"原文字幕文件损坏或不可读: {ORIGINAL_SRT}"
        return ResourceState.AVAILABLE, p, ""

    @classmethod
    def resolve_translated_srt(cls, task_id: str) -> Tuple[ResourceState, Optional[Path], str]:
        """解析 translated.srt"""
        d = task_dir(task_id)
        p = d / TRANSLATED_SRT
        state = cls.check_file_state(p)
        if state == ResourceState.DELETED:
            return ResourceState.DELETED, None, "译文字幕文件缺失，资源已删除"
        elif state == ResourceState.UNREADABLE:
            return ResourceState.UNREADABLE, p, f"译文字幕文件损坏或不可读: {TRANSLATED_SRT}"
        return ResourceState.AVAILABLE, p, ""

    @classmethod
    def resolve_output_video(cls, task_id: str, mode: Optional[str] = None) -> Tuple[ResourceState, Optional[Path], str]:
        """解析模式化成品，兼容旧版 output.mp4。"""
        d = task_dir(task_id)
        unreadable = None
        names = (output_video_filename(mode), OUTPUT_VIDEO) if mode in ("hard", "soft") else OUTPUT_VIDEO_NAMES
        for name in names:
            p = d / name
            state = cls.check_file_state(p)
            if state == ResourceState.AVAILABLE:
                return ResourceState.AVAILABLE, p, ""
            if state == ResourceState.UNREADABLE:
                unreadable = p
        if unreadable is not None:
            return ResourceState.UNREADABLE, unreadable, f"成品视频文件损坏或不可读: {unreadable.name}"
        return ResourceState.DELETED, None, "成品视频文件缺失，资源已删除"

    @classmethod
    def require_source(cls, task_id: str) -> Path:
        state, path, msg = cls.resolve_source(task_id)
        if state != ResourceState.AVAILABLE or path is None:
            raise ResourceError(msg, state)
        return path

    @classmethod
    def cleanup_cancelled_artifacts(
        cls, task_id: str, current_step: Optional[str] = None, source_type: Optional[str] = None
    ) -> None:
        """清理取消任务的半截/不完整产物，防止下次重试时复用损坏或残缺的文件。"""
        d = task_dir(task_id)
        if not d.exists():
            return

        step_artifacts_map = {
            "BURNING": [*OUTPUT_VIDEO_NAMES, "output_dubbed.mp4", "dubbed.wav", "dubbed_mix.wav"],
            "SYNTHESIZING": ["tts_source.wav", "dubbed.wav", "dubbed_mix.wav", "output_dubbed.mp4"],
            "DUBBING": ["tts_source.wav", "background.wav", "background.meta.json", "vocal_stem.wav", "dubbed_mix.wav", "output_dubbed.mp4"],
            "TRANSLATING": [TRANSLATED_SRT, *OUTPUT_VIDEO_NAMES],
            "TRANSCRIBING": [ORIGINAL_SRT, TRANSLATED_SRT, *OUTPUT_VIDEO_NAMES],
            "EXTRACTING": [AUDIO_FILENAME, "audio.meta.json", "vocal.wav", "vocal.meta.json", ORIGINAL_SRT, TRANSLATED_SRT, *OUTPUT_VIDEO_NAMES],
            "DOWNLOADING": [AUDIO_FILENAME, "audio.meta.json", "vocal.wav", "vocal.meta.json", ORIGINAL_SRT, TRANSLATED_SRT, *OUTPUT_VIDEO_NAMES],
        }

        to_remove_names = set(step_artifacts_map.get(current_step, [
            AUDIO_FILENAME, ORIGINAL_SRT, TRANSLATED_SRT, *OUTPUT_VIDEO_NAMES
        ]))

        to_remove_names.add("tmp_burn.srt")
        to_remove_names.add(".vocal.wav.tmp")

        for name in to_remove_names:
            p = d / name
            if p.exists():
                try:
                    p.unlink()
                    logger.info("已清理取消任务的产物: task=%s, file=%s", task_id, name)
                except OSError as e:
                    logger.warning("清理取消任务产物失败: task=%s, file=%s, err=%s", task_id, name, e)

        for p in d.glob("*.part"):
            try:
                p.unlink()
                logger.info("已清理取消任务的 .part 文件: task=%s, file=%s", task_id, p.name)
            except OSError as e:
                logger.warning("清理 .part 文件失败: task=%s, file=%s, err=%s", task_id, p.name, e)

        # Demucs 会在该目录下生成模型名子目录；取消时整目录删除，避免
        # 下一次重试误拾取半截 stems 或长期占用大量磁盘。
        separation_dir = d / "vocal-separation"
        if separation_dir.exists():
            import shutil
            try:
                shutil.rmtree(separation_dir)
            except OSError as e:
                logger.warning("清理人声分离临时目录失败: task=%s, err=%s", task_id, e)

        if (current_step in (None, "DOWNLOADING")) and source_type != "upload":
            for p in d.glob(f"{SOURCE_VIDEO_STEM}.*"):
                if not p.name.endswith(".part"):
                    try:
                        p.unlink()
                        logger.info("已清理取消任务的 source 文件: task=%s, file=%s", task_id, p.name)
                    except OSError as e:
                        logger.warning("清理 source 文件失败: task=%s, file=%s, err=%s", task_id, p.name, e)

    @classmethod
    def require_audio(cls, task_id: str) -> Path:
        state, path, msg = cls.resolve_audio(task_id)
        if state != ResourceState.AVAILABLE or path is None:
            raise ResourceError(msg, state)
        return path

    @classmethod
    def require_original_srt(cls, task_id: str) -> Path:
        state, path, msg = cls.resolve_original_srt(task_id)
        if state != ResourceState.AVAILABLE or path is None:
            raise ResourceError(msg, state)
        return path

    @classmethod
    def require_translated_srt(cls, task_id: str) -> Path:
        state, path, msg = cls.resolve_translated_srt(task_id)
        if state != ResourceState.AVAILABLE or path is None:
            raise ResourceError(msg, state)
        return path

    @classmethod
    def require_output_video(cls, task_id: str, mode: Optional[str] = None) -> Path:
        state, path, msg = cls.resolve_output_video(task_id, mode)
        if state != ResourceState.AVAILABLE or path is None:
            raise ResourceError(msg, state)
        return path
