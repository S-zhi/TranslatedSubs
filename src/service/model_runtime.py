"""Resolve model settings once when a task is created.

The returned snapshot is safe to persist: it contains model routing data but
never an API key.  Workers use the snapshot for model identity and look up the
current secret only when execution starts.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException

from src.config import settings as default_settings
from src.store import ModelSettingsStore

_SNAPSHOT_VERSION = 1
_LLM_PROVIDERS = {
    "deepseek", "openai", "openai_compatible", "anthropic",
    "anthropic_compatible", "custom",
}
_TTS_PROVIDERS = {"kokoro", "local"}


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def active_model_card(store: ModelSettingsStore, kind: str) -> dict[str, Any] | None:
    """Return the saved current card when unified runtime management is on."""
    config = store.get(kind) or {}
    if not config.get("_runtimeManaged") or not config.get("enabled", True):
        return None
    models = [item for item in config.get("models", []) if isinstance(item, dict)]
    card = next((item for item in models if item.get("isCurrent")), None)
    merged = dict(config)
    if card:
        merged.update(card)
        if not merged.get("apiKey") and config.get("apiKey"):
            merged["apiKey"] = config["apiKey"]
    return merged


def _card_id(card: dict[str, Any] | None) -> str | None:
    value = card.get("id") if card else None
    return str(value) if value else None


def _engine_params(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "temperature": float(item.get("temperature") or 0.3),
        "maxTokens": int(item.get("maxTokens") or 4096),
        "timeout": int(item.get("timeout") or 120),
        "concurrency": max(1, int(item.get("concurrency") or 1)),
    }


def _check_local_translation(compat_settings: Any, manager: Any = None) -> None:
    if manager is None:
        from src.service.translation_model_manager import get_translation_model_manager
        manager = get_translation_model_manager()
    if not manager.is_ready():
        raise _error(409, "LOCAL_TRANSLATION_MODEL_NOT_READY", "本地英译中模型尚未安装，请先在翻译引擎设置中下载。")
    dependency_status = getattr(manager, "dependency_status", None)
    if dependency_status is not None:
        status = dependency_status()
        if not status.get("ready", False):
            missing = ", ".join(status.get("missing", []))
            raise _error(
                409,
                "LOCAL_TRANSLATION_DEPENDENCY_MISSING",
                f"本地翻译依赖未安装：{missing}。请运行 uv sync --extra local-translation。",
            )


def _resolve_engine(
    engine: str,
    need_subtitle: bool,
    engines: Any,
    *,
    model_store: ModelSettingsStore,
    compat_settings: Any = default_settings,
    translation_model_manager: Any = None,
) -> dict[str, Any]:
    if not need_subtitle:
        return {"source": "disabled", "id": engine, "provider": "", "model": "", "baseUrl": "", "params": {}}

    if engine == "deepseek":
        card = active_model_card(model_store, "llm")
        if card is not None:
            provider = str(card.get("provider") or "").strip().lower()
            model = str(card.get("model") or "").strip()
            if provider not in _LLM_PROVIDERS:
                raise _error(422, "UNSUPPORTED_LLM_PROVIDER", f"不支持的 LLM Provider: {provider}")
            if not card.get("enabled", True):
                raise _error(422, "MODEL_DISABLED", "统一 LLM 模型已停用")
            if not model:
                raise _error(422, "MODEL_UNSUPPORTED", "统一 LLM 模型未配置")
            if not str(card.get("apiKey") or "").strip():
                raise _error(422, "MODEL_API_KEY_MISSING", "未配置 API Key")
            return {
                "source": "unified_llm", "id": engine, "cardId": _card_id(card),
                "provider": provider, "model": model,
                "baseUrl": str(card.get("baseUrl") or "").strip(),
                "params": _engine_params(card),
            }
        rec = engines.get("deepseek")
        key = (getattr(rec, "api_key", None) if rec else None) or getattr(compat_settings, "deepseek_api_key", None)
        if not str(key or "").strip():
            # Keep the legacy response text stable for existing clients.  The
            # unified card path above returns the structured error code.
            raise HTTPException(
                status_code=422,
                detail="缺少 DeepSeek API Key，请在 .env 配置 SUBTRANS_DEEPSEEK_API_KEY",
            )
        return {
            "source": "legacy", "id": engine, "provider": "deepseek",
            "model": str(getattr(rec, "model", None) or getattr(compat_settings, "deepseek_model", "deepseek-chat")),
            "baseUrl": str(getattr(rec, "base_url", None) or getattr(compat_settings, "deepseek_base_url", "https://api.deepseek.com")),
            "params": _engine_params({}),
        }

    rec = engines.get(engine)
    if rec is None:
        raise _error(422, "ENGINE_NOT_FOUND", "翻译引擎配置不存在")
    if not rec.enabled:
        raise _error(422, "ENGINE_DISABLED", "翻译引擎已停用")
    if rec.api_type == "local_ct2":
        _check_local_translation(compat_settings, translation_model_manager)
    elif not rec.api_key or not rec.api_key.strip():
        # Preserve the historic text response for translation-engine cards;
        # unified model cards use the structured code above.
        raise HTTPException(status_code=422, detail="翻译引擎尚未配置 API Key")
    elif rec.availability != "AVAILABLE":
        raise HTTPException(status_code=422, detail="翻译引擎尚未通过可用性检测")
    provider = "local" if rec.api_type == "local_ct2" else rec.api_type
    return {
        "source": "translation_store", "id": engine, "provider": provider,
        "model": rec.model, "baseUrl": rec.base_url, "params": {},
    }


def _resolve_whisper(
    request_model: str,
    *,
    need_subtitle: bool,
    model_store: ModelSettingsStore,
    ensure_local_model_ready: Callable[[str], None] | None,
) -> dict[str, Any]:
    if not need_subtitle:
        return {
            "source": "disabled", "cardId": None, "provider": "",
            "model": str(request_model or ""),
        }
    card = active_model_card(model_store, "whisper")
    source = "unified_whisper" if card is not None else "request"
    selected = str((card or {}).get("model") or request_model).strip()
    provider = str((card or {}).get("provider") or "").strip().lower()
    if card is not None and not card.get("enabled", True):
        raise _error(422, "MODEL_DISABLED", "统一 Whisper 模型已停用")
    if card is None:
        prefix, sep, _ = selected.partition(":")
        provider = prefix.lower() if sep else "local"
    if provider == "replicate":
        raise _error(422, "REPLICATE_BACKEND_REMOVED", "Replicate 云端转写已移除，请选择已就绪的本地 Whisper 模型")
    if provider in {"local", "local_whisper", "whisper", "faster_whisper"}:
        check_model = selected
        if provider == "local" and not check_model.lower().startswith("local:"):
            check_model = f"local:{check_model}"
        if ensure_local_model_ready is not None:
            ensure_local_model_ready(check_model)
        elif provider == "local":
            from src.service.model_manager import model_manager
            if not model_manager.is_ready(selected.removeprefix("local:")):
                raise _error(409, "MODEL_NOT_READY", f"本地模型未导入或未通过检查: {selected.removeprefix('local:')}")
    elif provider:
        raise _error(422, "UNSUPPORTED_WHISPER_PROVIDER", f"不支持的 Whisper Provider: {provider}")
    if card is not None and provider == "local" and not selected.lower().startswith("local:"):
        selected = f"local:{selected}"
    return {"source": source, "cardId": _card_id(card), "provider": provider or "local", "model": selected}


def _validate_tts_voice(voice: str, target_lang: str) -> None:
    if not target_lang.lower().startswith(("zh", "en")):
        raise _error(422, "UNSUPPORTED_TTS_LANGUAGE", "Kokoro 配音目前支持中文和英文目标语")
    supported_voices = {"auto", "zf_001", "zm_010", "af_maple", "af_sol"}
    if voice not in supported_voices:
        raise _error(422, "UNSUPPORTED_TTS_VOICE", f"不支持的 Kokoro 音色: {voice}")
    if voice in {"zf_001", "zm_010"} and not target_lang.lower().startswith("zh"):
        raise _error(422, "UNSUPPORTED_TTS_VOICE", "中文音色只能用于中文目标语")
    if voice in {"af_maple", "af_sol"} and not target_lang.lower().startswith("en"):
        raise _error(422, "UNSUPPORTED_TTS_VOICE", "英文音色只能用于英文目标语")


def _resolve_tts(
    enabled: bool,
    request_voice: str,
    *,
    model_store: ModelSettingsStore,
    target_lang: str,
) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False, "source": "request", "cardId": None, "provider": "", "model": "", "voice": request_voice, "speed": 1.0, "format": "wav"}
    raw_config = model_store.get("tts") or {}
    if raw_config.get("_runtimeManaged") and not raw_config.get("enabled", True):
        raise _error(422, "TTS_DISABLED", "统一 TTS 模型已停用")
    card = active_model_card(model_store, "tts")
    if card is not None:
        if not card.get("enabled", True):
            raise _error(422, "TTS_DISABLED", "统一 TTS 模型已停用")
        provider = str(card.get("provider") or "").strip().lower()
        if provider not in _TTS_PROVIDERS:
            raise _error(422, "UNSUPPORTED_TTS_PROVIDER", f"不支持的 TTS Provider: {provider}")
        if provider in {"kokoro", "local"} and importlib.util.find_spec("kokoro") is None:
            raise _error(409, "TTS_RUNTIME_NOT_READY", "Kokoro 依赖未安装")
        voice = str(card.get("voice") or request_voice or "auto")
        _validate_tts_voice(voice, target_lang)
        try:
            speed = float(card.get("speed") or 1.0)
        except (TypeError, ValueError) as exc:
            raise _error(422, "INVALID_TTS_SPEED", "Kokoro 语速必须是数字") from exc
        if speed <= 0 or speed > 4:
            raise _error(422, "INVALID_TTS_SPEED", "Kokoro 语速必须在 0 到 4 之间")
        output_format = str(card.get("format") or "wav").lower()
        if output_format != "wav":
            raise _error(422, "UNSUPPORTED_TTS_FORMAT", "当前 Kokoro 配音只支持 wav 输出格式")
        return {
            "enabled": True, "source": "unified_tts", "cardId": _card_id(card),
            "provider": provider, "model": str(card.get("model") or ""),
            "voice": voice, "speed": speed,
            "format": output_format,
        }
    voice = str(request_voice or "auto")
    _validate_tts_voice(voice, target_lang)
    return {"enabled": True, "source": "request", "cardId": None, "provider": "kokoro", "model": None, "voice": voice, "speed": 1.0, "format": "wav"}


@dataclass(frozen=True)
class RuntimePlan:
    snapshot: dict[str, Any]


def resolve_runtime(
    *,
    engine: str,
    need_subtitle: bool,
    request_model: str,
    source_lang: str,
    target_lang: str,
    tts_enabled: bool,
    tts_voice: str,
    engines: Any,
    model_store: ModelSettingsStore | None = None,
    compat_settings: Any = default_settings,
    ensure_local_model_ready: Callable[[str], None] | None = None,
    translation_model_manager: Any = None,
) -> RuntimePlan:
    model_store = model_store or ModelSettingsStore(getattr(compat_settings, "db_path", default_settings.db_path))
    engine_snapshot = _resolve_engine(
        engine, need_subtitle, engines, model_store=model_store,
        compat_settings=compat_settings, translation_model_manager=translation_model_manager,
    )
    whisper_snapshot = _resolve_whisper(
        request_model,
        need_subtitle=need_subtitle,
        model_store=model_store,
        ensure_local_model_ready=ensure_local_model_ready,
    )
    tts_snapshot = _resolve_tts(tts_enabled, tts_voice, model_store=model_store, target_lang=target_lang)
    return RuntimePlan({
        "schemaVersion": _SNAPSHOT_VERSION,
        "engine": engine_snapshot,
        "whisper": whisper_snapshot,
        "tts": tts_snapshot,
    })


def resolve_engine(
    engine: str,
    need_subtitle: bool,
    engines: Any,
    *,
    model_store: ModelSettingsStore | None = None,
    compat_settings: Any = default_settings,
    translation_model_manager: Any = None,
) -> dict[str, Any]:
    """Validate and resolve only the translation engine compatibility helper."""
    model_store = model_store or ModelSettingsStore(getattr(compat_settings, "db_path", default_settings.db_path))
    return _resolve_engine(
        engine, need_subtitle, engines, model_store=model_store,
        compat_settings=compat_settings, translation_model_manager=translation_model_manager,
    )
