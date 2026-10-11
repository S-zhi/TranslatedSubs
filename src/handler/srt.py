"""SRT 相关配置接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from src.handler.deps import get_store, require_api_token

from src.service.model_manager import MODEL_NAMES, ModelDependencyError, ModelValidationError, model_manager
from src.store import TaskStore

router = APIRouter(prefix="/api/srt", tags=["srt"])
LOCAL_VIDEO_LANGUAGES = ["en", "zh", "hi", "es", "ar", "fr", "pt", "ru", "de", "ko", "ja"]


@router.get("/languages", response_model=list[str])
def list_video_languages() -> list[str]:
    """返回本地 Whisper 支持的视频源语言列表。"""
    return list(LOCAL_VIDEO_LANGUAGES)


@router.get("/model-weights", response_model=list[str])
def list_model_weights() -> list[str]:
    """返回已就绪的本地 Whisper 模型。"""
    return [f"local:{item['name']}" for item in model_manager.list_models() if item["status"] == "READY"]


@router.get("/model-options", response_model=list[str])
def list_model_options() -> list[str]:
    """返回带本地后端标识的 Whisper 模型选项。"""
    return list_model_weights()


@router.get("/local-models")
def list_local_models() -> list[dict]:
    """返回已安装和可导入的本地模型。"""
    return model_manager.list_models()


@router.post("/local-models/import", dependencies=[Depends(require_api_token)])
def import_local_model(
    name: str = Form(...),
    label: str = Form(""),
    files: list[UploadFile] = File(...),
) -> dict:
    """接收用户已有的 Hugging Face Whisper 文件并离线检查。"""
    try:
        return model_manager.import_files(name, label, [(item.filename or "", item.file) for item in files])
    except ModelValidationError as exc:
        raise HTTPException(status_code=422, detail={"code": "MODEL_INVALID", "message": str(exc), "cleaned": True}) from exc
    except ModelDependencyError as exc:
        raise HTTPException(status_code=503, detail={"code": "MODEL_DEPENDENCY_MISSING", "message": str(exc), "cleaned": True}) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        for item in files:
            item.file.close()


@router.post("/local-models/{model_name}/download", dependencies=[Depends(require_api_token)])
def download_local_model(model_name: str) -> dict:
    """按用户操作下载官方 faster-whisper 模型，并返回可轮询状态。"""
    if model_name not in MODEL_NAMES:
        raise HTTPException(status_code=404, detail=f"未知官方模型: {model_name}")
    return model_manager.download(model_name)


def _ensure_model_not_in_use(model_name: str, store: TaskStore) -> None:
    local_backends = {"local", "local_whisper", "whisper", "faster_whisper"}
    for task in store.list():
        if task.status in {"SUCCESS", "FAILED", "CANCELLED"} or not task.need_subtitle:
            continue
        backend, separator, selected_model = str(task.model or "").partition(":")
        if separator and backend.strip().lower() not in local_backends:
            continue
        if (selected_model if separator else task.model).strip() == model_name:
            raise HTTPException(
                status_code=409,
                detail={"code": "MODEL_IN_USE", "message": "有未结束的任务选用了这个模型，暂时无法清理"},
            )


@router.post("/local-models/{model_name}/check", dependencies=[Depends(require_api_token)])
def check_local_model(model_name: str, store: TaskStore = Depends(get_store)) -> dict:
    """重新验证受管模型；不合规则清理该受管副本。"""
    _ensure_model_not_in_use(model_name, store)
    try:
        return model_manager.check(model_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ModelValidationError as exc:
        raise HTTPException(status_code=422, detail={"code": "MODEL_INVALID", "message": str(exc), "cleaned": True}) from exc
    except ModelDependencyError as exc:
        raise HTTPException(status_code=503, detail={"code": "MODEL_DEPENDENCY_MISSING", "message": str(exc), "cleaned": False}) from exc


@router.delete("/local-models/{model_name}", dependencies=[Depends(require_api_token)])
def delete_local_model(model_name: str, store: TaskStore = Depends(get_store)) -> dict:
    """删除受管模型，避免清理正在使用的模型文件。"""
    _ensure_model_not_in_use(model_name, store)

    try:
        return model_manager.delete(model_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/target-languages", response_model=list[str])
def list_target_languages() -> list[str]:
    """返回支持的翻译目标语言列表。"""
    from src.config import settings
    return list(settings.target_languages)
