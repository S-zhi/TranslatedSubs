"""任务执行器（方案 A：后台线程池）。

API 层 POST/retry 时调 enqueue_pipeline(task_id)，本模块把任务丢进线程池异步执行，
HTTP 请求立即返回。执行过程中通过 on_event 把状态/进度写回 SQLite，
SSE 端点轮询库表即可拿到实时进度。

把"执行"隔离在这一处：API 层不感知用线程还是队列，
将来换 RQ + Redis 只改本文件的 enqueue_pipeline / 提交方式。
"""

from __future__ import annotations

import logging
import inspect
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Optional

from src.config import OUTPUT_VIDEO_NAMES, artifact_name, settings, task_dir
from src.service.orchestrator import (
    PipelineEvent,
    PipelineParams,
    is_cancelled_signal,
    register_cancellation_signal,
    run_pipeline,
    set_cancelled_signal,
    unregister_cancellation_signal,
)
from src.service.asset_resolver import AssetResolver, ResourceError, ResourceState
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
_engine_store = TranslationEngineStore(settings.db_path)


def _active_unified_model(kind: str, db_path=None) -> dict | None:
    """Read a user-saved current model at task start.

    Default rows are intentionally passive so existing tasks keep their
    persisted model until the unified settings page has explicitly saved a
    configuration.  This also keeps old installations and test doubles
    compatible while making every subsequent task use the selected card.
    """
    if kind not in MODEL_KINDS:
        return None
    try:
        config = ModelSettingsStore(db_path or settings.db_path).get(kind) or {}
    except Exception:
        logger.exception("读取统一模型配置失败: kind=%s", kind)
        return None
    if not config.get("_runtimeManaged") or not config.get("enabled", True):
        return None
    models = [item for item in config.get("models", []) if isinstance(item, dict)]
    active = next((item for item in models if item.get("isCurrent")), None)
    merged = dict(config)
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


def _snapshot_engine_config(snapshot: dict, *, engine_store=None) -> object | None:
    """Build an engine config from a persisted snapshot and current secret."""
    source = str(snapshot.get("source") or "")
    engine_id = str(snapshot.get("id") or "")
    key = ""
    if source == "unified_llm":
        store_path = getattr(engine_store, "db_path", settings.db_path)
        config = ModelSettingsStore(store_path).get("llm") or {}
        card_id = snapshot.get("cardId")
        card = next(
            (item for item in config.get("models", [])
             if isinstance(item, dict) and str(item.get("id")) == str(card_id)),
            None,
        )
        item = dict(config)
        if card:
            item.update(card)
            if not item.get("apiKey") and config.get("apiKey"):
                item["apiKey"] = config["apiKey"]
        key = str(item.get("apiKey") or "").strip()
    else:
        rec = (engine_store or _engine_store).get(engine_id)
        key = str(getattr(rec, "api_key", None) or "").strip()
        if not key and engine_id == "deepseek":
            key = str(getattr(settings, "deepseek_api_key", None) or "").strip()
    if not key and source not in {"disabled"} and str(snapshot.get("provider") or "").lower() != "local":
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

