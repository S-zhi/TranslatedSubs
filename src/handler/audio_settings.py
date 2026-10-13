"""Runtime audio enhancement settings exposed to the web workbench.

Only a small, safe allow-list is writable from the browser.  The executable
command remains deployment configuration (environment variable) so the web UI
cannot turn this endpoint into an arbitrary command execution surface.
"""

from __future__ import annotations

import importlib.util
import shutil
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.config import settings
from src.config.config import update_runtime_settings
from src.handler.deps import require_api_token
from src.config.redis_store import ConfigUnavailable, RevisionConflict


router = APIRouter(prefix="/api/settings/audio", tags=["audio-settings"])
AudioModel = Literal["htdemucs", "mdx_q"]


class AudioSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    model: AudioModel = "htdemucs"
    threads: int = Field(default=1, ge=1, le=8)
    timeout: int = Field(default=1800, ge=60, le=86400)


class AudioSettingsOut(BaseModel):
    enabled: bool
    backend: str
    model: str
    threads: int
    timeout: int
    demucsInstalled: bool
    ffmpegAvailable: bool
    ready: bool
    message: Optional[str] = None


def _availability() -> tuple[bool, bool, bool, Optional[str]]:
    demucs_installed = importlib.util.find_spec("demucs") is not None
    ffmpeg_available = bool(shutil.which(settings.ffmpeg_bin))
    backend_supported = settings.vocal_separation_backend == "demucs"
    ready = backend_supported and demucs_installed and ffmpeg_available
    if ready:
        message = None
    elif not backend_supported:
        message = f"当前 backend 不受支持：{settings.vocal_separation_backend}。请配置 demucs。"
    elif not demucs_installed:
        message = "未安装 Demucs；启用前请在部署环境安装 demucs。"
    else:
        message = f"未找到 FFmpeg（当前配置：{settings.ffmpeg_bin}）。"
    return demucs_installed, ffmpeg_available, ready, message


def _out() -> AudioSettingsOut:
    demucs, ffmpeg, ready, message = _availability()
    return AudioSettingsOut(
        enabled=settings.vocal_separation_enabled,
        backend=settings.vocal_separation_backend,
        model=settings.vocal_separation_model,
        threads=settings.vocal_separation_threads,
        timeout=settings.vocal_separation_timeout,
        demucsInstalled=demucs,
        ffmpegAvailable=ffmpeg,
        ready=ready,
        message=message,
    )


@router.get("", response_model=AudioSettingsOut)
def get_audio_settings() -> AudioSettingsOut:
    return _out()


@router.put("", response_model=AudioSettingsOut, dependencies=[Depends(require_api_token)])
def put_audio_settings(body: AudioSettingsIn) -> AudioSettingsOut:
    # Explicitly omit backend/command: the deployment controls those values.
    try:
        update_runtime_settings({
            "vocal_separation_enabled": body.enabled,
            "vocal_separation_model": body.model,
            "vocal_separation_threads": body.threads,
            "vocal_separation_timeout": body.timeout,
        })
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail="Redis 配置存储不可用") from exc
    return _out()
