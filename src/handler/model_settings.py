"""Model settings API for LLM, Whisper and TTS providers."""
from __future__ import annotations

import importlib.util
import time
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import RootModel

from src.config import settings
from src.config.model_schema import validate_models
from src.core.translation_engines import TranslationEngineError, make_engine_client
from src.handler.deps import require_api_token
from src.service.model_manager import model_manager
from src.store.model_settings_store import KINDS, ModelSettingsStore

router = APIRouter(prefix="/api/settings/models", tags=["model-settings"])


class SettingsPayload(RootModel[dict[str, Any]]):
    """Accept the extensible per-kind settings payload."""


def _store() -> ModelSettingsStore:
    return ModelSettingsStore(settings.db_path)


def _public_model(item: dict[str, Any], *, current: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    camel_secret = result.pop("apiKey", None)
    snake_secret = result.pop("api_key", None)
    secret = camel_secret or snake_secret
    is_current = bool(result.get("isCurrent", current.get("model") == result.get("model")))
    result["hasApiKey"] = bool(secret)
    result.setdefault("isCurrent", is_current)
    result.setdefault("enabled", True)
    result.setdefault("baseUrl", "")
    result.setdefault("provider", current.get("provider", ""))
    result.setdefault("model", current.get("model", ""))
    result.setdefault("name", result.get("model") or result.get("id"))
    return result


def _public_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    result = dict(config)
    models = list(config.get("models") or [])
    current_model = next(
        (item for item in models if isinstance(item, dict) and item.get("isCurrent")),
        None,
    )
    result.pop("apiKey", None)
    result.pop("api_key", None)
    result.pop("_runtimeManaged", None)
    result["hasApiKey"] = bool(
        (current_model or {}).get("apiKey") or (current_model or {}).get("api_key")
    )
    result["models"] = [_public_model(item, current=config) for item in models]
    return result


def _public_all(store: ModelSettingsStore) -> dict[str, dict[str, Any]]:
    all_values = store.get_all()
    return {kind: _public_config(kind, all_values[kind]) for kind in KINDS}


def _normalise_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if any(key in payload for key in KINDS):
        return {kind: value for kind, value in payload.items() if kind in KINDS and isinstance(value, dict)}
    kind = payload.get("kind")
    if isinstance(kind, str) and kind in KINDS:
        return {kind: {key: value for key, value in payload.items() if key != "kind"}}
    raise HTTPException(status_code=422, detail="请求必须包含 llm、whisper 或 tts 配置")


@router.get("", response_model=dict[str, dict[str, Any]])
def get_model_settings() -> dict[str, dict[str, Any]]:
    return _public_all(_store())


@router.put("", response_model=dict[str, dict[str, Any]], dependencies=[Depends(require_api_token)])
def update_model_settings(body: SettingsPayload) -> dict[str, dict[str, Any]]:
    values = _normalise_payload(body.root)
    store = _store()
    try:
        store.update(values)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"不支持的模型类型: {exc.args[0]}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _public_all(store)


@router.post("/{kind}", status_code=201, response_model=dict[str, Any], dependencies=[Depends(require_api_token)])
def create_model(kind: str, body: SettingsPayload) -> dict[str, Any]:
    if kind not in KINDS:
        raise HTTPException(status_code=404, detail="不支持的模型类型")
    store = _store()
    payload = dict(body.root)
    payload.pop("kind", None)
    try:
        item, config = store.create_model(kind, payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"model": _public_model(item, current=config), "config": _public_config(kind, config)}


def _validate(kind: str, payload: dict[str, Any], store: ModelSettingsStore) -> dict[str, Any]:
    started = time.perf_counter()
    config = store.get(kind) or {}
    candidate = dict(config)
    candidate.update(payload)
    try:
        validate_models({kind: candidate})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    provider = str(candidate.get("provider") or "").strip().lower()
    model = str(candidate.get("model") or "").strip()
    current_cards = [item for item in config.get("models", []) if isinstance(item, dict)]
    selected_id = next((item.get("id") for item in current_cards if item.get("isCurrent")), None)
    if isinstance(payload.get("models"), list):
        requested_cards = [item for item in payload["models"] if isinstance(item, dict)]
        selected = next((item for item in requested_cards if item.get("isCurrent")), None)
        selected_id = (selected or {}).get("id") or selected_id
    saved_card = next((item for item in current_cards if str(item.get("id")) == str(selected_id)), None)
    api_key = payload.get("apiKey") or (saved_card or {}).get("apiKey")
    if kind == "llm":
        if provider not in {"deepseek", "openai", "openai_compatible", "anthropic", "anthropic_compatible", "custom"}:
            return {"ok": False, "message": f"不支持的 LLM Provider: {provider}"}
        if not api_key:
            return {"ok": False, "message": "未配置 API Key"}
        try:
            client = make_engine_client(SimpleNamespace(
                api_type="anthropic_compatible" if provider in {"anthropic", "anthropic_compatible"} else "openai_compatible",
                base_url=candidate.get("baseUrl") or "", model=model, api_key=api_key,
                temperature=float(candidate.get("temperature") or 0.3),
                max_tokens=int(candidate.get("maxTokens") or 4096),
            ), timeout=int(candidate.get("timeout") or 120))
            client.complete("Reply with OK only.", "OK", max_tokens=4)
        except TranslationEngineError as exc:
            message = "API Key 无效" if exc.code == "unauthorized" else str(exc)
            return {"ok": False, "message": message, "latencyMs": int((time.perf_counter() - started) * 1000)}
        except Exception as exc:
            return {"ok": False, "message": str(exc), "latencyMs": int((time.perf_counter() - started) * 1000)}
    elif kind == "whisper":
        if provider == "local":
            selected = model.removeprefix("local:")
            if not selected or not model_manager.is_ready(selected):
                return {"ok": False, "message": "本地 Whisper 模型尚未就绪", "latencyMs": int((time.perf_counter() - started) * 1000)}
            dependency = "transformers" if model_manager.is_hf(selected) else "faster_whisper"
            if importlib.util.find_spec(dependency) is None:
                return {
                    "ok": False,
                    "message": f"未安装本地 Whisper 依赖: {dependency}",
                    "latencyMs": int((time.perf_counter() - started) * 1000),
                }
        elif provider == "replicate":
            return {"ok": False, "message": "当前版本不支持 Replicate Whisper"}
        else:
            return {"ok": False, "message": f"不支持的 Whisper Provider: {provider}"}
    elif kind == "tts":
        if provider not in {"kokoro", "local"}:
            return {"ok": False, "message": f"不支持的 TTS Provider: {provider}"}
        if importlib.util.find_spec("kokoro") is None:
            return {"ok": False, "message": "未安装 Kokoro 依赖", "latencyMs": int((time.perf_counter() - started) * 1000)}
        try:
            # Loading the pipeline catches broken local installations and
            # unavailable model assets before the user saves a card.  The
            # loader owns its language cache, so this check does not create a
            # second runtime for normal task execution.
            from src.core.tts import _pipeline

            _pipeline("en", model_id=model or None)
        except Exception as exc:
            return {
                "ok": False,
                "message": f"Kokoro 模型加载失败: {exc}",
                "latencyMs": int((time.perf_counter() - started) * 1000),
            }
    return {"ok": True, "message": "连接成功", "latencyMs": int((time.perf_counter() - started) * 1000)}


@router.post("/{kind}/validate", response_model=dict[str, Any], dependencies=[Depends(require_api_token)])
def validate_model(kind: str, body: SettingsPayload) -> dict[str, Any]:
    if kind not in KINDS:
        raise HTTPException(status_code=404, detail="不支持的模型类型")
    return _validate(kind, dict(body.root), _store())
