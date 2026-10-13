"""Shared task HTTP wiring and compatibility helpers.

The endpoint implementations live in :mod:`task_lifecycle`,
:mod:`task_artifacts`, and :mod:`task_runtime`.  This module owns the common
router and the small helpers shared by those endpoint modules.
"""

from __future__ import annotations

from functools import wraps
from typing import Callable, Optional, TypeVar, Any

from fastapi import APIRouter, HTTPException

from src.handler.schemas import ErrorDetail
from src.service.model_manager import model_manager
from src.service.translation_model_manager import get_translation_model_manager
from src.store import (
    DOWNGRADE_REASON_UNKNOWN,
    TranslationEngineStore,
)


F = TypeVar("F", bound=Callable[..., Any])


def task_error_boundary(func: F) -> F:
    """Map shared task service errors to their historic HTTP responses.

    ``HTTPException`` is deliberately passed through unchanged so endpoint
    specific validation keeps its original status and detail.  ``wraps``
    preserves the public endpoint signature for FastAPI and direct callers.
    """

    @wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except HTTPException:
            raise
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc) or "任务不存在") from exc
        except RuntimeError as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code is not None:
                raise HTTPException(
                    status_code=int(status_code),
                    detail=getattr(exc, "detail", str(exc)),
                ) from exc
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return wrapped  # type: ignore[return-value]


# Import route modules after defining task_error_boundary.  They import this
# helper while task.py is being initialized, so the order avoids a cycle.
from src.service.task import task_artifacts, task_lifecycle, task_runtime


_BROWSER_PREVIEW_EXTENSIONS = {
    ".txt", ".srt", ".vtt", ".json", ".md", ".log", ".csv",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mov",
}
_TERMINAL = {"SUCCESS", "FAILED", "CANCELLED"}
_DELETED_MESSAGE = "资源已删除"
_UPLOAD_VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi",
    ".m4v", ".flv", ".ts", ".mpeg", ".mpg", ".wmv",
}


router = APIRouter(tags=["tasks"])
router.include_router(task_lifecycle.router)
router.include_router(task_artifacts.router)
router.include_router(task_runtime.router)


def _upload_error(
    status_code: int,
    *,
    code: str,
    message: str,
    limits: Optional[dict] = None,
    suggestion: Optional[str] = None,
) -> HTTPException:
    detail = ErrorDetail(
        code=code,
        message=message,
        limits=limits,
        suggestion=suggestion,
    ).model_dump(exclude_none=True)
    return HTTPException(status_code=status_code, detail=detail)


def _require(store, task_id: str):
    rec = store.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return rec


def _mark_resource_missing(
    store,
    task_id: str,
    reason: str,
    downgrade_reason: str = DOWNGRADE_REASON_UNKNOWN,
    downgrade_errno: Optional[int] = None,
) -> None:
    # Keep this helper patchable through handler.tasks while retaining the
    # service implementation as the source of the behavior.
    task_runtime.mark_resource_missing(
        store,
        task_id,
        reason,
        downgrade_reason=downgrade_reason,
        downgrade_errno=downgrade_errno,
    )


def _ensure_local_model_ready(model: str) -> None:
    value = str(model or "")
    backend, separator, name = value.partition(":")
    if separator and backend.strip().lower() == "replicate":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REPLICATE_BACKEND_REMOVED",
                "message": "Replicate 云端转写已移除，请选择已就绪的本地 Whisper 模型",
            },
        )
    if separator and backend.strip().lower() in {
        "local", "local_whisper", "whisper", "faster_whisper"
    }:
        if not model_manager.is_ready(name.strip()):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MODEL_NOT_READY",
                    "message": f"本地模型未导入或未通过检查: {name}",
                },
            )


def _ensure_translation_engine(
    engine: str,
    need_subtitle: bool,
    engines: TranslationEngineStore,
) -> None:
    from src.handler import tasks as compat
    from src.service.model_runtime import resolve_engine
    from src.store.model_settings_store import ModelSettingsStore

    resolve_engine(
        engine,
        need_subtitle,
        engines,
        model_store=ModelSettingsStore(
            getattr(engines, "db_path", getattr(compat.settings, "db_path", "translatedsubs.db"))
        ),
        compat_settings=compat.settings,
        translation_model_manager=compat.get_translation_model_manager(),
    )


def _ensure_supported_translation_languages(
    engine: str,
    need_subtitle: bool,
    source_lang: str,
    target_lang: str,
) -> None:
    if not need_subtitle or engine != "local-opus-en-zh":
        return
    if source_lang != "en" or target_lang not in {"zh-CN", "zh"}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNSUPPORTED_TRANSLATION_LANGUAGE",
                "message": "本地翻译模型只支持源语言 en 和目标语言 zh-CN/zh。",
            },
        )


__all__ = [
    "router",
    "task_error_boundary",
    "_BROWSER_PREVIEW_EXTENSIONS",
    "_TERMINAL",
    "_DELETED_MESSAGE",
    "_UPLOAD_VIDEO_EXTS",
    "_upload_error",
    "_require",
    "_mark_resource_missing",
    "_ensure_local_model_ready",
    "_ensure_translation_engine",
    "_ensure_supported_translation_languages",
]