_procs: dict[str, list[subprocess.Popen]] = {}
_procs_lock = threading.Lock()


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
    """清理任务取消后留下的半截/临时产物（如半截 output.mp4、.part/.ytdl 临时文件等）。"""
    d = task_dir(task_id)
    if not d.exists():
        return

    for output_name in (*OUTPUT_VIDEO_NAMES, "output_dubbed.mp4"):
        out_video = d / output_name
        if out_video.exists():
            try:
                out_video.unlink()
                logger.info("已清理取消任务的半截成品视频: task=%s, path=%s", task_id, out_video)
            except Exception as e:
                logger.warning("清理半截成品视频失败: task=%s, err=%s", task_id, e)

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
    set_cancelled_signal(task_id)

    rec = _store.get(task_id)
    if rec is None:
        return False

    _store.update(task_id, is_cancelling=1)
    try:
        _store.update(task_id, status="CANCELLED", error="用户取消")

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

        AssetResolver.cleanup_cancelled_artifacts(
            task_id,
            current_step=rec.current_step,
            source_type=rec.source_type,
        )

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
    rec = _store.get(task_id)
    if rec is None:
        logger.warning("任务不存在，跳过执行: %s", task_id)
        return

    register_cancellation_signal(task_id, is_cancelled=(rec.status == "CANCELLED"))
    if rec.status == "CANCELLED":
        logger.info("任务已被取消，跳过执行: %s", task_id)
        unregister_cancellation_signal(task_id)
        return

    try:
        runtime_snapshot = json.loads(rec.runtime_config_json) if rec.runtime_config_json else None
    except (TypeError, ValueError):
        runtime_snapshot = None
    task_db_path = getattr(_store, "db_path", settings.db_path)
    whisper_model = (
        _active_unified_model("whisper", task_db_path)
        if runtime_snapshot is None else None
    )
    tts_model = (
        _active_unified_model("tts", task_db_path)
        if runtime_snapshot is None else None
    )
    if runtime_snapshot:
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
    tts_snapshot = (runtime_snapshot or {}).get("tts") if runtime_snapshot else None
    params = PipelineParams(
        task_id=rec.id,
        url=rec.url,
        source_lang=rec.source_lang,
        target_lang=rec.target_lang,
        mode=rec.mode,
        burn=rec.burn,
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
        if is_cancelled_signal(task_id):
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

    # New tasks use the immutable snapshot; legacy rows retain the historical
    # dynamic lookup behavior for backwards compatibility.
    if runtime_snapshot:
        snapshot_engine_store = TranslationEngineStore(getattr(_store, "db_path", settings.db_path))
        engine_config = _snapshot_engine_config(runtime_snapshot.get("engine") or {}, engine_store=snapshot_engine_store)
    else:
        # Historical rows have no snapshot.  Prefer the database that owns the
        # task, then fall back to the process singleton used by old callers.
        task_engine_store = TranslationEngineStore(getattr(_store, "db_path", settings.db_path))
        engine_config = task_engine_store.get(rec.engine) or _engine_store.get(rec.engine)
        # ``deepseek`` is the only legacy alias backed by the unified LLM card.
        # A task's explicit custom/local engine remains authoritative.
        if rec.engine == "deepseek":
            unified_llm = _active_unified_model(
                "llm", getattr(_store, "db_path", settings.db_path)
            )
            if unified_llm is not None:
                engine_config = _llm_engine_config(unified_llm) or engine_config
    if engine_config is None and (runtime_snapshot or rec.engine != "deepseek"):
        _store.update(task_id, status="FAILED", error="翻译引擎配置不存在", error_code="engine_not_found")
        unregister_cancellation_signal(task_id)
        return
    try:
        pipeline_kwargs = {
            "api_key": None,
        }
        pipeline_kwargs["engine_config"] = engine_config
        # 保持第三方/旧测试注入的简化 runner 可用；正式编排器支持 engine_config。
        if "engine_config" in inspect.signature(run_pipeline).parameters:
            run_pipeline(params, on_event, **pipeline_kwargs)
        else:
            run_pipeline(params, on_event, api_key=None)
    except ResourceError as e:
        if is_cancelled_signal(task_id):
            logger.info("任务已被取消: %s", task_id)
            AssetResolver.cleanup_cancelled_artifacts(
                task_id,
                current_step=rec.current_step if rec else None,
                source_type=rec.source_type if rec else "url",
            )
            _store.update(task_id, resource_status="MISSING")
            return
        logger.error("任务由于资源异常执行失败: %s - %s", task_id, str(e))
        if last_state["status"] != "FAILED":
            _store.update(task_id, status="FAILED", error=str(e), error_code=getattr(e, "code", "resource_error"))
            last_state["status"] = "FAILED"
    except Exception as exc:
        if is_cancelled_signal(task_id):
            logger.info("任务已被取消: %s", task_id)
            AssetResolver.cleanup_cancelled_artifacts(
                task_id,
                current_step=rec.current_step if rec else None,
                source_type=rec.source_type if rec else "url",
            )
            _store.update(task_id, resource_status="MISSING")
            return
        logger.exception("流水线执行失败: %s", task_id)
        if last_state["status"] != "FAILED":
            err_code = getattr(exc, "code", "execution_error")
            _store.update(task_id, status="FAILED", error=str(exc) or "执行异常", error_code=err_code)
            last_state["status"] = "FAILED"
    finally:
        with _procs_lock:
            remaining_procs = _procs.pop(task_id, [])
        for proc in remaining_procs:
            _terminate_process(proc, task_id=task_id)
        if is_cancelled_signal(task_id):
            _cleanup_partial_artifacts(task_id)
        unregister_cancellation_signal(task_id)
