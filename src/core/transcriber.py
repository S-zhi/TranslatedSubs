"""流水线第③步：语音识别与字幕生成。

输入：audio.wav（提取阶段产物） + 任务 ID
输出：TranscribeResult（其中 srt_path 指向 data/{task_id}/original.srt）

默认使用本地 faster-whisper，也支持符合协议的自定义 HTTP 服务。
支持本地上传音频文件。进度通过回调透传。
"""

from __future__ import annotations

import logging
import math
import os
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Protocol

import httpx

from src.config import settings, ensure_task_dir, ORIGINAL_SRT
from src.core.srt_utils import Subtitle, decode_srt_bytes, write_srt
from src.service.model_manager import model_manager

logger = logging.getLogger(__name__)


class TranscribeError(RuntimeError):
    """语音识别阶段失败。"""

    def __init__(self, message: str, *, code: str = "transcribe_error"):
        super().__init__(message)
        self.code = code


class TranscribeCancelledError(RuntimeError):
    """语音识别已被取消。"""


def _do_cancel_check(cancel_check: Optional[Callable[[], None]]) -> None:
    if cancel_check is not None:
        cancel_check()


@dataclass
class TranscribeProgress:
    percent: Optional[float]
    status: str   # "starting" | "processing" | "retrying" | "succeeded"


@dataclass
class TranscribeResult:
    srt_path: Path
    language: str
    language_probability: Optional[float]
    segment_count: int
    duration: Optional[float]


ProgressHook = Callable[[TranscribeProgress], None]


@dataclass(frozen=True)
class TranscribeRequest:
    """所有识别服务都必须能够处理的请求参数。"""

    audio_path: Path | str
    task_id: str
    language: Optional[str] = None
    model_name: Optional[str] = None


@dataclass(frozen=True)
class TranscribeResponse:
    """识别服务返回的统一外壳，output 使用现有 segments/SRT 解析协议。"""

    output: Any
    language: Optional[str] = None
    language_probability: Optional[float] = None
    duration: Optional[float] = None


