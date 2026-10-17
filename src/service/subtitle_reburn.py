"""Run one saved render slot and publish its output with rollback protection."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from src.config import output_video_filename, settings, task_dir
from src.config.config import settings_scope
from src.plugin_system import PluginCancelled, PluginError, SubtitleRenderRequest
from src.plugins import get_plugin_manager
from src.service.asset_resolver import AssetResolver, ResourceError
from src.service.orchestrator import (
    is_cancelled_signal,
    register_cancellation_signal,
    unregister_cancellation_signal,
)
from src.service.plugin_plan import parse_plugin_plan
from src.service.task_operations import get_task_operation_lock
from src.store import TaskStore
from src.store.task_store import TaskRecord

logger = logging.getLogger(__name__)
TERMINAL_STATUSES = ("SUCCESS", "FAILED", "CANCELLED")


class SubtitleOperationError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def require_terminal_task(store: TaskStore, task_id: str) -> TaskRecord:
    record = store.get(task_id)
    if record is None:
        raise SubtitleOperationError("任务不存在", status_code=404)
    if record.status not in TERMINAL_STATUSES or record.is_cancelling:
        raise SubtitleOperationError("仅已完成、失败或已取消的任务可编辑或重新烧录字幕，请稍后重试")
    return record


def restore_operation(store: TaskStore, record: TaskRecord, token: str) -> TaskRecord | None:
    """Restore only fields changed by the claim, without stealing another owner."""
    return store.finish_operation(
        record.id, token, status=record.status,
        current_step=record.current_step, progress=record.progress,
    )


def _render_slot(record: TaskRecord, mode: str) -> dict:
    if record.plugin_plan_json is None:
        return {
            "plugin_id": "builtin.ffmpeg_render",
            "implementation_version": "1",
            "config": {"mode": mode},
        }
    try:
        plan = parse_plugin_plan(record.plugin_plan_json)
        slot = plan["slots"]["render.subtitle"]
        if slot["config"].get("mode") not in {"hard", "soft"}:
            raise ValueError("render mode is missing or invalid")
    except (ValueError, TypeError, KeyError) as exc:
        raise SubtitleOperationError(
            "任务插件计划无效或缺少 render.subtitle 插槽，无法重新烧录",
            status_code=400,
        ) from exc
    return {**slot, "config": {**slot["config"], "mode": mode}}


def _business_snapshot(record: TaskRecord) -> dict:
    if record.runtime_config_json is None:
        return settings.capture_business_snapshot()
    try:
        snapshot = json.loads(record.runtime_config_json)
        business = snapshot["business"]
        if not isinstance(business, dict):
            raise ValueError("invalid business snapshot")
        for name in ("ffmpeg_bin", "ffprobe_bin"):
            if name in business and (
                not isinstance(business[name], str) or not business[name].strip()
            ):
                raise ValueError("invalid FFmpeg configuration")
        return business
    except (ValueError, KeyError, TypeError) as exc:
        raise SubtitleOperationError("任务运行配置快照无效，无法重新烧录", status_code=400) from exc


class _ReburnContext:
    def __init__(self, store: TaskStore, record: TaskRecord, token: str, directory: Path, plugin_id: str):
        self.task_id = record.id
        self.invocation_id = uuid.uuid4().hex
        self.task_dir = directory
        self.purpose = "render.subtitle"
        self._store = store
        self._token = token
        self._plugin_id = plugin_id

    def check_cancelled(self) -> None:
        current = self._store.get(self.task_id)
        if (
            current is None or current.status != "BURNING"
            or current.current_step != self._token or current.is_cancelling
            or is_cancelled_signal(self.task_id)
        ):
            raise PluginCancelled("字幕重新烧录已取消或任务状态已改变")

    def report_progress(self, percent: float | None) -> None:
        self.check_cancelled()

    def register_process(self, proc) -> None:
        from src.service.runner import register_process
        self.check_cancelled()
        register_process(self.task_id, proc)

    def unregister_process(self, proc) -> None:
        from src.service.runner import unregister_process
        unregister_process(self.task_id, proc)

    def resolve_credential(self, ref: str) -> str:
        # The v1 render slot has only a mode; it authorizes no credential references.
        raise PluginError(
            "credential_missing", "render", self._plugin_id,
            "render slot does not authorize a credential reference",
        )


_JOURNAL_NAME = ".subtitle-publication.json"
_TOKEN = re.compile(r"(?:reburn|subtitle_edit):[0-9a-f]{32}")
_BACKUP_NAME = re.compile(r"\.reburn-backup-[0-9a-f]{32}")
_SUBTITLE_NAME = re.compile(r"(?:original|translated)(?:\.[A-Za-z0-9._-]{1,16})?\.srt")


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restore_files(backups: dict[Path, Path | None]) -> None:
    # Keep every backup until all restores finish; a crash midway is retryable.
    for path, backup in backups.items():
        if backup is None:
            path.unlink(missing_ok=True)
            continue
        temporary = path.parent / f".subtitle-restore-{uuid.uuid4().hex}"
        try:
            try:
                os.link(backup, temporary)
            except OSError:
                shutil.copy2(backup, temporary)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    if backups:
        _sync_directory(next(iter(backups)).parent)


def _read_publication_journal(task_id: str, directory: Path, expected_token: str | None) -> dict | None:
    path = directory / _JOURNAL_NAME
    if path.is_symlink():
        raise SubtitleOperationError("字幕发布恢复日志不是安全的普通文件")
    if not path.exists():
        return None
    try:
        if not path.is_file() or path.stat().st_size > 65536:
            raise ValueError("invalid journal file")
        journal = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(journal, dict) or set(journal) != {
            "schema_version", "task_id", "token", "previous", "files", "temporary_names",
        }:
            raise ValueError("invalid journal schema")
        token = journal["token"]
        if (
            type(journal["schema_version"]) is not int or journal["schema_version"] != 1
            or journal["task_id"] != task_id or not isinstance(token, str)
            or _TOKEN.fullmatch(token) is None
            or (expected_token is not None and token != expected_token)
        ):
            raise ValueError("invalid journal identity")
        previous = journal["previous"]
        if (
            not isinstance(previous, dict) or set(previous) != {"status", "progress", "current_step"}
            or previous["status"] not in TERMINAL_STATUSES
            or type(previous["progress"]) is not int or not 0 <= previous["progress"] <= 100
            or (previous["current_step"] is not None and not isinstance(previous["current_step"], str))
        ):
            raise ValueError("invalid previous task state")
        files = journal["files"]
        if not isinstance(files, dict) or not 1 <= len(files) <= 4:
            raise ValueError("invalid publication targets")
        reburn = token.startswith("reburn:")
        render_names = {"output_hard.mp4", "output_soft.mp4"}
        metadata_names = {f"{purpose}.plugin-meta.json" for purpose in ("render.subtitle", "tts", "render.dub_mux")}
        if reburn and (len(set(files) & render_names) != 1 or set(files) - render_names - metadata_names):
            raise ValueError("invalid render targets")
        if not reburn and (len(files) != 1 or _SUBTITLE_NAME.fullmatch(next(iter(files))) is None):
            raise ValueError("invalid subtitle target")
        seen_backups = set()
        for name, backup in files.items():
            target = directory / name
            if target.is_symlink() or target.resolve().parent != directory or (target.exists() and not target.is_file()):
                raise ValueError("unsafe publication target")
            if backup is None:
                continue
            if not isinstance(backup, str) or _BACKUP_NAME.fullmatch(backup) is None or backup in seen_backups:
                raise ValueError("invalid backup name")
            seen_backups.add(backup)
            backup_path = directory / backup
            if backup_path.is_symlink() or not backup_path.is_file() or backup_path.resolve().parent != directory:
                raise ValueError("missing or unsafe backup")
        temporary_names = journal["temporary_names"]
        if not isinstance(temporary_names, list) or len(temporary_names) > 1:
            raise ValueError("invalid temporary targets")
        pattern = r"\.reburn-[0-9a-f]{32}\.mp4" if reburn else r"\.subtitle-edit-[A-Za-z0-9_-]+\.srt"
        for name in temporary_names:
            if not isinstance(name, str) or re.fullmatch(pattern, name) is None:
                raise ValueError("invalid temporary name")
            temporary = directory / name
            if temporary.is_symlink() or (temporary.exists() and not temporary.is_file()):
                raise ValueError("unsafe temporary target")
        return journal
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise SubtitleOperationError("字幕发布恢复日志或备份损坏，已保留文件供检查") from exc


def recover_subtitle_publication(
    task_id: str, directory: Path | None = None, *, expected_token: str | None = None,
) -> dict | None:
    """Restore an interrupted claim; return its previous state for the DB owner.

    The caller must restrict recovery to a still-claimed BURNING task. A completed
    SUCCESS row may have committed immediately before journal cleanup.
    """
    with get_task_operation_lock(task_id):
        root = (directory if directory is not None else task_dir(task_id)).resolve()
        journal = _read_publication_journal(task_id, root, expected_token)
        if journal is None:
            return None
        backups = {root / name: root / backup if backup is not None else None for name, backup in journal["files"].items()}
        try:
            _restore_files(backups)
            for name in journal["temporary_names"]:
                (root / name).unlink(missing_ok=True)
            (root / _JOURNAL_NAME).unlink()
            _sync_directory(root)
        except OSError as exc:
            raise SubtitleOperationError("字幕发布恢复失败，已保留日志与备份供检查") from exc
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)
        return {"token": journal["token"], "previous": journal["previous"]}


def discard_subtitle_publication(task_id: str, directory: Path | None = None) -> bool:
    """Clean a committed terminal task's validated journal without rolling back."""
    with get_task_operation_lock(task_id):
        root = (directory if directory is not None else task_dir(task_id)).resolve()
        journal = _read_publication_journal(task_id, root, None)
        if journal is None:
            return False
        for name in journal["temporary_names"]:
            (root / name).unlink(missing_ok=True)
        (root / _JOURNAL_NAME).unlink()
        _sync_directory(root)
        for backup in journal["files"].values():
            if backup is not None:
                (root / backup).unlink(missing_ok=True)
        return True


