"""Optional CPU Kokoro TTS and subtitle-timeline mixing."""

from __future__ import annotations

import logging
import re
import threading
import wave
from pathlib import Path
from src.core.ffmpeg_utils import run_ffmpeg

logger = logging.getLogger(__name__)
_MODEL_LOCK = threading.Lock()
_PIPELINES = {}
SAMPLE_RATE = 24000
MODEL_ID = "hexgrad/Kokoro-82M-v1.1-zh"


class TTSError(RuntimeError):
    code = "tts_error"


def _blocks(path: Path) -> list[tuple[float, float, str]]:
    content = path.read_text(encoding="utf-8-sig")
    result = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = block.splitlines()
        timing = next((line for line in lines if "-->" in line), None)
        if timing is None:
            continue
        start, end = timing.split("-->", 1)
        def seconds(value: str) -> float:
            value = value.strip().split()[0].replace(",", ".")
            hours, minutes, sec = value.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(sec)
        index = lines.index(timing)
        text = "\n".join(lines[index + 1:]).strip()
        result.append((seconds(start), seconds(end), text))
    return result


def _text_for_translation(translated: str, original: str | None, bilingual: bool) -> str:
    text = translated.strip()
    if bilingual:
        normalized = text.replace("\r\n", "\n")
        source = original.strip().replace("\r\n", "\n") if original else ""
        if source and normalized.startswith(source):
            text = normalized[len(source):].lstrip("\r\n")
        elif "\n" in normalized:
            # Bilingual output is source + newline + translation. If the
            # source prefix drifted, never fall back to speaking both.
            nonempty_lines = [line for line in normalized.splitlines() if line.strip()]
            text = nonempty_lines[-1] if nonempty_lines else ""
        else:
            raise TTSError("双语字幕原文前缀无法安全识别，已跳过配音")
    return re.sub(r"\s*\n+\s*", " ", text).strip()


def _pipeline(language: str):
    with _MODEL_LOCK:
        if language in _PIPELINES:
            return _PIPELINES[language]
        try:
            from kokoro import KModel, KPipeline
            model = KModel(repo_id=MODEL_ID).to("cpu").eval()
            pipe = KPipeline(
                lang_code="z" if language == "zh" else "a",
                repo_id=MODEL_ID,
                model=model,
            )
        except Exception as exc:
            raise TTSError(f"Kokoro 加载失败：{exc}") from exc
        _PIPELINES[language] = pipe
        return pipe


def _split_sentences(text: str, language: str, max_chars: int = 240) -> list[str]:
    """Split long subtitle text at punctuation/whitespace with a hard bound."""
    pieces = re.split(r"(?<=[。！？.!?])\s*", text)
    result = []
    limit = 120 if language == "zh" else max_chars
    for piece in pieces:
        piece = piece.strip()
        while len(piece) > limit:
            cut = max(piece.rfind(mark, 1, limit + 1) for mark in "，,、；; \t")
            if cut <= 0:
                cut = limit
            result.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            result.append(piece)
    return result


