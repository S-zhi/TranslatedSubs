"""Shared business configuration API backed by the Redis aggregate store."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import RootModel

from src.config.config import _validate_business_patch
from src.config.config_repository import get_config_provider
from src.config.redis_store import ConfigUnavailable, RevisionConflict
from src.config.schema import BUSINESS_DEFAULTS, SECRET_KEYS, effective_business_values
from src.handler.deps import require_api_token

router = APIRouter(prefix="/api/settings/runtime", tags=["runtime-settings"])


class RuntimeSettingsPayload(RootModel[dict[str, Any]]):
    pass


def _public(snapshot) -> dict[str, Any]:
    business = effective_business_values(snapshot.values["business"])
    values = {
        key: value for key, value in business.items()
        if key in BUSINESS_DEFAULTS and key not in SECRET_KEYS
    }
    return {
        "schema": snapshot.schema,
        "revision": snapshot.revision,
        "updatedAt": snapshot.updated_at,
        "values": values,
        "hasSecrets": {
            key: bool(business.get(key))
            for key in sorted(SECRET_KEYS)
        },
    }


def _save(values: dict[str, Any], expected_revision: int | None, *, replace: bool = False):
    try:
        clean = _validate_business_patch(values)

        def apply(document: dict[str, Any]) -> dict[str, Any]:
            business = document["business"]
            if replace:
                secrets = {key: business[key] for key in SECRET_KEYS if key in business}
                business.clear()
                business.update(BUSINESS_DEFAULTS)
                business.update(secrets)
            for key, value in clean.items():
                if value is None:
                    business.pop(key, None)
                else:
                    business[key] = value
            return document

        return _public(get_config_provider().mutate(apply, expected_revision=expected_revision))
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail="Redis 配置存储不可用") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _revision(if_match: str | None) -> int | None:
    if if_match is None:
        return None
    raw = if_match.strip().strip('"')
    try:
        return int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="If-Match 必须是配置 revision") from exc


@router.get("", dependencies=[Depends(require_api_token)])
def get_runtime_settings() -> dict[str, Any]:
    try:
        return _public(get_config_provider().read())
    except ConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail="Redis 配置存储不可用") from exc


@router.patch("", dependencies=[Depends(require_api_token)])
def patch_runtime_settings(
    body: RuntimeSettingsPayload,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, Any]:
    return _save(body.root, _revision(if_match))


@router.put("", dependencies=[Depends(require_api_token)])
def put_runtime_settings(
    body: RuntimeSettingsPayload,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, Any]:
    return _save(body.root, _revision(if_match), replace=True)
