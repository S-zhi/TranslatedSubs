"""任务执行器（方案 A：后台线程池）。

API 层 POST/retry 时调 enqueue_pipeline(task_id)，本模块把任务丢进线程池异步执行，
HTTP 请求立即返回。执行过程中通过 on_event 把状态/进度写回 SQLite，
SSE 端点轮询库表即可拿到实时进度。

把"执行"隔离在这一处：API 层不感知用线程还是队列，
将来换 RQ + Redis 只改本文件的 enqueue_pipeline / 提交方式。
"""

from __future__ import annotations

import logging
import os
import inspect
import json
import math
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Optional

from src.config import OUTPUT_VIDEO_NAMES, artifact_name, settings, task_dir
from src.config.config import settings_scope
from src.config.config_repository import get_config_provider
from src.config.schema import BUSINESS_DEFAULTS, SECRET_KEYS, validate_business_value
from src.service.orchestrator import (
    PipelineEvent,
    PipelineParams,
    PipelineCancelledError,
    is_cancelled_signal,
    register_cancellation_signal,
    run_pipeline,
    set_cancelled_signal,
    unregister_cancellation_signal,
)
from src.service.asset_resolver import AssetResolver, ResourceError, ResourceState
from src.service.plugin_plan import ensure_plan, parse_plugin_plan
from src.plugin_system import PluginError
from src.store import MODEL_KINDS, ModelSettingsStore, STATUSES, TaskStore, TranslationEngineStore

logger = logging.getLogger(__name__)

# 线程池状态（延迟/懒构造 + 动态扩缩容）
_executor: ThreadPoolExecutor | None = None
_current_max_workers: int = 0
_executor_lock = threading.Lock()

_store = TaskStore(settings.db_path)


def get_executor() -> ThreadPoolExecutor:
    """获取或平滑重构后台任务线程池。

    当 settings.pipeline_workers 发生变化时，关闭旧线程池（等已有任务跑完），
    并按新配置创建新线程池。
    """
    global _executor, _current_max_workers
    target_workers = settings.pipeline_workers
    with _executor_lock:
        if _executor is None or (
            hasattr(_executor, "_max_workers") and _current_max_workers != target_workers
        ):
            old_executor = _executor
            _executor = ThreadPoolExecutor(
                max_workers=target_workers,
                thread_name_prefix="pipeline",
            )
            _current_max_workers = target_workers
            if old_executor is not None and hasattr(old_executor, "shutdown"):
                try:
                    old_executor.shutdown(wait=False)
                except Exception as e:
                    logger.warning("关闭旧线程池失败: %s", e)
            logger.info("流水线线程池就绪/已更新: max_workers=%d", target_workers)
        return _executor


def shutdown_executor(wait: bool = True) -> None:
    """关闭当前线程池。"""
    global _executor, _current_max_workers
    with _executor_lock:
        if _executor is not None:
            if hasattr(_executor, "shutdown"):
                try:
                    _executor.shutdown(wait=wait)
                except Exception as e:
                    logger.warning("关闭线程池失败: %s", e)
            _executor = None
            _current_max_workers = 0
            logger.info("流水线线程池已关闭")
_RECOVERABLE_STATUSES = set(STATUSES) - {"SUCCESS", "FAILED", "CANCELLED"}
_engine_store: TranslationEngineStore | None = None


def _get_engine_store() -> TranslationEngineStore:
    global _engine_store
    if _engine_store is None:
        _engine_store = TranslationEngineStore(settings.db_path)
    return _engine_store


def _active_unified_model(kind: str, db_path=None, *, snapshot=None) -> dict | None:
    """Read a user-saved current model at task start.

    Default rows are intentionally passive so existing tasks keep their
    persisted model until the unified settings page has explicitly saved a
    configuration.  This also keeps old installations and test doubles
    compatible while making every subsequent task use the selected card.
    """
    if kind not in MODEL_KINDS:
        return None
    config = ModelSettingsStore(db_path or settings.db_path, snapshot=snapshot).get(kind) or {}
    if not config.get("_runtimeManaged") or not config.get("enabled", True):
        return None
    models = [item for item in config.get("models", []) if isinstance(item, dict)]
    active = next((item for item in models if item.get("isCurrent")), None)
    merged = dict(config)
    # The per-card identity owns its credential. Never inherit a key from the
    # category-level compatibility field when the selected card has none.
    merged.pop("apiKey", None)
    if active:
        merged.update(active)
    return merged


def _llm_engine_config(item: dict) -> object | None:
    provider = str(item.get("provider") or "").strip().lower()
    api_key = str(item.get("apiKey") or "").strip()
    if not api_key:
        return None
    if provider in {"anthropic", "anthropic_compatible"}:
        api_type = "anthropic_compatible"
    elif provider in {"openai", "openai_compatible", "deepseek", "custom"}:
        api_type = "openai_compatible"
    else:
        return None
    return SimpleNamespace(
        api_type=api_type,
        base_url=str(item.get("baseUrl") or "").strip(),
        model=str(item.get("model") or "").strip(),
        api_key=api_key,
        timeout=int(item.get("timeout") or 120),
        temperature=float(item.get("temperature") or 0.3),
        max_tokens=int(item.get("maxTokens") or 4096),
        concurrency=max(1, int(item.get("concurrency") or 1)),
    )


