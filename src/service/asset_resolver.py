from __future__ import annotations

import logging
import os
import hashlib
import json
import tempfile
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
    _PLUGIN_META_SCHEMA = 1

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def plugin_meta_path(task_id: str, purpose: str) -> Path:
        return task_dir(task_id) / f"{purpose}.plugin-meta.json"

    @classmethod
    def plugin_input_sha256(
        cls,
        inputs: dict[str, object],
        *,
        upstream_purposes: Optional[dict[str, str]] = None,
    ) -> str:
        """Hash input content and producer provenance without storing input values."""
        upstream_purposes = upstream_purposes or {}
        entries: dict[str, object] = {}
        for name, value in sorted(inputs.items()):
            if isinstance(value, Path):
                if not value.is_file() or value.stat().st_size <= 0:
                    raise ResourceError("插件输入文件缺失或为空", ResourceState.DELETED)
                item: dict[str, object] = {"sha256": cls._sha256_file(value)}
                producer = upstream_purposes.get(name)
                if producer:
                    # task_dir may be customized, so derive the metadata beside the input.
                    metadata = value.parent / f"{producer}.plugin-meta.json"
                    if not metadata.is_file():
                        raise ResourceError("插件输入来源记录缺失", ResourceState.DELETED)
                    item["producer_sha256"] = cls._sha256_file(metadata)
                entries[name] = item
            else:
                entries[name] = {"value_sha256": hashlib.sha256(
                    json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()}
        payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def plugin_cache_matches(
        cls,
        task_id: str,
        *,
        stage: str,
        operation: str,
        purpose: str,
        plugin_id: str,
        implementation_version: str,
        config: dict,
        inputs: dict[str, object],
        output_names: tuple[str, ...],
        upstream_purposes: Optional[dict[str, str]] = None,
        legacy_cache: bool = False,
        legacy_compatible: bool = False,
    ) -> bool:
        """Return whether a purpose cache matches the frozen invocation identity."""
        directory = task_dir(task_id)
        outputs = tuple(directory / name for name in output_names)
        if not all(cls.check_file_state(path) == ResourceState.AVAILABLE for path in outputs):
            cls.invalidate_plugin_cache(task_id, purpose, output_names)
            return False
        expected = cls.plugin_metadata(
            task_id=task_id, stage=stage, operation=operation, purpose=purpose,
            plugin_id=plugin_id, implementation_version=implementation_version,
            config=config, inputs=inputs, output_names=output_names,
            upstream_purposes=upstream_purposes,
        )
        metadata_path = cls.plugin_meta_path(task_id, purpose)
        try:
            actual = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            actual = None
        if actual == expected:
            return True
        if actual is None and legacy_cache and legacy_compatible:
            cls.write_plugin_cache_metadata(task_id, expected)
            return True
        cls.invalidate_plugin_cache(task_id, purpose, output_names)
        return False

    @classmethod
    def plugin_metadata(
        cls,
        *,
        task_id: str,
        stage: str,
        operation: str,
        purpose: str,
        plugin_id: str,
        implementation_version: str,
        config: dict,
        inputs: dict[str, object],
        output_names: tuple[str, ...],
        upstream_purposes: Optional[dict[str, str]] = None,
    ) -> dict:
        config_json = json.dumps(dict(config), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "schema_version": cls._PLUGIN_META_SCHEMA,
            "stage": stage,
            "operation": operation,
            "purpose": purpose,
            "plugin_id": plugin_id,
            "implementation_version": implementation_version,
            "config_sha256": hashlib.sha256(config_json.encode("utf-8")).hexdigest(),
            "input_sha256": cls.plugin_input_sha256(inputs, upstream_purposes=upstream_purposes),
            "output_names": list(output_names),
        }

    @classmethod
    def write_plugin_cache_metadata(cls, task_id: str, metadata: dict) -> None:
        directory = task_dir(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = cls.plugin_meta_path(task_id, str(metadata["purpose"]))
        encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @classmethod
    def invalidate_plugin_cache(
        cls, task_id: str, purpose: str, output_names: tuple[str, ...] = ()
    ) -> None:
        directory = task_dir(task_id)
        (directory / f"{purpose}.plugin-meta.json").unlink(missing_ok=True)
        for name in output_names:
            (directory / name).unlink(missing_ok=True)
        if purpose in {"separate.asr", "separate.dubbing"}:
            # The Demucs core sidecar is a second-level cache; remove it whenever
            # the host invalidates separation provenance, even if audio bytes match.
            suffix = "asr" if purpose == "separate.asr" else "dubbing"
            (directory / f"separation.{suffix}.meta.json").unlink(missing_ok=True)

    @classmethod
    def cleanup_unpublished_invocation(
        cls, task_id: str, paths: set[Path], purposes: set[str]
    ) -> None:
        """Remove only artifacts owned by a failed or cancelled invocation."""
        for path in paths:
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("清理未发布插件产物失败: task=%s, err=%s", task_id, type(exc).__name__)
        directory = task_dir(task_id)
        for purpose in purposes:
            (directory / f"{purpose}.plugin-meta.json").unlink(missing_ok=True)

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