class TranscriberService(Protocol):
    """可接入 transcribe 阶段的服务协议。

    服务可以直接返回 TranscribeResponse，也可以返回能被 _extract_segments
    解析的原始 output（segments 列表、包含 segments/srt/srt_file 的字典等）。
    """

    def transcribe(
        self,
        request: TranscribeRequest,
        *,
        on_progress: Optional[ProgressHook] = None,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> TranscribeResponse | Any:
        ...


# 便于调用方按 backend 语义引用协议。
TranscriberBackend = TranscriberService

_LOCAL_WHISPER_SERVICES: dict[tuple[str, str, str, Optional[str], int], "LocalWhisperTranscriber"] = {}

def _load_env():
    """手动加载项目根 .env（不依赖 python-dotenv）。"""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and val and key not in os.environ:
            os.environ[key] = val


def _safe_callback(hook: ProgressHook, pct: Optional[float], status: str) -> None:
    try:
        hook(TranscribeProgress(percent=pct, status=status))
    except Exception:
        logger.exception("进度回调异常，已忽略")


def _error_code_for_status(status: Optional[int]) -> str:
    if status in (401, 403):
        return "unauthorized"
    if status == 429:
        return "rate_limited"
    if status and status >= 500:
        return "upstream_error"
    if status and 400 <= status < 500:
        return "model_error"
    return "upstream_error"


class HttpTranscriber:
    """符合自定义 HTTP 协议的识别服务适配器。

    请求：POST JSON ``{"audio_url": ..., "language": ..., "model": ..., "task_id": ...}``
    或 multipart ``audio`` 文件；本地文件使用 multipart，远程 URL 使用 JSON。
    响应：JSON 对象，必须包含 ``segments``（每项含 text/start/end）或 ``srt``，
    可选 ``language``、``language_probability``、``duration``。
    """

    def __init__(self, *, url: str, api_key: Optional[str] = None, timeout: int = 1800):
        self.url = url
        self.api_key = api_key
        self.timeout = timeout

    def transcribe(
        self,
        request: TranscribeRequest,
        *,
        on_progress: Optional[ProgressHook] = None,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> TranscribeResponse:
        _do_cancel_check(cancel_check)
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "task_id": request.task_id,
            "language": request.language,
            "model": request.model_name,
        }
        audio_str = str(request.audio_path)
        try:
            if audio_str.startswith(("http://", "https://")):
                response = httpx.post(
                    self.url,
                    json={"audio_url": audio_str, **payload},
                    headers=headers,
                    timeout=self.timeout,
                )
            else:
                with open(audio_str, "rb") as audio_file:
                    response = httpx.post(
                        self.url,
                        data=payload,
                        files={"audio": (Path(audio_str).name, audio_file)},
                        headers=headers,
                        timeout=self.timeout,
                    )
            response.raise_for_status()
            body = response.json()
        except FileNotFoundError:
            raise TranscribeError(f"输入音频不存在: {audio_str}") from None
        except httpx.TimeoutException as exc:
            raise TranscribeError("自定义识别服务请求超时", code="network_error") from exc
        except httpx.HTTPStatusError as exc:
            raise TranscribeError(
                f"自定义识别服务返回 HTTP {exc.response.status_code}",
                code=_error_code_for_status(exc.response.status_code),
            ) from exc
        except httpx.RequestError as exc:
            raise TranscribeError("自定义识别服务网络异常", code="network_error") from exc
        except (TypeError, ValueError) as exc:
            raise TranscribeError("自定义识别服务返回的 JSON 无法解析", code="invalid_response") from exc

        _do_cancel_check(cancel_check)
        if not isinstance(body, dict):
            raise TranscribeError("自定义识别服务返回结构无法解析", code="invalid_response")
        if on_progress is not None:
            _safe_callback(on_progress, 100.0, "succeeded")
        return TranscribeResponse(
            output=body,
            language=body.get("language") if isinstance(body.get("language"), str) else None,
            language_probability=body.get("language_probability"),
            duration=body.get("duration"),
        )


class LocalWhisperTranscriber:
    """使用受管目录中的 CTranslate2 或 Hugging Face Whisper 模型。"""

    def __init__(
        self,
        *,
        model_name: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        download_root: Optional[str] = None,
        beam_size: int = 5,
        require_model_ready: bool = False,
    ):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root
        self.beam_size = max(1, int(beam_size))
        self.require_model_ready = require_model_ready
        self._model = None
        self._loaded_model_name: Optional[str] = None
        self._loaded_model_identity: Optional[tuple[int, int]] = None
        self._model_lock = threading.Lock()

    def _get_model(self, model_name: Optional[str] = None):
        selected_model = model_name or self.model_name
        with self._model_lock:
            try:
                model_ref = model_manager.resolve_path(selected_model)
                stat = Path(model_ref).stat()
                identity = (stat.st_dev, stat.st_ino)
            except Exception as exc:
                raise TranscribeError(str(exc), code="model_not_ready") from exc
            if self._model is None or self._loaded_model_name != selected_model or self._loaded_model_identity != identity:
                try:
                    if model_manager.is_hf(selected_model):
                        from transformers import WhisperForConditionalGeneration, WhisperProcessor, pipeline

                        processor = WhisperProcessor.from_pretrained(model_ref, local_files_only=True, trust_remote_code=False)
                        model = WhisperForConditionalGeneration.from_pretrained(
                            model_ref, local_files_only=True, trust_remote_code=False,
                        )
                        self._model = pipeline(
                            "automatic-speech-recognition", model=model,
                            tokenizer=processor.tokenizer,
                            feature_extractor=processor.feature_extractor,
                            device="cpu",
                        )
                    else:
                        from faster_whisper import WhisperModel
                        self._model = WhisperModel(
                            model_ref, device="cpu", compute_type="int8",
                            local_files_only=True,
                        )
                except ImportError as exc:
                    raise TranscribeError("本地模型推理依赖未安装", code="missing_dependency") from exc
                except Exception as exc:
                    raise TranscribeError(
                        f"本地 Whisper 模型加载失败: {exc}",
                        code="model_error",
                    ) from exc
                self._loaded_model_name = selected_model
                self._loaded_model_identity = identity
            return self._model

    def transcribe(
        self,
        request: TranscribeRequest,
        *,
        on_progress: Optional[ProgressHook] = None,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> TranscribeResponse:
        audio_str = str(request.audio_path)
        if audio_str.startswith(("http://", "https://")):
            raise TranscribeError("本地 Whisper 只支持本地音频文件", code="invalid_input")
        if not Path(audio_str).exists():
            raise TranscribeError(f"输入音频不存在: {audio_str}", code="invalid_input")
        _do_cancel_check(cancel_check)
        if on_progress is not None:
            _safe_callback(on_progress, 5.0, "processing")
        try:
            selected_model = request.model_name or self.model_name
            engine = self._get_model(selected_model)
            normalized = []
            if model_manager.is_hf(selected_model):
                kwargs = {"return_timestamps": True, "generate_kwargs": {"task": "transcribe"}}
                if request.language:
                    kwargs["generate_kwargs"]["language"] = request.language
                result = engine(audio_str, **kwargs)
                if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                    raise TranscribeError("本地 Whisper 输出格式无效", code="model_error")
                audio_duration = 0.0
                try:
                    with wave.open(audio_str, "rb") as audio_file:
                        audio_duration = audio_file.getnframes() / audio_file.getframerate()
                except (OSError, ValueError, wave.Error):
                    pass
                for chunk in result.get("chunks", []):
                    stamp = chunk.get("timestamp")
                    if not isinstance(stamp, (list, tuple)) or len(stamp) != 2:
                        raise TranscribeError("本地 Whisper 时间戳无效", code="model_error")
                    text = str(chunk.get("text") or "").strip()
                    if text and stamp[0] is not None:
                        start = float(stamp[0])
                        end = float(stamp[1]) if stamp[1] is not None else max(start + 0.02, audio_duration)
                        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                            raise TranscribeError("本地 Whisper 时间戳无效", code="model_error")
                        normalized.append({"text": text, "start": start, "end": end})
                detected_language = request.language
                probability = None
                duration = normalized[-1]["end"] if normalized else None
            else:
                segments, info = engine.transcribe(
                    audio_str, language=request.language, beam_size=self.beam_size, vad_filter=True,
                )
                for segment in segments:
                    _do_cancel_check(cancel_check)
                    text = (getattr(segment, "text", "") or "").strip()
                    if text:
                        normalized.append({"text": text, "start": float(segment.start), "end": float(segment.end)})
                        if on_progress is not None:
                            _safe_callback(on_progress, min(95.0, 10.0 + len(normalized)), "processing")
                detected_language = getattr(info, "language", None)
                probability = getattr(info, "language_probability", None)
                duration = getattr(info, "duration", None) or (normalized[-1]["end"] if normalized else None)
        except TranscribeCancelledError:
            raise
        except TranscribeError:
            raise
        except Exception as exc:
            if type(exc).__name__ == "PipelineCancelledError":
                raise
            raise TranscribeError(f"本地 Whisper 转写失败: {exc}", code="model_error") from exc
        if not normalized:
            raise TranscribeError("本地 Whisper 返回空字幕列表", code="empty_response")
        return TranscribeResponse(
            output={"segments": normalized},
            language=detected_language if isinstance(detected_language, str) else request.language,
            language_probability=probability,
            duration=duration,
        )


def _build_transcriber_service(backend: Optional[str] = None) -> TranscriberService:
    backend = str(backend or getattr(settings, "transcriber_backend", "local_whisper") or "local_whisper").strip().lower()
    if backend in {"http", "custom", "custom_http"}:
        url = getattr(settings, "transcriber_url", None)
        if not url:
            raise TranscribeError("自定义识别服务未配置 SUBTRANS_TRANSCRIBER_URL", code="missing_service_config")
        return HttpTranscriber(url=url, api_key=getattr(settings, "transcriber_api_key", None), timeout=getattr(settings, "transcriber_timeout", 1800))
    if backend in {"local_whisper", "local", "whisper", "faster_whisper"}:
        config = (getattr(settings, "local_whisper_model", "small"), getattr(settings, "local_whisper_device", "cpu"), getattr(settings, "local_whisper_compute_type", "int8"), getattr(settings, "local_whisper_download_root", None), getattr(settings, "local_whisper_beam_size", 5))
        service = _LOCAL_WHISPER_SERVICES.get(config)
        if service is None:
            service = LocalWhisperTranscriber(model_name=config[0], device=config[1], compute_type=config[2], download_root=config[3], beam_size=config[4], require_model_ready=True)
            _LOCAL_WHISPER_SERVICES[config] = service
        return service
    raise TranscribeError(f"不支持的识别服务: {backend}", code="invalid_service_config")


def transcribe(
    audio_path: Path | str,
    task_id: str,
    on_progress: Optional[ProgressHook] = None,
    *,
    language: Optional[str] = None,
    model_name: Optional[str] = None,
    backend: Optional[str] = None,
    cancel_check: Optional[Callable[[], None]] = None,
    service: Optional[TranscriberService] = None,
) -> TranscribeResult:
    """把音频识别为原文字幕 data/{task_id}/original.srt。

    服务由 ``SUBTRANS_TRANSCRIBER_BACKEND`` 选择，默认使用本地 Whisper；
    ``http``/``custom`` 使用 ``SUBTRANS_TRANSCRIBER_URL`` 接入符合
    :class:`TranscriberService` 数据协议的自定义节点。

    Args:
        audio_path: 输入音频（通常是 audio.wav）；支持本地路径和 HTTP(S) URL。
        task_id: 任务 ID，决定输出目录。
        on_progress: 可选进度回调。
        language: 源语言代码；None / "" / "auto" 表示自动检测。
        model_name: Whisper 模型名，如 "tiny.en" / "small"。
        service: 可选的进程内 TranscriberService；传入时优先于环境变量选择。
    """
    _load_env()
    lang = None if language in (None, "", "auto") else language

    out_dir = ensure_task_dir(task_id)
    srt_path = out_dir / ORIGINAL_SRT

    # 区分远程 URL vs 本地文件
    audio_str = str(audio_path)
    is_remote = audio_str.startswith(("http://", "https://"))
    if not is_remote:
        p = Path(audio_path)
        if not p.exists():
            raise TranscribeError(f"输入音频不存在: {p}")
        audio_str = str(p)

    _do_cancel_check(cancel_check)
    service = service if service is not None else _build_transcriber_service(backend)
    logger.info(
        "开始识别(%s): task=%s model=%s lang=%s",
        backend or getattr(settings, "transcriber_backend", "local_whisper"),
        task_id,
        model_name or "small",
        lang,
    )

    if on_progress is not None:
        _safe_callback(on_progress, 1.0, "starting")

    response = service.transcribe(
        TranscribeRequest(
            audio_path=audio_str,
            task_id=task_id,
            language=lang,
            model_name=model_name,
        ),
        on_progress=on_progress,
        cancel_check=cancel_check,
    )
    _do_cancel_check(cancel_check)

    if isinstance(response, TranscribeResponse):
        output = response.output
        response_language = response.language
        response_language_probability = response.language_probability
        response_duration = response.duration
    else:
        output = response
        response_language = None
        response_language_probability = None
        response_duration = None

    if on_progress is not None:
        _safe_callback(on_progress, 95.0, "succeeded")

    # 解析 segments → Subtitle 列表
    segments = _extract_segments(output)
    subs: List[Subtitle] = []
    for i, seg in enumerate(segments, start=1):
        text = (seg.get("text") or "").strip()
        if text:
            try:
                start = float(seg["start"])
                end = float(seg["end"])
            except (KeyError, TypeError, ValueError):
                raise TranscribeError("识别服务返回结构无法解析", code="invalid_response") from None
            if not math.isfinite(start) or not math.isfinite(end):
                raise TranscribeError("识别服务返回结构无法解析", code="invalid_response")
            if end <= start:
                raise TranscribeError("结束时间必须大于开始时间", code="invalid_response")
            subs.append(Subtitle(index=i, start=start, end=end, text=text))

    if not subs:
        raise TranscribeError("识别服务返回空字幕列表", code="empty_response")
    try:
        write_srt(subs, srt_path)
    except ValueError as exc:
        raise TranscribeError("识别服务返回结构无法解析", code="invalid_response") from exc
    if on_progress is not None:
        _safe_callback(on_progress, 100.0, "succeeded")

    detected = response_language or lang or "unknown"
    duration = response_duration if response_duration is not None else (subs[-1].end if subs else None)
    result = TranscribeResult(
        srt_path=srt_path,
        language=detected,
        language_probability=response_language_probability,
        segment_count=len(subs),
        duration=duration,
    )
    logger.info("识别完成: task=%s lang=%s segments=%d", task_id, detected, len(subs))
    return result


def _extract_segments(output) -> List[dict]:
    """从识别服务返回中提取 segments 列表，兼容多种输出格式。"""
    # 格式 1: dict 包含 srt_file（远程 SRT 文件 URL），下载后解析
    if isinstance(output, dict):
        if "srt_file" in output:
            srt_url = str(output["srt_file"])
            logger.info("下载远程 SRT: %s", srt_url[:80])
            srt_text = _download_text(srt_url)
            return _parse_srt_segments(srt_text)
        if "segments" in output:
            segments = output["segments"]
            if not isinstance(segments, list) or any(
                not isinstance(seg, dict) or "text" not in seg or "start" not in seg or "end" not in seg
                for seg in segments
            ):
                raise TranscribeError("识别服务返回结构无法解析", code="invalid_response")
            return segments
        if "srt" in output:
            return _parse_srt_segments(output["srt"])
        if "text" in output:
            return [output]

    # 格式 2: output 直接是 segments 列表
    if isinstance(output, list):
        if not output:
            return []
        if all(
            isinstance(seg, dict) and "text" in seg and "start" in seg and "end" in seg
            for seg in output
        ):
            return output
        raise TranscribeError("识别服务返回结构无法解析", code="invalid_response")

    raise TranscribeError("识别服务返回结构无法解析", code="invalid_response")


def _download_text(url: str) -> str:
    """下载远程文本（SRT）。"""
    try:
        resp = httpx.get(url, timeout=30)
        resp.raise_for_status()
        raw = getattr(resp, "content", None)
        if raw is None:
            raw = getattr(resp, "text", "").encode("utf-8")
        text, _ = decode_srt_bytes(raw)
        return text
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        code = "unauthorized" if status in (401, 403) else "rate_limited" if status == 429 else "upstream_error"
        raise TranscribeError(f"下载远程字幕文件失败 HTTP {status}", code=code) from exc
    except httpx.RequestError as exc:
        raise TranscribeError("下载远程字幕文件超时或网络异常", code="network_error") from exc


def _parse_srt_segments(srt_text: str) -> List[dict]:
    """解析 SRT 文本为 segment dict 列表。

    兼容两种格式：
    - 标准 SRT：每个 segment 之间用空行分隔。
    - 紧凑 SRT：无空行，按「序号 + 时间轴 + 文本」模式逐行拆分。
    """
    import re
    text = srt_text.strip()

    # 策略 1：标准 SRT，有空行分隔
    if "\n\n" in text:
        return _parse_standard_srt(text)

    # 策略 2：逐行扫描，按序号行模式拆分
    return _parse_compact_srt(text)


def _parse_standard_srt(text: str) -> List[dict]:
    import re
    segments = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        time_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if time_idx is None:
            continue
        start_s, _, end_s = lines[time_idx].partition("-->")
        try:
            start = _parse_srt_ts(start_s)
            end = _parse_srt_ts(end_s)
        except (ValueError, IndexError):
            continue
        txt = " ".join(lines[time_idx + 1:]).strip()
        if txt:
            segments.append({"text": txt, "start": start, "end": end})
    return segments


def _parse_compact_srt(text: str) -> List[dict]:
    """解析无空行的紧凑 SRT：行首为纯数字即新 segment 开始。"""
    lines = text.splitlines()
    segments = []
    buf: List[str] = []
    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            continue
        # 纯数字行 = 新 segment 开始
        if stripped.isdigit() and buf:
            seg = _block_to_segment(buf)
            if seg:
                segments.append(seg)
            buf = [stripped]
        else:
            buf.append(stripped)
    if buf:
        seg = _block_to_segment(buf)
        if seg:
            segments.append(seg)
    return segments


def _block_to_segment(lines: List[str]) -> Optional[dict]:
    """把一段 SRT 行（序号 + 时间轴 + 文本）转成 segment dict。"""
    time_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
    if time_idx is None:
        return None
    # 跳过序号行（纯数字）
    text_lines = [ln for i, ln in enumerate(lines) if i > time_idx and not ln.strip().isdigit()]
    if not text_lines:
        return None
    start_s, _, end_s = lines[time_idx].partition("-->")
    try:
        start = _parse_srt_ts(start_s)
        end = _parse_srt_ts(end_s)
    except (ValueError, IndexError):
        return None
    return {"text": " ".join(text_lines).strip(), "start": start, "end": end}


def _parse_srt_ts(ts: str) -> float:
    ts = ts.strip().replace(".", ",")
    hms, _, millis = ts.partition(",")
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(millis or 0) / 1000


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if len(sys.argv) < 2:
        print("用法: python -m src.core.transcriber <audio_path_or_url> [task_id] [language] [model]")
        raise SystemExit(1)

    audio = sys.argv[1]
    task = sys.argv[2] if len(sys.argv) > 2 else "manual_test"
    lang = sys.argv[3] if len(sys.argv) > 3 else None
    mod = sys.argv[4] if len(sys.argv) > 4 else None

    def _p(p: TranscribeProgress) -> None:
        print(f"\r识别中 [{p.status}] {p.percent or '?'}%", end="", flush=True)

    res = transcribe(audio, task, on_progress=_p, language=lang, model_name=mod)
    print(f"\n识别结果: lang={res.language} segments={res.segment_count} dur={res.duration}")
    print(f"  字幕: {res.srt_path}")