def _snapshot_engine_config(snapshot: dict, *, engine_store=None, model_store=None) -> object | None:
    """Build an engine config from a persisted snapshot and current secret."""
    source = str(snapshot.get("source") or "")
    engine_id = str(snapshot.get("id") or "")
    if source == "disabled":
        return SimpleNamespace(
            api_type="disabled", base_url="", model="", api_key=None,
            timeout=120, temperature=0.3, max_tokens=4096, concurrency=1,
        )
    key = ""
    if source == "unified_llm":
        config = (model_store or ModelSettingsStore()).get("llm") or {}
        card_id = snapshot.get("cardId")
        card = next(
            (item for item in config.get("models", [])
             if isinstance(item, dict) and str(item.get("id")) == str(card_id)
             ),
            None,
        )
        if card is None or not card.get("enabled", True) or not config.get("enabled", True):
            return None
        item = card
        key = str(item.get("apiKey") or "").strip()
    else:
        rec = (engine_store or _get_engine_store()).get(engine_id)
        if rec is None or not rec.enabled:
            return None
        key = str(getattr(rec, "api_key", None) or "").strip()
    if not key and str(snapshot.get("provider") or "").lower() != "local":
        return None
    params = snapshot.get("params") or {}
    provider = str(snapshot.get("provider") or "").lower()
    api_type = "anthropic_compatible" if provider in {"anthropic", "anthropic_compatible"} else (
        "local_ct2" if provider == "local" else "openai_compatible"
    )
    return SimpleNamespace(
        api_type=api_type,
        base_url=str(snapshot.get("baseUrl") or ""),
        model=str(snapshot.get("model") or ""),
        api_key=key or None,
        timeout=int(params.get("timeout") or 120),
        temperature=float(params.get("temperature") or 0.3),
        max_tokens=int(params.get("maxTokens") or 4096),
        concurrency=max(1, int(params.get("concurrency") or 1)),
    )


def _snapshot_invalid(condition: bool, field: str) -> None:
    if condition:
        raise ValueError(f"invalid runtime snapshot field: {field}")


def _validate_runtime_snapshot(snapshot: object) -> dict:
    """Validate a complete v2 task snapshot before constructing pipeline args.

    Keep errors field-only: persisted snapshots can be manually edited and we
    must not echo an accidentally embedded credential into task error output.
    """
    _snapshot_invalid(not isinstance(snapshot, dict), "root")
    assert isinstance(snapshot, dict)
    _snapshot_invalid(type(snapshot.get("schemaVersion")) is not int or snapshot["schemaVersion"] != 2, "schemaVersion")
    revision = snapshot.get("configRevision")
    _snapshot_invalid(type(revision) is not int or revision < 0, "configRevision")
    business = snapshot.get("business")
    _snapshot_invalid(not isinstance(business, dict), "business")
    for key, value in business.items():
        _snapshot_invalid(key in SECRET_KEYS, f"business.{key}")
        try:
            validate_business_value(key, value)
        except (TypeError, ValueError):
            _snapshot_invalid(True, f"business.{key}")

    engine = snapshot.get("engine")
    _snapshot_invalid(not isinstance(engine, dict), "engine")
    _snapshot_invalid(engine.get("source") not in {"disabled", "unified_llm", "translation_store"}, "engine.source")
    _snapshot_invalid(not isinstance(engine.get("id"), str) or not engine["id"].strip(), "engine.id")
    for key in ("provider", "model", "baseUrl"):
        _snapshot_invalid(not isinstance(engine.get(key), str), f"engine.{key}")
    params = engine.get("params")
    _snapshot_invalid(not isinstance(params, dict), "engine.params")
    _snapshot_invalid(set(params) - {"temperature", "maxTokens", "timeout", "concurrency"} != set(), "engine.params")
    for key, min_value, max_value, integer in (
        ("temperature", 0, 2, False), ("maxTokens", 1, 1_000_000, True),
        ("timeout", 1, 86_400, True), ("concurrency", 1, 256, True),
    ):
        if key not in params:
            continue
        value = params.get(key)
        _snapshot_invalid(isinstance(value, bool) or not isinstance(value, (int, float)), f"engine.params.{key}")
        _snapshot_invalid(not math.isfinite(value) or not min_value <= value <= max_value, f"engine.params.{key}")
        if integer:
            _snapshot_invalid(type(value) is not int, f"engine.params.{key}")
    source = engine["source"]
    _snapshot_invalid(source == "unified_llm" and (
        not isinstance(engine.get("cardId"), str) or not engine["cardId"].strip()
    ), "engine.cardId")
    _snapshot_invalid(source != "disabled" and not engine["model"].strip(), "engine.model")

    whisper = snapshot.get("whisper")
    _snapshot_invalid(not isinstance(whisper, dict), "whisper")
    _snapshot_invalid(whisper.get("source") not in {"disabled", "unified_whisper", "request"}, "whisper.source")
    _snapshot_invalid(not isinstance(whisper.get("model"), str), "whisper.model")
    _snapshot_invalid(whisper.get("source") != "disabled" and not whisper["model"].strip(), "whisper.model")
    _snapshot_invalid(not isinstance(whisper.get("provider"), str), "whisper.provider")
    _snapshot_invalid(whisper.get("cardId") is not None and not isinstance(whisper.get("cardId"), str), "whisper.cardId")
    _snapshot_invalid(whisper.get("source") == "unified_whisper" and not whisper.get("cardId"), "whisper.cardId")

    tts = snapshot.get("tts")
    _snapshot_invalid(not isinstance(tts, dict), "tts")
    _snapshot_invalid(type(tts.get("enabled")) is not bool, "tts.enabled")
    _snapshot_invalid(tts.get("source") not in {"request", "unified_tts"}, "tts.source")
    _snapshot_invalid(tts.get("cardId") is not None and not isinstance(tts.get("cardId"), str), "tts.cardId")
    _snapshot_invalid(tts.get("source") == "unified_tts" and not tts.get("cardId"), "tts.cardId")
    for key in ("provider", "voice", "format"):
        _snapshot_invalid(not isinstance(tts.get(key), str), f"tts.{key}")
    model = tts.get("model")
    _snapshot_invalid(model is not None and not isinstance(model, str), "tts.model")
    speed = tts.get("speed")
    _snapshot_invalid(isinstance(speed, bool) or not isinstance(speed, (int, float)), "tts.speed")
    _snapshot_invalid(not math.isfinite(speed) or not 0 < speed <= 4, "tts.speed")
    _snapshot_invalid(tts.get("format") != "wav", "tts.format")
    return snapshot


