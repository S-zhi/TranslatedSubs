"""Serial, non-blocking preparation of the desktop development baseline.

Importing this module and constructing the coordinator do not load settings,
create model managers, inspect files, or start downloads. The desktop startup
hook calls ``start_prepare`` explicitly. Expired API snapshots trigger a local
background health check, never a download or a blocking request-thread probe.
"""

from __future__ import annotations

import copy
import importlib.util
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

WHISPER_MODEL = "base.en"
TRANSLATION_ENGINE = "local-opus-en-zh"
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_ACTIVE = {"downloading", "converting", "checking"}

# Task admission must hold this same lock until a new/retried task is recorded.
# A plain Lock permits async adapters to acquire it via to_thread and release
# it after the handler finishes. Never hold a threading.RLock across awaits.
model_mutation_lock = threading.Lock()


@contextmanager
def model_install_guard():
    """Serialize desktop publication and reject replacement during active work."""
    if os.environ.get("SUBTRANS_DESKTOP") != "1":
        yield
        return
    from src.desktop.lifecycle import active_task_count

    with model_mutation_lock:
        if active_task_count():
            raise RuntimeError("有任务正在处理，请等待任务完成后重试模型准备")
        yield


def desktop_task_defaults() -> dict[str, Any]:
    return {
        "sourceLang": "en", "targetLang": "zh-CN", "mode": "bilingual",
        "burn": "hard", "model": "local:base.en", "engine": TRANSLATION_ENGINE,
        "needSubtitle": True, "ttsEnabled": False,
    }


def _model_row(model_id: str, name: str) -> dict[str, Any]:
    return {
        "id": model_id, "name": name, "status": "not_installed",
        "phase": "not_installed", "downloaded_bytes": 0,
        "total_bytes": None, "error": None, "revision": None,
    }


def _runtime_dependencies() -> dict[str, Any]:
    """Check the local desktop pipeline without initializing a runtime cache."""
    from src.config import settings
    from src.core.ffmpeg_utils import has_subtitles_filter

    missing = [
        module for module in ("faster_whisper", "yt_dlp")
        if importlib.util.find_spec(module) is None
    ]
    for name, binary in (("ffmpeg", settings.ffmpeg_bin), ("ffprobe", settings.ffprobe_bin)):
        if not shutil.which(binary):
            missing.append(name)
    if "ffmpeg" not in missing and not has_subtitles_filter(settings.ffmpeg_bin):
        missing.append("FFmpeg subtitles/libass")
    return {"ready": not missing, "missing": missing}


