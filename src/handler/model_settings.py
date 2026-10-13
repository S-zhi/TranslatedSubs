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
from src.service.model_runtime import canonical_whisper_model, whisper_effective_config, whisper_inventory_status
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


def _public_config(kind: str, config: dict[str, Any], store: ModelSettingsStore | None = None) -> dict[str, Any]:
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
    if kind == "whisper":
        result["runtimeManaged"] = bool(config.get("_runtimeManaged"))
        inventory = {item["name"]: item for item in model_manager.list_models()}
        effective = whisper_effective_config(config)
        for key in ("provider", "model", "enabled"):
            if key in effective:
                result[key] = effective[key]
        result["status"] = _whisper_status(
            effective, inventory, missing_uninstalled=bool(config.get("_runtimeManaged")),
        )
        for card in result["models"]:
            card.pop("runtimeManaged", None)
            card.pop("_runtimeManaged", None)
            card["status"] = _whisper_status(
                card, inventory, missing_uninstalled=bool(config.get("_runtimeManaged")),
            )
    return result


def _whisper_status(
    config: dict[str, Any], inventory: dict[str, dict[str, Any]], *, missing_uninstalled: bool = False,
) -> str:
    return whisper_inventory_status(config, list(inventory.values()), missing_uninstalled=missing_uninstalled)


def _public_all(store: ModelSettingsStore) -> dict[str, dict[str, Any]]:
    all_values = store.get_all()
    return {kind: _public_config(kind, all_values[kind], store) for kind in KINDS}


def _normalise_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if any(key in payload for key in KINDS):
        values = {kind: value for kind, value in payload.items() if kind in KINDS}
        invalid = next((kind for kind, value in values.items() if not isinstance(value, dict)), None)
        if invalid:
            raise HTTPException(status_code=422, detail=f"{invalid} 配置必须是对象")
        return values
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
    # Reject client attempts to change internal/runtime-derived flags. A
    # real writable field must accompany an explicit save before management
    # mode is enabled.
    values = {kind: _strip_readonly(value) for kind, value in values.items()}
    values = {kind: value for kind, value in values.items() if _has_writable_change(value)}
    if not values:
        return _public_all(store)
    prepared, revision = store.prepare_versioned(values)
    if "whisper" in values:
        if prepared["whisper"].get("enabled", True):
            cards = values["whisper"].get("models")
            if isinstance(cards, list) and not any(
                isinstance(card, dict) and card.get("isCurrent") for card in cards
            ):
                raise HTTPException(status_code=422, detail="启用 Whisper 设置时必须指定唯一当前模型")
        _validate_saved_whisper(prepared["whisper"])
    try:
        store.update(values, prepared=prepared, expected_revision=revision)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"不支持的模型类型: {exc.args[0]}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _public_all(store)


_READONLY_FIELDS = {"status", "runtimeManaged", "_runtimeManaged", "hasApiKey"}


def _strip_readonly(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_readonly(item) for key, item in value.items() if key not in _READONLY_FIELDS}
    if isinstance(value, list):
        return [_strip_readonly(item) for item in value]
    return value


def _has_writable_change(value: dict[str, Any]) -> bool:
    for key, item in value.items():
        if key in _READONLY_FIELDS:
            continue
        if key == "models" and isinstance(item, list):
            if any(isinstance(card, dict) and any(field != "id" for field in card) for card in item):
                return True
            continue
        return True
    return False


def _validate_saved_whisper(candidate: dict[str, Any]) -> None:
    provider = str(candidate.get("provider") or "").strip().lower()
    if provider != "local":
        if candidate.get("enabled", True):
            raise HTTPException(status_code=422, detail=f"不支持的 Whisper Provider: {provider}")
        return
    raw_model = str(candidate.get("model") or "").strip()
    model = canonical_whisper_model(raw_model) if raw_model else ""
    candidate["provider"] = "local"
    name = model.removeprefix("local:")
    if candidate.get("enabled", True) and (not name or not model_manager.is_ready(name)):
        raise HTTPException(status_code=409, detail={"code": "MODEL_NOT_READY", "message": "本地 Whisper 模型尚未就绪"})
    candidate["model"] = model
    candidate["provider"] = "local"
    active = next((item for item in candidate.get("models", []) if item.get("isCurrent")), None)
    if active:
        active["provider"] = "local"
        active["model"] = model


@router.post("/{kind}", status_code=201, response_model=dict[str, Any], dependencies=[Depends(require_api_token)])
def create_model(kind: str, body: SettingsPayload) -> dict[str, Any]:
    if kind not in KINDS:
        raise HTTPException(status_code=404, detail="不支持的模型类型")
    store = _store()
    payload = dict(body.root)
    payload.pop("kind", None)
    payload = _strip_readonly(payload)
    if kind == "whisper":
        if str(payload.get("provider") or "").lower() == "local" and payload.get("model"):
            payload["model"] = canonical_whisper_model(str(payload.get("model") or ""))
    try:
        item, config, revision = store.prepare_create_versioned(kind, payload)
        if kind == "whisper":
            _validate_saved_whisper(config)
            item = next(model for model in config["models"] if model.get("id") == item["id"])
        store.update({kind: config}, prepared={kind: config}, expected_revision=revision)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    public_model = _public_model(item, current=config)
    if kind == "whisper":
        public_model["status"] = _whisper_status(
            public_model, {model["name"]: model for model in model_manager.list_models()},
            missing_uninstalled=bool(config.get("_runtimeManaged")),
        )
    return {"model": public_model, "config": _public_config(kind, config, store)}


def _validate(kind: str, payload: dict[str, Any], store: ModelSettingsStore) -> dict[str, Any]:
    started = time.perf_counter()
    config = store.get(kind) or {}
    if kind == "whisper":
        config = store.prepare({kind: payload})[kind]
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