def _validate_legacy_snapshot(snapshot: object) -> dict:
    if not isinstance(snapshot, dict):
        raise ValueError("invalid runtime snapshot field: root")
    version = snapshot.get("schemaVersion", 1)
    if type(version) is not int or version != 1:
        raise ValueError("invalid runtime snapshot field: schemaVersion")
    business = snapshot.get("business")
    if business is not None:
        if not isinstance(business, dict):
            raise ValueError("invalid runtime snapshot field: business")
        for key, value in business.items():
            if key in SECRET_KEYS:
                raise ValueError(f"invalid runtime snapshot field: business.{key}")
            try:
                validate_business_value(key, value)
            except (TypeError, ValueError):
                raise ValueError(f"invalid runtime snapshot field: business.{key}") from None
    for route in ("engine", "whisper", "tts"):
        value = snapshot.get(route)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"invalid runtime snapshot field: {route}")
    engine = snapshot.get("engine")
    if isinstance(engine, dict):
        for key in ("source", "id", "provider", "model", "baseUrl"):
            if key in engine and not isinstance(engine[key], str):
                raise ValueError(f"invalid runtime snapshot field: engine.{key}")
        if "params" in engine and not isinstance(engine["params"], dict):
            raise ValueError("invalid runtime snapshot field: engine.params")
        for key in ("temperature", "maxTokens", "timeout", "concurrency"):
            value = (engine.get("params") or {}).get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError(f"invalid runtime snapshot field: engine.params.{key}")
    whisper = snapshot.get("whisper")
    if isinstance(whisper, dict):
        for key in ("source", "provider", "model", "cardId"):
            if key in whisper and whisper[key] is not None and not isinstance(whisper[key], str):
                raise ValueError(f"invalid runtime snapshot field: whisper.{key}")
    tts = snapshot.get("tts")
    if isinstance(tts, dict):
        if "enabled" in tts and not isinstance(tts["enabled"], bool):
            raise ValueError("invalid runtime snapshot field: tts.enabled")
        if "speed" in tts:
            speed = tts["speed"]
            if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or not 0 < speed <= 4:
                raise ValueError("invalid runtime snapshot field: tts.speed")
        for key in ("source", "provider", "model", "voice", "format", "cardId"):
            if key in tts and tts[key] is not None and not isinstance(tts[key], str):
                raise ValueError(f"invalid runtime snapshot field: tts.{key}")
    revision = snapshot.get("configRevision")
    if revision is not None and (type(revision) is not int or revision < 0):
        raise ValueError("invalid runtime snapshot field: configRevision")
    return snapshot


