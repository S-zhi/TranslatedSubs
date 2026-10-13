"""可独立运行的本地 Whisper 转写服务。

启动：
    uv run uvicorn src.transcriber_server:app --host 0.0.0.0 --port 8010

在业务设置页/API 中将转写后端设为自定义 HTTP，并把服务地址设为
http://127.0.0.1:8010/transcribe。
"""

from __future__ import annotations

import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from src.config import settings
from src.core.transcriber import (
    LocalWhisperTranscriber,
    TranscribeError,
    TranscribeRequest,
)

app = FastAPI(title="Local Whisper Transcriber", version="1.0.0")
_service: Optional[LocalWhisperTranscriber] = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "backend": "faster-whisper"}


@app.post("/transcribe")
async def transcribe_audio(request: Request, authorization: Optional[str] = Header(None)) -> dict:
    """接收音频文件并返回标准 segments 响应。"""
    expected_key = settings.transcriber_api_key
    if not expected_key:
        raise HTTPException(status_code=503, detail="请先在设置页/API 配置转写服务 API Key")
    provided_key = authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else ""
    if not secrets.compare_digest(provided_key, expected_key):
        raise HTTPException(status_code=401, detail="无效的转写服务 API Key")

    audio: Optional[UploadFile] = None
    task_id = "external"
    language: Optional[str] = None
    model: Optional[str] = None
    audio_url: Optional[str] = None
    content_type = request.headers.get("content-type") or ""
    if "application/json" in content_type:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON 请求体必须是对象")
        audio_url = body.get("audio_url")
        task_id = body.get("task_id") or task_id
        language = body.get("language") or language
        model = body.get("model") or model
        if not isinstance(audio_url, str) or not audio_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="audio_url 必须是 HTTP(S) URL")
    elif "multipart/form-data" in content_type:
        form = await request.form()
        candidate = form.get("audio")
        if not hasattr(candidate, "filename") or not hasattr(candidate, "file"):
            raise HTTPException(status_code=400, detail="请上传 audio 文件或提供 audio_url")
        audio = candidate  # type: ignore[assignment]
        task_id = str(form.get("task_id") or task_id)
        language = str(form.get("language")) if form.get("language") else None
        model = str(form.get("model")) if form.get("model") else None
    else:
        raise HTTPException(status_code=415, detail="仅支持 application/json 或 multipart/form-data")
    if not isinstance(task_id, str):
        raise HTTPException(status_code=400, detail="task_id 必须是字符串")
    if language is not None and not isinstance(language, str):
        raise HTTPException(status_code=400, detail="language 必须是字符串或 null")
    if model is not None and not isinstance(model, str):
        raise HTTPException(status_code=400, detail="model 必须是字符串或 null")
    suffix = Path((audio.filename if audio else "audio.wav") or "audio.wav").suffix or ".wav"
    temp_path: Optional[Path] = None
    try:
        if audio is not None:
            with tempfile.NamedTemporaryFile(prefix="subtrans-", suffix=suffix, delete=False) as handle:
                temp_path = Path(handle.name)
                await run_in_threadpool(shutil.copyfileobj, audio.file, handle)
        elif audio_url:
            import httpx
            try:
                async with httpx.AsyncClient(
                    timeout=settings.transcriber_timeout,
                    follow_redirects=True,
                ) as client:
                    response = await client.get(audio_url)
                    response.raise_for_status()
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"下载 audio_url 失败: {exc}") from exc
            with tempfile.NamedTemporaryFile(prefix="subtrans-", suffix=suffix, delete=False) as handle:
                temp_path = Path(handle.name)
                await run_in_threadpool(handle.write, response.content)
        global _service
        if _service is None:
            _service = LocalWhisperTranscriber(
                model_name=settings.local_whisper_model,
                device=settings.local_whisper_device,
                compute_type=settings.local_whisper_compute_type,
                download_root=settings.local_whisper_download_root,
                beam_size=settings.local_whisper_beam_size,
                require_model_ready=True,
            )
        service = _service
        response = await run_in_threadpool(
            service.transcribe,
            TranscribeRequest(
                audio_path=temp_path,
                task_id=task_id,
                language=None if language in (None, "", "auto") else language,
                model_name=model,
            ),
        )
        return {
            **response.output,
            "language": response.language,
            "language_probability": response.language_probability,
            "duration": response.duration,
        }
    except TranscribeError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