class SubtitlePublication:
    """Keep recoverable old targets until filesystem publication and DB commit."""

    def __init__(
        self, directory: Path, paths: tuple[Path, ...], *, record: TaskRecord,
        token: str, temporary_names: tuple[str, ...] = (),
    ) -> None:
        self._directory = directory
        self._journal = directory / _JOURNAL_NAME
        self._journal_written = False
        self._backups: dict[Path, Path | None] = {}
        if self._journal.exists() or self._journal.is_symlink():
            raise SubtitleOperationError("存在尚未恢复的字幕发布日志，请先恢复任务")
        try:
            for path in paths:
                if path.parent.resolve() != directory or path.is_symlink():
                    raise SubtitleOperationError("成品或缓存路径不安全", status_code=400)
                if path.exists() and not path.is_file():
                    raise SubtitleOperationError("成品或缓存路径不是普通文件", status_code=400)
                if path.exists():
                    backup = directory / f".reburn-backup-{uuid.uuid4().hex}"
                    self._backups[path] = backup
                    try:
                        os.link(path, backup)
                    except OSError:
                        shutil.copy2(path, backup)
                else:
                    self._backups[path] = None
            journal = {
                "schema_version": 1, "task_id": record.id, "token": token,
                "previous": {"status": record.status, "progress": record.progress, "current_step": record.current_step},
                "files": {path.name: backup.name if backup is not None else None for path, backup in self._backups.items()},
                "temporary_names": list(temporary_names),
            }
            descriptor, name = tempfile.mkstemp(prefix=".subtitle-publication-", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(journal, stream, ensure_ascii=False, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self._journal)
                self._journal_written = True
                _sync_directory(directory)
            finally:
                Path(name).unlink(missing_ok=True)
        except BaseException:
            self.discard_backups()
            raise

    def restore(self) -> None:
        _restore_files(self._backups)
        self.discard_backups()

    def discard_backups(self) -> None:
        if self._journal_written:
            try:
                self._journal.unlink(missing_ok=True)
                _sync_directory(self._directory)
                self._journal_written = False
            except OSError:
                logger.warning("清理字幕发布日志失败，保留备份: file=%s", self._journal.name, exc_info=True)
                return
        for backup in self._backups.values():
            if backup is not None:
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    logger.warning("清理重烧备份失败: file=%s", backup.name, exc_info=True)


def reburn_task(store: TaskStore, task_id: str, mode: str | None = None) -> Path:
    """Synchronously run render.subtitle, without migrating or scheduling a pipeline."""
    require_terminal_task(store, task_id)
    with get_task_operation_lock(task_id):
        require_terminal_task(store, task_id)
        directory = task_dir(task_id).resolve()
        if not directory.is_dir():
            raise SubtitleOperationError("任务目录尚未生成")
        try:
            video = AssetResolver.require_source(task_id)
            subtitle = AssetResolver.require_translated_srt(task_id)
        except ResourceError as exc:
            raise SubtitleOperationError(str(exc)) from exc
        claimed = store.claim_operation(task_id, operation="reburn")
        if claimed is None:
            raise SubtitleOperationError("任务正在处理，请稍后重试")
        previous, token = claimed
        temporary = directory / f".reburn-{uuid.uuid4().hex}.mp4"
        publication = None
        register_cancellation_signal(task_id)
        try:
            # The preceding terminal claim excludes cross-worker publication while
            # cleaning a journal left after a prior successful DB commit.
            discard_subtitle_publication(task_id, directory)
            # The atomic claim's record is authoritative if another worker edited
            # the task between the initial read and claim.
            selected_mode = previous.burn if mode is None else mode
            if selected_mode not in ("hard", "soft"):
                raise SubtitleOperationError("非法的 burn 模式", status_code=400)
            slot = _render_slot(previous, selected_mode)
            business = _business_snapshot(previous)
            output = directory / output_video_filename(selected_mode)
            context = _ReburnContext(store, previous, token, directory, slot["plugin_id"])
            # Reserve the host-owned filename without ever truncating an existing file.
            with temporary.open("xb"):
                pass
            request = SubtitleRenderRequest(
                stage="render", operation="subtitle", purpose="render.subtitle",
                source_video=video.absolute(), translated_srt=subtitle.absolute(),
                output_video=temporary, mode=selected_mode,
            )
            with settings_scope(business):
                get_plugin_manager().run(
                    slot["plugin_id"], slot["implementation_version"],
                    request, slot["config"], context,
                )
            context.check_cancelled()
            metadata = AssetResolver.plugin_metadata(
                task_id=task_id, stage="render", operation="subtitle", purpose="render.subtitle",
                plugin_id=slot["plugin_id"], implementation_version=slot["implementation_version"],
                config=slot["config"],
                inputs={"source_video": video, "translated_srt": subtitle},
                output_names=(output.name,),
                upstream_purposes={
                    name: purpose for name, purpose in (
                        ("source_video", "download" if previous.source_type == "url" else ""),
                        ("translated_srt", "translate"),
                    )
                    if purpose and (directory / f"{purpose}.plugin-meta.json").is_file()
                },
            )
            publication = SubtitlePublication(directory, (
                output,
                *(AssetResolver.plugin_meta_path(task_id, purpose) for purpose in (
                    "render.subtitle", "tts", "render.dub_mux",
                )),
            ), record=previous, token=token, temporary_names=(temporary.name,))
            os.replace(temporary, output)
            _sync_directory(directory)
            AssetResolver.write_plugin_cache_metadata(task_id, metadata)
            for purpose in ("tts", "render.dub_mux"):
                AssetResolver.invalidate_plugin_cache(task_id, purpose)
            context.check_cancelled()
            finished = store.finish_operation(
                task_id, token, status="SUCCESS", progress=100, current_step=None,
                burn=selected_mode, output_video=output.name, resource_status="AVAILABLE",
                error=None, error_code=None, downgrade_reason=None, downgrade_errno=None,
                downgraded_at=None, output_dubbed_video=None,
                tts_status="PENDING" if previous.tts_enabled else "DISABLED", tts_error=None,
            )
            if finished is None:
                raise PluginCancelled("字幕重新烧录提交时任务状态已改变")
            publication.discard_backups()
            return output
        except BaseException:
            if publication is not None:
                # A failed filesystem rollback keeps the claim and journal for
                # startup recovery; restoring its DB state would hide that work.
                publication.restore()
            try:
                restore_operation(store, previous, token)
            except Exception:
                logger.exception("恢复字幕重烧任务状态失败: task=%s", task_id)
            raise
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("清理重烧临时文件失败: task=%s", task_id, exc_info=True)
            unregister_cancellation_signal(task_id)