def _upgrade_legacy_snapshot(snapshot: dict, rec, config_bundle, plugin_plan=None) -> dict:
    """Complete a null/v1 snapshot once, retaining already saved routing."""
    from src.service.model_runtime import _resolve_engine, _resolve_tts, _resolve_whisper

    engines = TranslationEngineStore(snapshot=config_bundle)
    models = ModelSettingsStore(snapshot=config_bundle)
    routes = dict(snapshot)
    if not isinstance(routes.get("engine"), dict):
        translate = ((plugin_plan or {}).get("slots") or {}).get("translate")
        translate_config = translate.get("config") if isinstance(translate, dict) else None
        if (isinstance(translate, dict)
                and translate.get("plugin_id") == "builtin.translation_engine"
                and isinstance(translate_config, dict)
                and translate_config.get("engine_id")):
            api_type = str(translate_config.get("api_type") or "openai_compatible")
            provider = (
                "local" if api_type == "local_ct2"
                else "anthropic_compatible" if api_type == "anthropic_compatible"
                else "openai_compatible"
            )
            routes["engine"] = {
                "source": "translation_store",
                "id": str(translate_config["engine_id"]),
                "provider": provider,
                "model": str(translate_config.get("model") or ""),
                "baseUrl": str(translate_config.get("base_url") or ""),
                "params": {
                    "temperature": 0.3, "maxTokens": 4096,
                    "timeout": 120, "concurrency": 1,
                },
            }
        elif plugin_plan is not None:
            routes["engine"] = {
                "source": "disabled", "id": str(rec.engine), "provider": "",
                "model": "", "baseUrl": "", "params": {},
            }
        else:
            routes["engine"] = _resolve_engine(
                rec.engine, bool(rec.need_subtitle), engines, model_store=models,
                compat_settings=settings,
            )
    else:
        saved = routes["engine"]
        saved_source = saved.get("source")
        defaults = {
            "source": "translation_store", "id": rec.engine, "provider": "",
            "model": "", "baseUrl": "", "params": {},
        }
        defaults.update(saved)
        if saved_source in {None, "legacy"}:
            defaults["source"] = "translation_store"
        if not isinstance(defaults.get("params"), dict):
            raise ValueError("invalid runtime snapshot field: engine.params")
        defaults["params"] = {
            **{"temperature": 0.3, "maxTokens": 4096, "timeout": 120, "concurrency": 1},
            **defaults["params"],
        }
        routes["engine"] = defaults

    if not isinstance(routes.get("whisper"), dict):
        transcribe = (((plugin_plan or {}).get("slots") or {}).get("transcribe"))
        transcribe_config = transcribe.get("config") if isinstance(transcribe, dict) else None
        if snapshot == {} and isinstance(transcribe_config, dict):
            backend = str(transcribe_config.get("backend") or "local_whisper")
            model = str(transcribe_config.get("model") or rec.model)
            if backend == "local_whisper":
                provider = "local"
                if not model.lower().startswith("local:"):
                    model = f"local:{model}"
            else:
                provider = backend
            routes["whisper"] = {
                "source": "request", "cardId": None,
                "provider": provider, "model": model,
            }
        elif plugin_plan is not None:
            routes["whisper"] = {
                "source": "disabled", "cardId": None,
                "provider": "", "model": "",
            }
        else:
            routes["whisper"] = _resolve_whisper(
                rec.model, need_subtitle=bool(rec.need_subtitle), model_store=models,
                ensure_local_model_ready=lambda _model: None,
            )
            if routes["whisper"].get("source") == "request":
                # Keep the task's legacy model alias stable across migration
                # and retry; the backend field already captures its provider.
                routes["whisper"]["model"] = str(rec.model)
    else:
        saved = routes["whisper"]
        model = saved.get("model") or rec.model
        prefix, separator, _ = str(model).partition(":")
        provider = saved.get("provider") or (prefix if separator else "local")
        routes["whisper"] = {
            "source": "request", "cardId": None, "provider": provider,
            "model": model, **saved,
        }

    if not isinstance(routes.get("tts"), dict):
        tts_slot = (((plugin_plan or {}).get("slots") or {}).get("tts"))
        tts_config = tts_slot.get("config") if isinstance(tts_slot, dict) else None
        if snapshot == {} and isinstance(tts_config, dict):
            routes["tts"] = {
                "enabled": True, "source": "request", "cardId": None,
                "provider": "kokoro", "model": tts_config.get("model_id"),
                "voice": str(tts_config.get("voice") or rec.tts_voice or "auto"),
                "speed": float(tts_config.get("speed", 1.0)),
                "format": str(tts_config.get("output_format") or "wav"),
            }
        elif plugin_plan is not None:
            routes["tts"] = {
                "enabled": False, "source": "request", "cardId": None,
                "provider": "", "model": None, "voice": "auto",
                "speed": 1.0, "format": "wav",
            }
        else:
            routes["tts"] = _resolve_tts(
                bool(rec.tts_enabled), rec.tts_voice or "auto", model_store=models,
                target_lang=rec.target_lang,
            )
    else:
        saved = routes["tts"]
        routes["tts"] = {
            "enabled": bool(rec.tts_enabled), "source": "request", "cardId": None,
            "provider": "kokoro", "model": None, "voice": rec.tts_voice or "auto",
            "speed": 1.0, "format": "wav", **saved,
        }

    from src.config import settings as config_settings
    capture_business_snapshot = getattr(settings, "capture_business_snapshot", None)
    if not callable(capture_business_snapshot):
        capture_business_snapshot = config_settings.capture_business_snapshot
    business = capture_business_snapshot(config_snapshot=config_bundle)
    if isinstance(snapshot.get("business"), dict):
        business.update(snapshot["business"])
    routes["schemaVersion"] = 2
    routes["configRevision"] = config_bundle.revision
    routes["business"] = business
    return _validate_runtime_snapshot(routes)

_procs: dict[str, list[subprocess.Popen]] = {}
_procs_lock = threading.Lock()
_task_state_locks: dict[str, threading.RLock] = {}
_task_state_locks_lock = threading.Lock()


def _task_state_lock(task_id: str) -> threading.RLock:
    with _task_state_locks_lock:
        return _task_state_locks.setdefault(task_id, threading.RLock())


def register_process(task_id: str, proc: subprocess.Popen) -> None:
    """注册运行中的子进程（如 ffmpeg），以便任务取消时终止。"""
    with _procs_lock:
        _procs.setdefault(task_id, []).append(proc)


def unregister_process(task_id: str, proc: subprocess.Popen) -> None:
    """移除已退出的子进程。"""
    with _procs_lock:
        if task_id in _procs:
            try:
                _procs[task_id].remove(proc)
            except ValueError:
                pass
            if not _procs[task_id]:
                del _procs[task_id]


