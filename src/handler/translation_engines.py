"""翻译引擎配置与连通性检测 API。"""

from __future__ import annotations

import time
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from src.core.translation_engines import TranslationEngineError, make_engine_client
from src.handler.deps import get_translation_engine_store, require_api_token
from src.service.translation_model_manager import (
    ENGINE_ID as LOCAL_ENGINE_ID,
    MODEL_STATUS_READY,
    TranslationModelError,
    get_translation_model_manager,
)
from src.store import (
    DEFAULT_TRANSLATION_ENGINE_ID,
    ENGINE_TYPES,
    LOCAL_TRANSLATION_ENGINE_ID,
    LOCAL_TRANSLATION_ENGINE_NAME,
    LOCAL_TRANSLATION_MODEL,
    TranslationEngine,
    TranslationEngineStore,
)


router = APIRouter(prefix="/api/settings/translation-engines", tags=["translation-engines"])

VALIDATE_TIMEOUT_SEC = 10


class EngineIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    apiType: str = Field(min_length=1)
    baseUrl: str = Field(default="", max_length=500)
    model: str = Field(min_length=1, max_length=200)
    apiKey: Optional[str] = Field(default=None, max_length=500)
    enabled: bool = True


class EngineOut(BaseModel):
    id: str
    name: str
    apiType: str
    baseUrl: str
    model: str
    enabled: bool
    hasApiKey: bool
    availability: str
    lastCheckedAt: Optional[int] = None
    lastError: Optional[str] = None
    apiKeyRotatedAt: Optional[int] = None
    active: bool = False
    modelStatus: Optional[str] = None
    installedBytes: Optional[int] = None
    modelError: Optional[str] = None
    supportedSourceLanguages: Optional[List[str]] = None
    supportedTargetLanguages: Optional[List[str]] = None


class EngineCheckOut(BaseModel):
    id: str
    availability: str
    available: bool
    checkedAt: int
    errorCode: Optional[str] = None
    message: Optional[str] = None


def _validate_type(api_type: str) -> None:
    if api_type not in ENGINE_TYPES:
        raise HTTPException(status_code=422, detail="不支持的 API 接入类型")


def _out(rec: TranslationEngine, *, active: bool = False) -> EngineOut:
    local_status = None
    if rec.api_type == "local_ct2":
        state = get_translation_model_manager().status()
        local_status = state["model_status"]
        availability = "AVAILABLE" if local_status == MODEL_STATUS_READY else "UNAVAILABLE"
        error = state.get("model_error")
    else:
        state = {}
        availability = rec.availability
        error = rec.last_error
    return EngineOut(
        id=rec.id, name=rec.name, apiType=rec.api_type, baseUrl=rec.base_url,
        model=rec.model, enabled=bool(rec.enabled), hasApiKey=rec.has_api_key,
        availability=availability, lastCheckedAt=rec.last_checked_at,
        lastError=error,
        apiKeyRotatedAt=rec.api_key_rotated_at,
        active=active,
        modelStatus=local_status,
        installedBytes=state.get("installed_bytes"),
        modelError=state.get("model_error"),
        supportedSourceLanguages=["en"] if local_status is not None else None,
        supportedTargetLanguages=["zh-CN", "zh"] if local_status is not None else None,
    )


def _require(store: TranslationEngineStore, engine_id: str) -> TranslationEngine:
    rec = store.get(engine_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="翻译引擎配置不存在")
    return rec


@router.get("", response_model=List[EngineOut])
def list_engines(store: TranslationEngineStore = Depends(get_translation_engine_store)) -> List[EngineOut]:
    records = store.list()
    # 页面需要明确显示默认运行配置：优先使用已检测可用的引擎，否则回退到首个启用项。
    local_default = next((r for r in records if r.id == DEFAULT_TRANSLATION_ENGINE_ID and r.enabled), None)
    active_id = local_default.id if local_default is not None else next((
        r.id for r in records
        if r.enabled and (
            r.availability == "AVAILABLE"
            or (r.api_type == "local_ct2" and get_translation_model_manager().is_ready())
        )
    ), None)
    if active_id is None:
        active_id = next((r.id for r in records if r.enabled), None)
    return [_out(rec, active=rec.id == active_id) for rec in records]


@router.post("", response_model=EngineOut, status_code=201, dependencies=[Depends(require_api_token)])
def create_engine(
    body: EngineIn,
    background_tasks: BackgroundTasks,
    store: TranslationEngineStore = Depends(get_translation_engine_store),
) -> EngineOut:
    _validate_type(body.apiType)
    if body.apiType == "local_ct2":
        raise HTTPException(status_code=422, detail="本地模型配置是固定内置项，不能自行创建")
    if not body.baseUrl.strip():
        raise HTTPException(status_code=422, detail="翻译引擎 Base URL 不能为空")
    key = body.apiKey.strip() if body.apiKey and body.apiKey.strip() else None
    rec = store.create(
        name=body.name, api_type=body.apiType, base_url=body.baseUrl,
        model=body.model, api_key=key, enabled=body.enabled,
    )
    background_tasks.add_task(_validate_engine, rec.id, store)
    return _out(rec)