def _validate_whisper(manager: Any) -> None:
    """Validate a preexisting model offline, then release its temporary runtime.

    Whisper's inventory/ready marker alone is not a load test. Do not call its
    ``check`` method: that method accepts imported HF models, not official CT2.
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(
        manager.resolve_path(WHISPER_MODEL), device="cpu", compute_type="int8",
        local_files_only=True,
    )
    del model


class DesktopModelManager:
    """Coordinate existing managers; own status metadata, never model runtimes.

    Inject managers and validation/dependency callbacks for offline tests.
    Polling timeout stops this coordinator, not an underlying download. Retry
    observes any still-active manager job before deciding to launch another.
    """

    def __init__(
        self,
        whisper_manager: Any = None,
        translation_manager: Any = None,
        *,
        official_revisions: Mapping[str, str] | None = None,
        translation_revision: str | None = None,
        endpoint: str | None = None,
        dependency_check: Callable[[], dict] | None = None,
        whisper_validator: Callable[[Any], None] | None = None,
        poll_interval: float = 0.25,
        max_poll_interval: float = 2.0,
        prepare_timeout: float = 3600.0,
        status_ttl: float = 5.0,
    ) -> None:
        if not 0 < poll_interval <= max_poll_interval or prepare_timeout <= 0:
            raise ValueError("Preparation polling intervals and timeout must be positive")
        self._whisper = whisper_manager
        self._translation = translation_manager
        self._revisions = dict(official_revisions) if official_revisions is not None else None
        self._translation_revision = translation_revision
        self._endpoint = endpoint
        self._dependency_check = dependency_check or _runtime_dependencies
        self._whisper_validator = whisper_validator or _validate_whisper
        self._poll_interval = poll_interval
        self._max_poll_interval = max_poll_interval
        self._prepare_timeout = prepare_timeout
        if status_ttl <= 0:
            raise ValueError("Status TTL must be positive")
        self._status_ttl = status_ttl
        self._checked_at = 0.0
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._snapshot = {
            "state": "not_started", "ready": False,
            "models": [
                _model_row("local:base.en", "英文转写 · Whisper base.en"),
                _model_row(TRANSLATION_ENGINE, "英译中 · OPUS"),
            ],
            "error": None,
        }

    def status(self) -> dict[str, Any]:
        """Return immediately, including while manager I/O or validation blocks."""
        with self._lock:
            if (self._snapshot["state"] == "ready"
                    and time.monotonic() - self._checked_at >= self._status_ttl):
                self._snapshot.update(state="preparing", ready=False)
                self._worker = threading.Thread(
                    target=self._refresh_health, name="desktop-model-health", daemon=True,
                )
                try:
                    self._worker.start()
                except Exception as exc:
                    self._snapshot.update(state="failed", ready=False, error=str(exc))
            return copy.deepcopy(self._snapshot)

    def start_prepare(self) -> dict[str, Any]:
        """Idempotently start preparation, or retry a previously failed run."""
        with self._lock:
            if self._snapshot["state"] == "preparing":
                return self.status()
            self._snapshot.update(state="preparing", ready=False, error=None)
            for row in self._snapshot["models"]:
                if row["status"] == "failed":
                    row.update(status="not_installed", phase="not_installed", error=None)
            self._worker = threading.Thread(
                target=self._prepare, name="desktop-model-preparation", daemon=True,
            )
            try:
                self._worker.start()
            except Exception as exc:
                self._snapshot.update(state="failed", error=str(exc))
            return self.status()

    def _refresh_health(self) -> None:
        """Read only local provenance/file stats and dependencies; never repair."""
        index = 0
        try:
            self._check_dependencies()
            for index, revision in enumerate((
                self._revisions[WHISPER_MODEL], self._translation_revision,
            )):
                row = self._read_model(index)
                if row["status"] != "ready" or row["revision"] != revision:
                    raise RuntimeError(row["error"] or "模型文件或版本已变化，请重试模型准备")
            with self._lock:
                self._checked_at = time.monotonic()
                self._snapshot.update(state="ready", ready=True, error=None)
        except Exception as exc:
            with self._lock:
                self._snapshot["models"][index].update(
                    status="failed", phase="failed", error=str(exc), revision=None,
                )
                self._snapshot.update(state="failed", ready=False, error=str(exc))

    def _initialize(self) -> tuple[str, str | None]:
        # Delay all settings/model imports until the background worker runs.
        if self._whisper is None or self._revisions is None:
            from src.service import model_manager as whisper_module

            if self._whisper is None:
                self._whisper = whisper_module.model_manager
            if self._revisions is None:
                self._revisions = dict(getattr(whisper_module, "OFFICIAL_MODEL_REVISIONS", {}))
        if self._translation is None or self._translation_revision is None:
            from src.service.translation_model_manager import MODEL_REVISION, get_translation_model_manager

            if self._translation is None:
                self._translation = get_translation_model_manager()
            if self._translation_revision is None:
                self._translation_revision = MODEL_REVISION
        revision = self._revisions.get(WHISPER_MODEL)
        if not isinstance(revision, str) or not _COMMIT.fullmatch(revision):
            raise RuntimeError("Desktop base.en requires a verified fixed OFFICIAL_MODEL_REVISIONS entry")
        if not _COMMIT.fullmatch(self._translation_revision or ""):
            raise RuntimeError("Desktop translation requires a fixed model revision")
        if getattr(self._translation, "revision", None) != self._translation_revision:
            raise RuntimeError("Translation manager revision differs from the desktop pinned revision")
        endpoint = self._endpoint
        if endpoint is None:
            endpoint = os.environ.get("SUBTRANS_DESKTOP_MODEL_ENDPOINT") or None
        if endpoint:
            try:
                parsed = urlsplit(endpoint)
                valid = (
                    parsed.scheme == "https" and parsed.hostname and parsed.port != 0
                    and not parsed.username and not parsed.password
                    and not parsed.query and not parsed.fragment
                    and not any(character.isspace() or ord(character) < 32 for character in endpoint)
                    and "\\" not in endpoint
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Desktop model endpoint must be HTTPS without credentials, query or fragment")
            # Translation configures its endpoint on the existing singleton;
            # start_download deliberately retains its zero-argument API.
            if getattr(self._translation, "endpoint", None) != endpoint:
                if self._read_model(1)["status"] in _ACTIVE:
                    raise RuntimeError("Cannot change model endpoint during an active translation download")
                self._translation.endpoint = endpoint
        return revision, endpoint

    def _check_dependencies(self) -> None:
        for check in (self._dependency_check, self._translation.dependency_status):
            result = check()
            if not result.get("ready", False):
                missing = ", ".join(str(item) for item in result.get("missing", []))
                raise RuntimeError("Desktop runtime dependencies unavailable" + (f": {missing}" if missing else ""))

    def _read_model(self, index: int) -> dict[str, Any]:
        if index == 0:
            raw = next((row for row in self._whisper.list_models() if row["name"] == WHISPER_MODEL), None)
            if raw is None:
                raise RuntimeError("Whisper manager inventory does not contain base.en")
            state = str(raw.get("status", "NOT_INSTALLED")).lower()
            downloaded = raw.get("downloadedBytes", raw.get("installedBytes", 0))
            total, error = raw.get("totalBytes"), raw.get("error")
        else:
            raw = self._translation.status()
            state = str(raw.get("model_status", "NOT_INSTALLED")).lower()
            downloaded = raw.get("downloaded_bytes", raw.get("installed_bytes", 0))
            total, error = raw.get("total_bytes"), raw.get("model_error")
        state = "failed" if state == "error" else state
        phase = str(raw.get("phase") or state).lower()
        if state in _ACTIVE and phase in _ACTIVE:
            state = phase
        if state not in _ACTIVE | {"ready", "failed", "not_installed"}:
            raise RuntimeError("Model manager returned an unsupported preparation state")
        return {
            "status": state, "phase": phase, "downloaded_bytes": downloaded,
            "total_bytes": total, "error": error, "revision": raw.get("revision"),
        }

    def _publish(self, index: int, row: dict[str, Any]) -> None:
        with self._lock:
            # A manager's READY can mean only that files exist. Keep it checking
            # until revision, dependencies and offline validation have passed.
            update = {**row, "revision": None}
            if update.get("status") == "ready":
                update.update(status="checking", phase="checking")
            self._snapshot["models"][index].update(update)

    def _prepare_model(self, index: int, revision: str, endpoint: str | None) -> None:
        row = self._read_model(index)
        self._publish(index, row)
        started = False
        # A READY model with no recorded pin cannot be certified by a load test.
        # Ask the owning manager to repair/reinstall it using the pinned revision.
        if row["status"] not in _ACTIVE and (row["status"] != "ready" or row["revision"] != revision):
            if index == 0:
                self._whisper.download(WHISPER_MODEL, revision=revision, endpoint=endpoint)
            else:
                self._translation.start_download()
            started = True
        deadline = time.monotonic() + self._prepare_timeout
        delay = self._poll_interval
        while True:
            row = self._read_model(index)
            self._publish(index, row)
            if row["status"] == "failed":
                raise RuntimeError(row["error"] or "Model preparation failed; retry is available")
            if row["status"] == "ready":
                if row["revision"] != revision:
                    raise RuntimeError("Installed model revision is missing or differs from the desktop pin")
                break
            if row["status"] not in _ACTIVE:
                raise RuntimeError("Model manager did not start preparation; retry is available")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Model preparation timed out; retry will observe any ongoing download")
            time.sleep(min(delay, remaining))
            delay = min(delay * 1.5, self._max_poll_interval)
        if not started:
            # Existing files and externally started jobs still need explicit
            # offline validation. Use the translation manager's existing cache.
            if index == 0:
                try:
                    self._whisper_validator(self._whisper)
                except Exception as exc:
                    marker = getattr(self._whisper, "mark_failed", None)
                    if marker is not None:
                        marker(WHISPER_MODEL, exc)
                    raise
            else:
                self._translation.validate_offline()
        self._check_dependencies()
        with self._lock:
            self._snapshot["models"][index].update(
                status="ready", phase="ready", error=None, revision=row["revision"],
            )

    def _prepare(self) -> None:
        index = 0
        try:
            revision, endpoint = self._initialize()
            self._check_dependencies()
            self._prepare_model(0, revision, endpoint)
            index = 1
            self._prepare_model(1, self._translation_revision, endpoint)
            self._check_dependencies()
            with self._lock:
                self._checked_at = time.monotonic()
                self._snapshot.update(state="ready", ready=True, error=None)
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            with self._lock:
                self._snapshot["models"][index].update(
                    status="failed", phase="failed", error=message, revision=None,
                )
                self._snapshot.update(state="failed", ready=False, error=message)


_manager: DesktopModelManager | None = None
_manager_lock = threading.Lock()


def get_desktop_model_manager() -> DesktopModelManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = DesktopModelManager()
        return _manager
