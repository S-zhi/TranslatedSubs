"""运行时就绪检查。

该模块只返回脱敏后的能力状态，供健康检查和 MCP 适配层使用；不会返回任何
API Key 的实际内容。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import time
from pathlib import Path
from typing import Any

from src.config import settings
from src.core.ffmpeg_utils import has_subtitles_filter
from src.service.translation_model_manager import get_translation_model_manager
from src.service.replicate_account import query_replicate_balance
from src.store import DEFAULT_TRANSLATION_ENGINE_ID


def _check_binary(command: str) -> str:
    """返回可执行文件状态，不暴露命令行参数或环境变量。"""
    return "available" if shutil.which(command) else "missing"


def _check_writable_directory(path: Path) -> str:
    """确保目录存在并检查当前进程是否可写。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        return "writable" if os.access(path, os.W_OK) else "not_writable"
    except OSError:
        return "unavailable"


def _has_value(value: str | None) -> bool:
    return bool(value and value.strip())


def _configured_replicate_token() -> str | None:
    """页面运行时 Token 优先；兼容测试替换的旧 settings 对象。"""
    if hasattr(settings, "replicate_api_token"):
        return settings.replicate_api_token
    return os.getenv("REPLICATE_API_TOKEN")


def build_readiness() -> dict[str, Any]:
    """构造业务服务的脱敏 readiness 响应。

    ``ok`` 表示默认的完整流水线（含硬字幕）所需的基础环境是否可用。
    下载能力和硬字幕能力单独暴露，便于 MCP 给出精确的下一步提示。
    """
    env_file = settings.backend_dir / ".env"

    replicate_token_configured = _has_value(_configured_replicate_token())
    replicate_token_invalid = False
    # Replicate remains an optional compatibility backend. Its account state is
    # reported when configured, but never blocks the local default pipeline.
    replicate_ready = True
    replicate_checked_at: int | None = None
    replicate_cached: bool = False
    replicate_check_status = "missing"

    if replicate_token_configured:
        # External account checks must not make the readiness endpoint fail.
        try:
            account_info = query_replicate_balance()
        except Exception:
            account_info = {
                "status": "unavailable",
                "errorCode": "network_error",
                "checkedAt": int(time.time() * 1000),
                "cached": False,
            }
        if not isinstance(account_info, dict):
            account_info = {
                "status": "unavailable",
                "errorCode": "network_error",
                "checkedAt": int(time.time() * 1000),
                "cached": False,
            }
        replicate_checked_at = account_info.get("checkedAt")
        replicate_cached = bool(account_info.get("cached", False))

        status = account_info.get("status")
        error_code = account_info.get("errorCode")

        if status == "error" or error_code == "invalid_api_token":
            replicate_token_invalid = True
            replicate_ready = True
            replicate_check_status = "invalid"
        elif status == "unavailable":
            replicate_token_invalid = False
            replicate_ready = True
            replicate_check_status = (
                "network_error" if error_code == "network_error" else "unavailable"
            )
        else:
            replicate_token_invalid = False
            replicate_ready = True
            replicate_check_status = "available"

    deepseek_ready = _has_value(
        os.getenv("SUBTRANS_DEEPSEEK_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or settings.deepseek_api_key
    )
    local_engine = None
    local_engine_enabled = False
    local_model_ready = False
    local_dependency_ready = False
    local_translation_ready = False
    try:
        from src.handler.deps import get_translation_engine_store

        local_engine = get_translation_engine_store().get(DEFAULT_TRANSLATION_ENGINE_ID)
        local_engine_enabled = bool(local_engine and local_engine.enabled)
        manager = get_translation_model_manager()
        check_dependencies = getattr(manager, "dependency_status", None)
        local_dependencies = check_dependencies() if check_dependencies is not None else {"ready": True, "missing": []}
        local_model_ready = bool(manager.is_ready())
        local_dependency_ready = bool(local_dependencies.get("ready", False))
        local_translation_ready = bool(
            local_engine_enabled
            and local_model_ready
            and local_dependency_ready
        )
    except Exception:
        # Readiness remains useful when the translation-engine database is
        # temporarily unavailable; the legacy DeepSeek check is still reported.
        local_translation_ready = False
        local_dependencies = {"ready": False, "missing": []}
        local_engine = None
        local_engine_enabled = False
        local_model_ready = False
        local_dependency_ready = False

    # The local engine is the default execution path.  A configured DeepSeek
    # key remains visible for compatibility, but it must not make the default
    # readiness check pass while the local model is absent or broken.
    translation_ready = local_translation_ready
    ffmpeg_status = _check_binary(settings.ffmpeg_bin)
    ffprobe_status = _check_binary(settings.ffprobe_bin)
    yt_dlp_status = "available" if importlib.util.find_spec("yt_dlp") else "missing"
    data_dir_status = _check_writable_directory(settings.data_dir)
    db_dir_status = _check_writable_directory(settings.db_path.parent)

    hard_burn_ready = (
        ffmpeg_status == "available"
        and has_subtitles_filter(settings.ffmpeg_bin)
    )

    download_ready = (
        ffmpeg_status == "available"
        and ffprobe_status == "available"
        and yt_dlp_status == "available"
        and data_dir_status == "writable"
        and db_dir_status == "writable"
    )
    full_pipeline_ready = download_ready and translation_ready
    hard_pipeline_ready = full_pipeline_ready and hard_burn_ready

    missing: list[str] = []
    if not translation_ready:
        if local_engine is not None:
            if not local_engine_enabled:
                missing.append("本地翻译引擎已停用")
            if not local_model_ready:
                missing.append("本地翻译模型未就绪，请在翻译引擎设置中点击“下载并转换”")
            if not local_dependency_ready:
                dependency_names = local_dependencies.get("missing", [])
                if dependency_names:
                    missing.append("本地翻译依赖缺失：" + ", ".join(dependency_names))
                else:
                    missing.append("本地翻译依赖未就绪")
        else:
            missing.append("本地翻译引擎配置不可用")
    if ffmpeg_status != "available":
        missing.append("ffmpeg")
    if ffprobe_status != "available":
        missing.append("ffprobe")
    if yt_dlp_status != "available":
        missing.append("yt-dlp")
    if data_dir_status != "writable":
        missing.append("SUBTRANS_DATA_DIR 可写权限")
    if db_dir_status != "writable":
        missing.append("SUBTRANS_DB 所在目录可写权限")
    if ffmpeg_status == "available" and not hard_burn_ready:
        missing.append("FFmpeg subtitles 滤镜（通常由 libass 提供）")

    local_install_actionable = bool(
        local_engine is not None
        and local_engine_enabled
        and not local_translation_ready
    )
    if not full_pipeline_ready:
        if local_install_actionable:
            message = (
                "本地翻译模型尚未就绪，请在翻译引擎设置中下载并转换；"
                "同时确认 FFmpeg、FFprobe 和 yt-dlp 可用。"
            )
        else:
            message = (
                "业务服务尚未完成初始化。请配置可用的翻译引擎，"
                "并确认 FFmpeg、FFprobe 和 yt-dlp 可用。"
            )
        agent_action = "ask_user_to_configure"
        # Model downloads and optional Python dependencies are loaded lazily;
        # fixing either does not require restarting the FastAPI process.
        restart_required = not local_install_actionable
    elif not hard_burn_ready:
        message = (
            "基础流水线已就绪，但当前 FFmpeg 不支持硬字幕滤镜；"
            "请安装带 libass 的 ffmpeg-full，或将 burn 设置为 soft。"
        )
        agent_action = "use_soft_burn_or_install_libass"
        restart_required = False
    else:
        message = "业务服务已就绪，可以运行完整字幕流水线。"
        agent_action = "continue"
        restart_required = False

    return {
        "ok": hard_pipeline_ready,
        "initialized": full_pipeline_ready,
        "config_file": str(env_file),
        "config_file_present": env_file.is_file(),
        "required_environment": [],
        "replicate_checked_at": replicate_checked_at,
        "replicate_cached": replicate_cached,
        "checks": {
            "api_token_required": bool(settings.api_token),
            "replicate_api_token": replicate_check_status,
            "replicate_checked_at": replicate_checked_at,
            "replicate_cached": replicate_cached,
            "deepseek_api_key": "available" if deepseek_ready else "missing",
            "translation_engine": "available" if translation_ready else "missing",
            "local_translation_model": "available" if local_model_ready else "not_ready",
            "local_translation_dependencies": "available" if local_dependencies.get("ready") else "missing",
            "ffmpeg": ffmpeg_status,
            "ffprobe": ffprobe_status,
            "yt_dlp": yt_dlp_status,
            "data_directory": data_dir_status,
            "database_directory": db_dir_status,
            "subtitle_filter": "available" if hard_burn_ready else "missing",
        },
        "capabilities": {
            "download": download_ready,
            "ffprobe_available": ffprobe_status == "available",
            "full_pipeline": full_pipeline_ready,
            "hard_burn": hard_burn_ready,
            "soft_burn": full_pipeline_ready,
            "max_concurrent_tasks": settings.pipeline_workers,
            "max_concurrent_downloads": getattr(
                settings,
                "download_workers",
                settings.pipeline_workers,
            ),
        },
        "limits": {
            "max_upload_mb": settings.max_upload_mb,
            "max_video_minutes": settings.max_video_minutes,
        },
        "missing": missing,
        "agent_action": agent_action,
        "restart_required": restart_required,
        "message": message,
    }