def _terminate_process(proc: subprocess.Popen, task_id: Optional[str] = None) -> None:
    """平滑终止子进程：优先发送 SIGTERM，超时则补发 SIGKILL 确保子进程真正退出。"""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning("子进程(PID %s)响应 SIGTERM 超时，发送 SIGKILL: task=%s", getattr(proc, "pid", None), task_id)
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception as e:
            logger.warning("发送 SIGKILL 终止子进程失败: PID=%s, task=%s, err=%s", getattr(proc, "pid", None), task_id, e)
    except Exception as e:
        logger.warning("终止子进程失败: PID=%s, task=%s, err=%s", getattr(proc, "pid", None), task_id, e)


def _cleanup_partial_artifacts(task_id: str) -> None:
    """清理 yt-dlp 和本次插件调用留下的临时文件，不删已发布产物。"""
    d = task_dir(task_id)
    if not d.exists():
        return

    try:
        for p in d.iterdir():
            if p.is_file():
                name_lower = p.name.lower()
                if (
                    name_lower.endswith(".part")
                    or name_lower.endswith(".ytdl")
                    or name_lower.endswith(".tmp")
                    or name_lower.startswith("tmp_")
                ):
                    try:
                        p.unlink()
                        logger.info("已清理取消任务的临时文件: task=%s, path=%s", task_id, p)
                    except Exception as e:
                        logger.warning("清理临时文件失败: task=%s, path=%s, err=%s", task_id, p, e)
    except Exception as e:
        logger.warning("清理任务临时文件目录失败: task=%s, err=%s", task_id, e)


def cancel_pipeline(task_id: str) -> bool:
    """取消运行中的任务，终止其关联子进程，清理半截产物并更新状态为 CANCELLED。"""
    lock = _task_state_lock(task_id)
    with lock:
        set_cancelled_signal(task_id)
        rec = _store.get(task_id)
        if rec is None or rec.status in {"SUCCESS", "FAILED", "CANCELLED"}:
            return False
        _store.update(task_id, is_cancelling=1)
        _store.update(task_id, status="CANCELLED", error="用户取消", error_code="cancelled")
    try:
        with _procs_lock:
            procs = _procs.pop(task_id, [])

        for proc in procs:
            _terminate_process(proc, task_id=task_id)

        _cleanup_partial_artifacts(task_id)

        for proc in procs:
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception as e:
                    logger.warning("强制杀死子进程失败: task=%s, err=%s", task_id, e)

        logger.info("任务已取消并清理产物: %s", task_id)
        return True
    finally:
        # 删除接口依赖此标记避开整个取消清理窗口；异常也不能留下永久阻塞。
        _store.update(task_id, is_cancelling=0)


def enqueue_pipeline(task_id: str) -> None:
    """提交一个任务去后台执行（不阻塞调用方）。"""
    get_executor().submit(_run, task_id)
    logger.info("已入队: %s", task_id)


def recover_interrupted_tasks() -> list[str]:
    """服务启动时重新提交未完成任务，避免任务永久停留在处理中。

    对于 upload 模式且缺少有效源视频文件的任务，直接置为 FAILED 并跳过重投。
    """
    recovered: list[str] = []
    for rec in _store.list():
        if rec.status not in _RECOVERABLE_STATUSES:
            if rec.status in {"SUCCESS", "FAILED", "CANCELLED"}:
                from src.service.subtitle_reburn import discard_subtitle_publication
                try:
                    discard_subtitle_publication(rec.id)
                except Exception:
                    logger.warning("字幕已提交日志无法清理，保留备份: task=%s", rec.id, exc_info=True)
            continue
        if str(rec.current_step or "").startswith(("reburn:", "subtitle_edit:")):
            from src.service.subtitle_reburn import recover_subtitle_publication
            try:
                publication = recover_subtitle_publication(rec.id, expected_token=rec.current_step)
                if publication is not None:
                    previous = publication["previous"]
                    _store.finish_operation(
                        rec.id, rec.current_step, status=previous["status"],
                        progress=previous["progress"], current_step=previous["current_step"],
                    )
                else:
                    _store.finish_operation(
                        rec.id, rec.current_step, status="FAILED", current_step=None,
                        error="字幕操作因服务重启中断，请重新保存或烧录字幕",
                        error_code="subtitle_operation_interrupted",
                    )
            except Exception:
                # Keep the claim while recovery is incomplete. Turning it into
                # a terminal row could make the next startup discard its backups.
                logger.exception("字幕操作发布日志恢复失败，保留占用令牌、日志与备份: task=%s", rec.id)
            logger.warning("已处理重启中断的字幕操作，不重投完整流水线: task=%s", rec.id)
            continue
        if str(rec.model or "").partition(":")[0].strip().lower() == "replicate":
            _store.update(
                rec.id,
                status="FAILED",
                error="该任务使用已移除的 Replicate 云端转写，请新建任务并选择本地 Whisper 模型",
                error_code="REPLICATE_BACKEND_REMOVED",
            )
            logger.warning("恢复中断任务时发现已移除的 Replicate 模型，标记为 FAILED: task=%s", rec.id)
            continue
        if rec.source_type == "upload":
            state, _, msg = AssetResolver.resolve_source(rec.id)
            if state != ResourceState.AVAILABLE:
                _store.update(
                    rec.id,
                    status="FAILED",
                    error="上传源文件缺失或损坏",
                    error_code="resource_error",
                )
                logger.warning("恢复中断任务时发现上传源文件缺失或损坏，标记为 FAILED: task=%s, msg=%s", rec.id, msg)
                continue
        enqueue_pipeline(rec.id)
        recovered.append(rec.id)
    return recovered