def generate_timeline_audio(
    translated_srt: Path | str,
    original_srt: Path | str,
    output_path: Path | str,
    *,
    target_lang: str,
    voice: str,
    bilingual: bool = False,
    duration: float | None = None,
    cancel_check=None,
    on_progress=None,
    task_id: str | None = None,
) -> Path:
    """按 SRT 时间轴合成 24 kHz 单声道语音；重叠字幕会自然叠加。"""
    try:
        import numpy as np
    except ImportError as exc:
        raise TTSError("生成 Kokoro 配音需要安装 numpy") from exc
    lang = "zh" if target_lang.lower().startswith("zh") else "en"
    translated = _blocks(Path(translated_srt))
    originals = _blocks(Path(original_srt))
    if not translated:
        raise TTSError("译文字幕中没有可配音的文本")
    total = max(duration or 0, max(end for _, end, _ in translated))
    pipeline = _pipeline(lang)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    timeline_path = path.with_suffix(".timeline.f32")
    mixed = np.memmap(
        timeline_path,
        mode="w+",
        dtype=np.float32,
        shape=(max(1, int(total * SAMPLE_RATE)),),
    )
    mixed[:] = 0
    timeline_open = True

    def discard_timeline() -> None:
        nonlocal timeline_open
        if not timeline_open:
            return
        mixed.flush()
        mixed._mmap.close()
        timeline_path.unlink(missing_ok=True)
        timeline_open = False

    for index, (start, end, raw_text) in enumerate(translated):
        if cancel_check is not None:
            try:
                cancel_check()
            except Exception:
                discard_timeline()
                raise
        try:
            original_text = originals[index][2] if index < len(originals) else None
            text = _text_for_translation(raw_text, original_text, bilingual)
        except Exception:
            discard_timeline()
            raise
        if not text or end <= start:
            continue
        try:
            # Keep each sentence within its own time budget. Kokoro returns
            # result objects with `.audio`; sentence splitting bounds memory
            # and avoids feeding very long paragraphs into the model.
            sentences = _split_sentences(text, lang)
            chunks = []
            weights = [max(1, len(sentence)) for sentence in sentences]
            for sentence, weight in zip(sentences, weights):
                if cancel_check is not None:
                    try:
                        cancel_check()
                    except Exception:
                        raise
                sentence_budget = max(1, int((end - start) * SAMPLE_RATE * weight / sum(weights)))
                generated = []
                for result in pipeline(sentence, voice=voice, speed=1.0, split_pattern=r"\n+"):
                    audio_piece = getattr(result, "audio", None)
                    if audio_piece is None and isinstance(result, tuple) and len(result) == 3:
                        audio_piece = result[2]
                    if audio_piece is None:
                        raise TTSError("Kokoro 返回了无法识别的音频结果")
                    if hasattr(audio_piece, "detach"):
                        audio_piece = audio_piece.detach().cpu().numpy()
                    generated.append(np.asarray(audio_piece, dtype=np.float32).reshape(-1))
                audio_piece = (
                    np.concatenate(generated)
                    if generated else np.empty(0, dtype=np.float32)
                )
                if len(audio_piece) > sentence_budget:
                    ratio = len(audio_piece) / sentence_budget
                    # Pitch-preserving time compression through FFmpeg.
                    source = path.with_name(f".tts-segment-{index}.in.wav")
                    target = path.with_name(f".tts-segment-{index}.out.wav")
                    try:
                        _write_wav(source, audio_piece)
                        from src.config import settings
                        speed_factors = []
                        remaining = ratio
                        while remaining > 2:
                            speed_factors.append(2.0)
                            remaining /= 2
                        speed_factors.append(max(1.0, remaining))
                        filters = ",".join(f"atempo={factor:.4f}" for factor in speed_factors)
                        run_ffmpeg([
                            settings.ffmpeg_bin, "-v", "error", "-y", "-i", str(source),
                            "-filter:a", filters, "-progress", "pipe:1",
                            "-nostats", "-loglevel", "error", str(target),
                        ], task_id=task_id)
                        with wave.open(str(target), "rb") as wav:
                            compressed = np.frombuffer(
                                wav.readframes(wav.getnframes()), dtype="<i2"
                            )
                        audio_piece = compressed.astype(np.float32) / 32768.0
                    finally:
                        source.unlink(missing_ok=True)
                        target.unlink(missing_ok=True)
                chunks.append((audio_piece, sentence_budget))
            audio = (
                np.concatenate([item[0] for item in chunks])
                if chunks else np.empty(0, dtype=np.float32)
            )
        except Exception as exc:
            discard_timeline()
            # Imported lazily to avoid a module cycle during orchestrator setup.
            from src.service.orchestrator import PipelineCancelledError
            if isinstance(exc, PipelineCancelledError):
                raise
            raise TTSError(f"第 {index + 1} 条字幕配音失败：{exc}") from exc
        if not audio.size:
            continue
        # Clip rare cases that remain over budget after the bounded atempo pass.
        budget = max(1, int((end - start) * SAMPLE_RATE))
        if len(audio) > budget:
            discard_timeline()
            raise TTSError(f"第 {index + 1} 条字幕无法在时间窗内对齐")
        offset = max(0, int(start * SAMPLE_RATE))
        stop = min(len(mixed), offset + len(audio))
        if stop > offset:
            mixed[offset:stop] += audio[:stop - offset] * 0.85
        if on_progress is not None:
            try:
                on_progress((index + 1) * 100 / len(translated))
            except Exception:
                discard_timeline()
                raise
    mixed.flush()
    chunk_size = SAMPLE_RATE * 30
    peak = max((float(np.max(np.abs(mixed[offset:offset + chunk_size])))
                for offset in range(0, len(mixed), chunk_size)), default=0.0)
    if peak > 0.98:
        for offset in range(0, len(mixed), chunk_size):
            mixed[offset:offset + chunk_size] *= 0.98 / peak
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        for offset in range(0, len(mixed), chunk_size):
            pcm = (np.clip(mixed[offset:offset + chunk_size], -1, 1) * 32767).astype("<i2")
            wav.writeframes(pcm.tobytes())
    del mixed
    timeline_path.unlink(missing_ok=True)
    return path


def _write_wav(path: Path, samples) -> None:
    import numpy as np
    if samples.dtype != np.dtype("<i2"):
        samples = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(samples.tobytes())