@router.put("/{engine_id}", response_model=EngineOut, dependencies=[Depends(require_api_token)])
def update_engine(
    engine_id: str,
    body: EngineIn,
    store: TranslationEngineStore = Depends(get_translation_engine_store),
) -> EngineOut:
    _validate_type(body.apiType)
    rec = _require(store, engine_id)
    if rec.id == LOCAL_TRANSLATION_ENGINE_ID:
        if (
            body.apiType != "local_ct2"
            or body.name != LOCAL_TRANSLATION_ENGINE_NAME
            or body.baseUrl != ""
            or body.model != LOCAL_TRANSLATION_MODEL
            or (body.apiKey and body.apiKey.strip())
        ):
            raise HTTPException(status_code=422, detail="本地模型信息固定，只能启用或停用")
        updated = store.update(engine_id, enabled=body.enabled)
        return _out(updated or rec)
    if body.apiType == "local_ct2":
        raise HTTPException(status_code=422, detail="本地模型只能使用固定内置配置")
    if not body.baseUrl.strip():
        raise HTTPException(status_code=422, detail="翻译引擎 Base URL 不能为空")
    fields = dict(name=body.name, api_type=body.apiType, base_url=body.baseUrl, model=body.model, enabled=body.enabled)
    # 空值表示保持原密钥不变；创建配置时则自然表示未配置。
    if body.apiKey is not None:
        fields["api_key"] = body.apiKey.strip() if body.apiKey.strip() else ""
    updated = store.update(engine_id, **fields)
    return _out(updated or rec)


@router.delete("/{engine_id}", status_code=204, dependencies=[Depends(require_api_token)])
def delete_engine(engine_id: str, store: TranslationEngineStore = Depends(get_translation_engine_store)) -> None:
    rec = _require(store, engine_id)
    if rec.api_type == "local_ct2":
        raise HTTPException(status_code=422, detail="本地模型固定内置项不能删除")
    store.delete(engine_id)


def _validate_engine(engine_id: str, store: TranslationEngineStore) -> EngineCheckOut:
    rec = _require(store, engine_id)
    checked_at = int(time.time() * 1000)
    if rec.api_type == "local_ct2":
        manager = get_translation_model_manager()
        try:
            manager.validate_offline()
        except TranslationModelError as exc:
            manager.mark_failed(exc)
            store.update(engine_id, availability="UNAVAILABLE", last_checked_at=checked_at, last_error=str(exc))
            return EngineCheckOut(
                id=engine_id, availability="UNAVAILABLE", available=False,
                checkedAt=checked_at, errorCode=exc.code, message=str(exc),
            )
        store.update(engine_id, availability="AVAILABLE", last_checked_at=checked_at, last_error=None)
        return EngineCheckOut(id=engine_id, availability="AVAILABLE", available=True, checkedAt=checked_at)
    if not (rec.api_key and rec.api_key.strip()):
        store.update(engine_id, availability="UNCONFIGURED", last_checked_at=checked_at, last_error="未配置 API Key")
        return EngineCheckOut(id=engine_id, availability="UNCONFIGURED", available=False, checkedAt=checked_at, errorCode="missing_api_key", message="未配置 API Key")

    store.update(engine_id, availability="CHECKING", last_checked_at=checked_at, last_error=None)
    try:
        client = make_engine_client(rec, timeout=VALIDATE_TIMEOUT_SEC)
        client.complete("Reply with OK only.", "OK", max_tokens=4)
    except TranslationEngineError as exc:
        store.update(engine_id, availability="UNAVAILABLE", last_checked_at=checked_at, last_error=str(exc))
        return EngineCheckOut(id=engine_id, availability="UNAVAILABLE", available=False, checkedAt=checked_at, errorCode=exc.code, message=str(exc))
    except Exception as exc:
        store.update(engine_id, availability="UNAVAILABLE", last_checked_at=checked_at, last_error=str(exc))
        return EngineCheckOut(id=engine_id, availability="UNAVAILABLE", available=False, checkedAt=checked_at, errorCode="validation_error", message=str(exc))

    store.update(engine_id, availability="AVAILABLE", last_checked_at=checked_at, last_error=None)
    return EngineCheckOut(id=engine_id, availability="AVAILABLE", available=True, checkedAt=checked_at)


@router.post("/{engine_id}/validate", response_model=EngineCheckOut, dependencies=[Depends(require_api_token)])
def validate_engine(
    engine_id: str,
    store: TranslationEngineStore = Depends(get_translation_engine_store),
) -> EngineCheckOut:
    return _validate_engine(engine_id, store)


@router.post(
    "/{engine_id}/download",
    response_model=EngineOut,
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
def download_engine(
    engine_id: str,
    store: TranslationEngineStore = Depends(get_translation_engine_store),
) -> EngineOut:
    rec = _require(store, engine_id)
    if engine_id != LOCAL_ENGINE_ID or rec.api_type != "local_ct2":
        raise HTTPException(status_code=404, detail="没有可下载的模型")
    get_translation_model_manager().start_download()
    return _out(rec)
