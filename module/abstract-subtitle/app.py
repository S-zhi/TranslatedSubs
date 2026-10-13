"""Standalone OpenAI Whisper transcription service.

The service accepts the same ``/transcribe`` contract as the main pipeline,
while keeping model loading and dependencies isolated from the application.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request

app = FastAPI(title="Abstract Subtitle Whisper Service", version="1.0.0")
_models: dict[str, Any] = {}
_model_lock = Lock()
_inference_lock = Lock()
_OFFICIAL_MODELS = {
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large", "large-v1", "large-v2", "large-v3",
    "large-v3-turbo", "turbo",
}


def _setting(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _check_auth(authorization: str | None) -> None:
    expected = _setting("ABSTRACT_SUBTITLE_API_KEY")
    if not expected:
        if _setting("ABSTRACT_SUBTITLE_REQUIRE_API_KEY", "0").lower() in {"1", "true", "yes"}:
            raise HTTPException(status_code=503, detail="service API key is not configured")
        return
    provided = authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else ""
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid API key")


def _model_ref(name: str | None) -> str:
    selected = (name or _setting("WHISPER_MODEL", "small")).strip()
    if not selected:
        raise HTTPException(status_code=400, detail="model must not be empty")
    return selected


def _model_dir() -> Path:
    value = _setting("WHISPER_MODEL_DIR") or _setting("WHISPER_DOWNLOAD_ROOT", "/models")
    return Path(value).expanduser().resolve()


def _max_audio_bytes() -> int:
    try:
        return max(1, int(_setting("MAX_AUDIO_BYTES", str(512 * 1024 * 1024))))
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="MAX_AUDIO_BYTES must be an integer") from exc


def _resolve_model(name: str) -> str:
    """Allow official model names or trusted checkpoints below WHISPER_MODEL_DIR."""
    if name in _OFFICIAL_MODELS:
        return name
    model_dir = _model_dir()
    candidate = Path(name)
    if not candidate.is_absolute():
        candidate = model_dir / candidate
    try:
        resolved = candidate.expanduser().resolve()
        resolved.relative_to(model_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="custom model must be inside WHISPER_MODEL_DIR") from exc
    if resolved.suffix.lower() != ".pt":
        raise HTTPException(status_code=400, detail="custom model must be a .pt checkpoint")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail=f"custom model does not exist: {resolved.name}")
    return str(resolved)


def _load_model(name: str) -> Any:
    with _model_lock:
        if name in _models:
            return _models[name]
        try:
            import whisper
            model = whisper.load_model(
                _resolve_model(name),
                device=_setting("WHISPER_DEVICE", "cpu"),
                download_root=str(_model_dir()),
            )
        except ImportError as exc:
            raise HTTPException(status_code=503, detail="openai-whisper is not installed") from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"unable to load Whisper model: {exc}") from exc
        _models[name] = model
        return model


def _validate_task(task: str) -> str:
    task = (task or "transcribe").strip().lower()
    if task not in {"transcribe", "translate"}:
        raise HTTPException(status_code=400, detail="task must be transcribe or translate")
    return task


def _run_model(path: str, model_name: str, language: str | None, task: str, words: bool) -> dict:
    options: dict[str, Any] = {"task": task, "word_timestamps": words}
    if language and language.lower() != "auto":
        options["language"] = language
    with _inference_lock:
        result = _load_model(model_name).transcribe(path, **options)
    segments = []
    for segment in result.get("segments", []):
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        item = {"text": text, "start": float(segment["start"]), "end": float(segment["end"])}
        if words and segment.get("words") is not None:
            item["words"] = segment["words"]
        segments.append(item)
    if not segments:
        raise HTTPException(status_code=422, detail="Whisper returned no subtitle segments")
    return {
        "segments": segments,
        "language": result.get("language") or language,
        "duration": segments[-1]["end"],
        "model": model_name,
        "task": task,
    }


async def _transcribe(path: Path, model: str | None, language: str | None, task: str, words: bool) -> dict:
    return await asyncio.to_thread(_run_model, str(path), _model_ref(model), language, _validate_task(task), words)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "backend": "openai-whisper"}


@app.post("/transcribe")
async def transcribe(
    request: Request,
) -> dict:
    _check_auth(request.headers.get("authorization"))
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _max_audio_bytes() + 1024 * 1024:
                raise HTTPException(status_code=413, detail="audio file is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
    content_type = request.headers.get("content-type", "")
    language: str | None = None
    model: str | None = None
    task = "transcribe"
    word_timestamps = False
    audio_bytes: bytes | None = None
    suffix = ".wav"
    if "multipart/form-data" in content_type:
        form = await request.form()
        audio = form.get("audio")
        if audio is None or not hasattr(audio, "read"):
            raise HTTPException(status_code=400, detail="audio file is required")
        audio_bytes = await audio.read()
        if len(audio_bytes) > _max_audio_bytes():
            raise HTTPException(status_code=413, detail="audio file is too large")
        suffix = Path(getattr(audio, "filename", "audio.wav") or "audio.wav").suffix or ".wav"
        language = str(form.get("language")) if form.get("language") else None
        model = str(form.get("model")) if form.get("model") else None
        task = str(form.get("task") or task)
        word_timestamps = str(form.get("word_timestamps", "false")).lower() in {"1", "true", "yes"}
    elif "application/json" in content_type:
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("audio_url"), str):
            raise HTTPException(status_code=400, detail="JSON body requires an audio_url")
        parsed_url = urlparse(body["audio_url"])
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise HTTPException(status_code=400, detail="audio_url must be an HTTP(S) URL")
        import httpx
        try:
            max_bytes = _max_audio_bytes()
            async with httpx.AsyncClient(timeout=float(_setting("AUDIO_DOWNLOAD_TIMEOUT", "600"))) as client:
                async with client.stream("GET", body["audio_url"], follow_redirects=True) as response:
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise HTTPException(status_code=413, detail="audio file is too large")
                        chunks.append(chunk)
                    audio_bytes = b"".join(chunks)
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"unable to download audio_url: {exc}") from exc
        language = body.get("language") if isinstance(body.get("language"), str) else None
        model = body.get("model") if isinstance(body.get("model"), str) else None
        task = str(body.get("task") or task)
        word_timestamps = bool(body.get("word_timestamps", False))
    else:
        raise HTTPException(status_code=415, detail="use multipart/form-data or application/json")
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="abstract-subtitle-", suffix=suffix, delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(audio_bytes or b"")
        return await _transcribe(temp_path, model, language, task, word_timestamps)
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