def _run(task_id: str) -> None:
    """线程内执行：读记录 → 跑五步 → 进度写库。"""
    with _task_state_lock(task_id):
        rec = _store.get(task_id)
        if rec is None:
            logger.warning("任务不存在，跳过执行: %s", task_id)
            return
        # Serialize registration with cancel_pipeline so a cancellation cannot
        # be erased between the initial row read and Event registration.
        if rec.status == "CANCELLED":
            logger.info("任务已被取消，跳过执行: %s", task_id)
            unregister_cancellation_signal(task_id)
            return
        register_cancellation_signal(task_id)

    def fail_invalid_snapshot() -> None:
        _store.update(
            task_id, status="FAILED", error="任务配置快照损坏或字段无效",
            error_code="runtime_snapshot_invalid",
        )
        _cleanup_partial_artifacts(task_id)
        unregister_cancellation_signal(task_id)

    try:
        runtime_snapshot = json.loads(rec.runtime_config_json) if rec.runtime_config_json else None
        if runtime_snapshot is not None:
            version = runtime_snapshot.get("schemaVersion", 1) if isinstance(runtime_snapshot, dict) else None
            if version == 2:
                runtime_snapshot = _validate_runtime_snapshot(runtime_snapshot)
            else:
                runtime_snapshot = _validate_legacy_snapshot(runtime_snapshot)
    except (TypeError, ValueError):
        fail_invalid_snapshot()
        return
    runtime_snapshot_missing = runtime_snapshot is None

    try:
        config_bundle = get_config_provider().read()
    except Exception as exc:
        logger.exception("读取 Redis 配置失败，无法执行任务: %s", task_id)
        _store.update(
            task_id, status="FAILED", error="Redis 配置存储不可用，无法执行任务",
            error_code="config_unavailable",
        )
        unregister_cancellation_signal(task_id)
        return

    existing_plugin_plan = None
    if rec.plugin_plan_json:
        try:
            existing_plugin_plan = parse_plugin_plan(rec.plugin_plan_json)
        except Exception:
            logger.warning("任务插件计划不可用: task=%s code=plugin_plan_invalid", task_id)
            _store.update(
                task_id, status="FAILED", error="任务插件计划不可用",
                error_code="plugin_plan_invalid",
            )
            unregister_cancellation_signal(task_id)
            return

    # Null/v1 rows and any missing plugin plan are completed from one aggregate
    # read. Retries of the resulting v2 row never resolve routing from live settings.
    if runtime_snapshot is None or runtime_snapshot.get("schemaVersion", 1) == 1:
        try:
            runtime_snapshot = _upgrade_legacy_snapshot(
                runtime_snapshot or {}, rec, config_bundle,
                plugin_plan=existing_plugin_plan if runtime_snapshot_missing else None,
            )
        except (TypeError, ValueError):
            fail_invalid_snapshot()
            return
        except Exception:
            logger.exception("旧任务快照无法补全: task=%s", task_id)
            _store.update(
                task_id, status="FAILED", error="旧任务配置快照无法补全，请重新创建任务",
                error_code="runtime_snapshot_upgrade_failed",
            )
            unregister_cancellation_signal(task_id)
            return
        encoded_snapshot = json.dumps(runtime_snapshot, ensure_ascii=False, separators=(",", ":"))
        _store.update(task_id, runtime_config_json=encoded_snapshot)
        rec.runtime_config_json = encoded_snapshot
    try:
        _validate_runtime_snapshot(runtime_snapshot)
    except (TypeError, ValueError):
        fail_invalid_snapshot()
        return

    has_model_snapshot = True
    task_db_path = getattr(_store, "db_path", None) or getattr(settings, "db_path", None)
    model_snapshot_store = ModelSettingsStore(task_db_path, snapshot=config_bundle)

    legacy_cache = rec.plugin_plan_json is None
    snapshot_engine_store = TranslationEngineStore(snapshot=config_bundle)
    try:
        # Resolve a missing legacy plan under the same business snapshot and
        # Redis aggregate used to complete its runtime routing.
        rec.engine = str((runtime_snapshot.get("engine") or {}).get("id") or rec.engine)
        with settings_scope(runtime_snapshot["business"]):
            ensure_parameters = inspect.signature(ensure_plan).parameters
            if "runtime_snapshot" in ensure_parameters:
                rec = ensure_plan(
                    rec, _store, snapshot_engine_store, settings,
                    runtime_snapshot=runtime_snapshot,
                )
            else:
                rec = ensure_plan(rec, _store, snapshot_engine_store, settings)
        plugin_plan = parse_plugin_plan(rec.plugin_plan_json)
    except Exception:
        code = "plugin_plan_migration_failed" if legacy_cache else "plugin_plan_invalid"
        logger.warning("任务插件计划不可用: task=%s code=%s", task_id, code)
        with _task_state_lock(task_id):
            current = _store.get(task_id)
            if (not is_cancelled_signal(task_id) and current is not None
                    and current.status not in {"CANCELLED", "SUCCESS", "FAILED"}):
                _store.update(
                    task_id, status="FAILED", error="任务插件计划不可用", error_code=code
                )
        unregister_cancellation_signal(task_id)
        return
    whisper_model = (
        _active_unified_model("whisper", task_db_path, snapshot=config_bundle)
        if not has_model_snapshot else None
    )
    tts_model = (
        _active_unified_model("tts", task_db_path, snapshot=config_bundle)
        if not has_model_snapshot else None
    )
    if has_model_snapshot:
        whisper_snapshot = runtime_snapshot.get("whisper") or {}
        selected_whisper = str(whisper_snapshot.get("model") or rec.model)
    else:
        selected_whisper = str((whisper_model or {}).get("model") or rec.model)
        if whisper_model:
            whisper_provider = str(whisper_model.get("provider") or "local").strip().lower()
            if whisper_provider == "local" and not selected_whisper.lower().startswith("local:"):
                selected_whisper = f"local:{selected_whisper}"
            elif whisper_provider not in {"local", "whisper", "faster_whisper"} and ":" not in selected_whisper:
                selected_whisper = f"{whisper_provider}:{selected_whisper}"
    tts_snapshot = runtime_snapshot.get("tts") if has_model_snapshot else None
    params = PipelineParams(
        task_id=rec.id,
        url=rec.url,
        source_lang=rec.source_lang,
        target_lang=rec.target_lang,
        mode=rec.mode,
        burn=str(plugin_plan["slots"].get("render.subtitle", {}).get("config", {}).get("mode") or rec.burn),
        model=selected_whisper,
        engine=rec.engine,
        source_type=rec.source_type,
        need_subtitle=bool(rec.need_subtitle),
        title=rec.title,
        quality=getattr(rec, "quality", None),
        tts_enabled=bool(getattr(rec, "tts_enabled", 0)),
        tts_voice=str((tts_snapshot or {}).get("voice") or (tts_model or {}).get("voice") or getattr(rec, "tts_voice", "auto")),
        tts_speed=float((tts_snapshot or {}).get("speed", 1.0) if tts_snapshot else ((tts_model or {}).get("speed", 1.0) or 1.0)),
        tts_format=str((tts_snapshot or {}).get("format", "wav") if tts_snapshot else ((tts_model or {}).get("format", "wav") or "wav")),
        tts_model=(str((tts_snapshot or {}).get("model") or (tts_model or {}).get("model") or "") or None),
        tts_provider=str((tts_snapshot or {}).get("provider") or (tts_model or {}).get("provider", "kokoro") or "kokoro"),
        original_voice_mode=getattr(rec, "original_voice_mode", "keep"),
    )

    last_state = {
        "status": rec.status,
        "progress": rec.progress,
        "title": rec.title,
    }

    def on_event(ev: PipelineEvent) -> None:
        lock = _task_state_lock(task_id)
        with lock:
            current = _store.get(task_id)
            if is_cancelled_signal(task_id) or current is None or current.status in {"CANCELLED", "SUCCESS", "FAILED"}:
                return
            status_changed = ev.status != last_state["status"]
            progress_decade_changed = (ev.progress // 10) != (last_state["progress"] // 10)
            progress_reached_100 = ev.progress == 100
            is_terminal = ev.status in {"SUCCESS", "FAILED", "CANCELLED"}
            has_extra = (
                ev.title is not None and ev.title != last_state["title"]
            ) or ev.error is not None or ev.error_code is not None or bool(ev.outputs) or ev.tts_status is not None or ev.tts_error is not None

            if not (status_changed or progress_decade_changed or progress_reached_100 or is_terminal or has_extra):
                return

            fields: dict = {
                "status": ev.status,
                "progress": ev.progress,
                "current_step": ev.current_step,
            }
            if ev.title is not None:
                fields["title"] = ev.title
                last_state["title"] = ev.title
            if ev.error is not None:
                fields["error"] = ev.error
            if ev.error_code is not None:
                fields["error_code"] = ev.error_code
            if ev.outputs:
                video = ev.outputs.get("video")
                subtitle = ev.outputs.get("subtitle")
                fields["output_video"] = artifact_name(video) if video else None
                fields["output_subtitle"] = artifact_name(subtitle) if subtitle else None
                dubbed = ev.outputs.get("dubbedVideo")
                fields["output_dubbed_video"] = artifact_name(dubbed) if dubbed else None
            if ev.tts_status is not None:
                fields["tts_status"] = ev.tts_status
            if ev.tts_error is not None:
                fields["tts_error"] = ev.tts_error

            _store.update(task_id, **fields)
            last_state["status"] = ev.status
            last_state["progress"] = ev.progress

    def resolve_credential(ref: str) -> str:
        allowed_refs = {
            value for slot in plugin_plan["slots"].values()
            for key, value in slot["config"].items()
            if key in {"credential_ref", "cookies_ref"} and isinstance(value, str)
        }
        if ref not in allowed_refs:
            raise PluginError("credential_missing", "translate", "host", "credential is not authorized by this task plan")
        if isinstance(ref, str) and ref.startswith("model_card:"):
            from src.service.plugin_selection import resolve_model_credential
            try:
                return resolve_model_credential(ref, config_bundle)
            except ValueError as exc:
                raise PluginError("credential_missing", "translate", "host", str(exc), exc) from exc
        if ref == "settings:transcriber_api_key":
            value = getattr(settings, "transcriber_api_key", None)
            if isinstance(value, str) and value:
                return value
            raise PluginError("credential_missing", "transcribe", "builtin.whisper", "credential is unavailable")
        if ref == "settings:cookies_file":
            value = getattr(settings, "cookies_file", None)
            if isinstance(value, (str, os.PathLike)):
                path = os.fspath(value)
                if path:
                    return path
            raise PluginError("credential_missing", "download", "builtin.ytdlp", "credential is unavailable")
        prefix = "translation_engine:"
        suffix = ":api_key"
        if isinstance(ref, str) and ref.startswith(prefix) and ref.endswith(suffix):
            engine_id = ref[len(prefix):-len(suffix)]
            if engine_id:
                if (
                    engine_config is not None
                    and runtime_snapshot
                    and runtime_snapshot.get("engine", {}).get("id") == engine_id
                    and not (runtime_snapshot_missing and not legacy_cache)
                ):
                    value = getattr(engine_config, "api_key", None)
                else:
                    engine_record = snapshot_engine_store.get(engine_id)
                    value = getattr(engine_record, "api_key", None) if engine_record is not None else None
                if isinstance(value, str) and value:
                    return value
            raise PluginError("credential_missing", "translate", "builtin.translation_engine", "credential is unavailable")
        raise PluginError("invalid_input", "download", "host", "credential reference is not allowed")

    # Routing fields are immutable in new task snapshots. Credentials are
    # deliberately read from the current Redis entry addressed by the saved ID.
    translate_slot = plugin_plan["slots"].get("translate")
    requires_builtin_engine = (
        translate_slot is not None
        and translate_slot["plugin_id"] == "builtin.translation_engine"
    )
    engine_config = None
    instance_plan = plugin_plan.get("schema_version") == 2
    if requires_builtin_engine and not instance_plan:
        engine_config = _snapshot_engine_config(
            runtime_snapshot.get("engine") or {}, engine_store=snapshot_engine_store,
            model_store=model_snapshot_store,
        )
    pipeline_parameters = inspect.signature(run_pipeline).parameters
    supports_plugin_plan = "plugin_plan" in pipeline_parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in pipeline_parameters.values()
    )
    authoritative_plan_over_legacy_snapshot = runtime_snapshot_missing and not legacy_cache
    if requires_builtin_engine and not instance_plan and engine_config is None and not authoritative_plan_over_legacy_snapshot:
        _store.update(
            task_id, status="FAILED", error="翻译引擎配置不存在、已停用或缺少对应 API Key",
            error_code="engine_credentials_missing",
        )
        unregister_cancellation_signal(task_id)
        return
    try:
        pipeline_kwargs = {
            "api_key": None,
        }
        pipeline_kwargs["engine_config"] = engine_config
        # 保持第三方/旧测试注入的简化 runner 可用；正式编排器支持 engine_config。
        from src.plugins import get_plugin_manager
        with settings_scope(runtime_snapshot["business"]):
            if supports_plugin_plan:
                run_pipeline(
                    params, on_event, plugin_manager=get_plugin_manager(),
                    plugin_plan=plugin_plan, credential_resolver=resolve_credential,
                    legacy_cache=legacy_cache,
                )
            elif "engine_config" in pipeline_parameters:
                run_pipeline(params, on_event, **pipeline_kwargs)
            else:
                run_pipeline(params, on_event, api_key=None)
    except ResourceError as e:
        if is_cancelled_signal(task_id):
            logger.info("任务已被取消: %s", task_id)
            return
        logger.error("任务由于资源异常执行失败: %s", task_id)
        with _task_state_lock(task_id):
            current = _store.get(task_id)
            if current is not None and current.status not in {"CANCELLED", "SUCCESS", "FAILED"}:
                _store.update(task_id, status="FAILED", error="任务资源不可用", error_code=getattr(e, "code", "resource_error"))
                last_state["status"] = "FAILED"
    except PipelineCancelledError:
        set_cancelled_signal(task_id)
        with _task_state_lock(task_id):
            current = _store.get(task_id)
            if current is not None and current.status not in {"SUCCESS", "FAILED", "CANCELLED"}:
                _store.update(
                    task_id, status="CANCELLED", error="任务已取消", error_code="cancelled"
                )
                last_state["status"] = "CANCELLED"
        logger.info("流水线协作式取消: task=%s", task_id)
    except Exception as exc:
        if is_cancelled_signal(task_id):
            logger.info("任务已被取消: %s", task_id)
            return
        if isinstance(exc, PluginError):
            err_code, public_error = exc.code, exc.message
            logger.warning("流水线插件失败: task=%s stage=%s code=%s", task_id, exc.stage, exc.code)
        else:
            err_code, public_error = getattr(exc, "code", "execution_error"), "任务执行失败"
            logger.error("流水线执行失败: task=%s code=%s", task_id, err_code)
        with _task_state_lock(task_id):
            current = _store.get(task_id)
            if current is not None and current.status not in {"CANCELLED", "SUCCESS", "FAILED"}:
                _store.update(task_id, status="FAILED", error=public_error, error_code=err_code)
                last_state["status"] = "FAILED"
    finally:
        with _procs_lock:
            remaining_procs = _procs.pop(task_id, [])
        for proc in remaining_procs:
            _terminate_process(proc, task_id=task_id)
        if is_cancelled_signal(task_id):
            _cleanup_partial_artifacts(task_id)
        unregister_cancellation_signal(task_id)
