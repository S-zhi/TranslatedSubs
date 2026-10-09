"""流水线第④步：翻译字幕（可配置 OpenAI/Anthropic 兼容协议）。

输入：original.srt（识别阶段产物） + 任务 ID + 源/目标语言 + 模式
输出：TranslateResult（其中 srt_path 指向 data/{task_id}/translated.srt）

时间轴严格保留：只翻译每条字幕的文本，时间戳原样复制。
mode="bilingual" 时每条字幕保留原文 + 译文两行。

翻译按配置选择 OpenAI-compatible 或 Anthropic-compatible 接口，按批发送以减少请求数。
网络调用集中在 _call_deepseek（懒加载 httpx），便于单测 mock。

旧版 DeepSeek 环境变量仍作为兼容路径保留。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from src.config import settings, ensure_task_dir, TRANSLATED_SRT
from src.core.translation_engines import TranslationEngineError, make_engine_client
from src.core.srt_utils import Subtitle, parse_srt, write_srt
from src.service.translation_model_manager import (
    TranslationModelError,
    get_translation_model_manager,
)

logger = logging.getLogger(__name__)


class TranslateError(RuntimeError):
    """翻译阶段失败。"""

    def __init__(self, message: str, *, code: str = "translate_error"):
        super().__init__(message)
        self.code = code


@dataclass
class TranslateProgress:
    percent: float
    done: int
    total: int


@dataclass
class TranslateResult:
    srt_path: Path
    count: int
    bilingual: bool


ProgressHook = Callable[[TranslateProgress], None]
BatchHook = Callable[[int, int], None]

# 上游 5xx 通常是暂时性故障，但不应因此把一个批次无限拆成单条请求。
# 每次重试前等待 5、15、60 秒；网络错误则立即重试 3 次，耗尽后直接失败。
_UPSTREAM_RETRY_DELAYS = (5, 15, 60)
_NETWORK_RETRY_COUNT = 3
_RETRYABLE_ERROR_CODES = {"upstream_error", "network_error"}


def _sleep_with_cancel(seconds: float, cancel_check: Optional[Callable[[], None]]) -> None:
    elapsed = 0.0
    while elapsed < seconds:
        if cancel_check is not None:
            cancel_check()
        sleep_time = min(0.5, seconds - elapsed)
        time.sleep(sleep_time)
        elapsed += sleep_time
    if cancel_check is not None:
        cancel_check()


def _lang_name(code: str) -> str:
    names = getattr(settings, "lang_names", {})
    return names.get(code, code)


def _call_deepseek(messages: list, *, api_key: str, base_url: str, model: str, timeout: int) -> str:
    """调用 DeepSeek /chat/completions，返回模型回复文本。懒加载 httpx。"""
    import httpx

    url = base_url.rstrip("/") + "/chat/completions"
    try:
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.3,
                "stream": False,
                "max_tokens": 4096,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        code = "unauthorized" if status in (401, 403) else "rate_limited" if status == 429 else "upstream_error"
        raise TranslateError(f"翻译 API 返回 HTTP {status}", code=code) from exc
    except httpx.RequestError as exc:
        raise TranslateError("连接翻译 API 超时或网络异常", code="network_error") from exc
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TranslateError("翻译 API 返回数据格式无法解析", code="invalid_response") from exc


def translate_texts(
    texts: List[str],
    source_lang: str,
    target_lang: str,
    *,
    api_key: Optional[str] = None,
    on_batch: Optional[BatchHook] = None,
    engine_config=None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> List[str]:
    """翻译一批文本，保持顺序与数量一致。

    数量不匹配时自动减半批量重试（模型可能截断长 JSON）。
    """
    if not texts:
        return []
    api_type = (
        getattr(engine_config, "api_type", None)
        if engine_config is not None
        else None
    )
    if isinstance(engine_config, dict):
        api_type = engine_config.get("api_type", api_type)
    if api_type == "local_ct2":
        if source_lang != "en" or target_lang not in {"zh-CN", "zh"}:
            raise TranslateError(
                "本地翻译模型只支持 en 到 zh-CN/zh",
                code="unsupported_language",
            )
        manager = get_translation_model_manager()
        batch_size = max(1, getattr(settings, "local_translate_batch_size", 16))
        translated: List[str] = []
        for start in range(0, len(texts), batch_size):
            if cancel_check is not None:
                cancel_check()
            batch = texts[start:start + batch_size]
            try:
                translated.extend(manager.translate_texts(batch, cancel_check=cancel_check))
            except TranslationModelError as exc:
                raise TranslateError(str(exc), code=exc.code) from exc
            if on_batch is not None:
                on_batch(len(translated), len(texts))
        return translated

    key = api_key or settings.deepseek_api_key
    if engine_config is not None:
        key = getattr(engine_config, "api_key", None) or (engine_config.get("api_key") if isinstance(engine_config, dict) else None)
    if not (key and str(key).strip()):
        raise TranslateError("缺少翻译引擎 API Key", code="missing_api_key")

    batch_size = max(1, settings.translate_batch_size)
    out: List[str] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        translated = _translate_with_fallback(
            batch,
            source_lang,
            target_lang,
            key,
            engine_config=engine_config,
            on_batch=on_batch,
            total_count=len(texts),
            completed_offset=len(out),
            cancel_check=cancel_check,
        )
        out.extend(translated)
    return out


def _translate_with_fallback(
    batch: List[str],
    source_lang: str,
    target_lang: str,
    api_key: str,
    *,
    engine_config=None,
    partial: Optional[dict[int, str]] = None,
    indices: Optional[List[int]] = None,
    on_batch: Optional[BatchHook] = None,
    total_count: int = 0,
    completed_offset: int = 0,
    last_error: Optional[Exception] = None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> List[str]:
    """翻译一个批次，数量不匹配时自动减半拆分重试（对已成功部分做 dedup 去重）。"""
    size = len(batch)
    if indices is None:
        indices = list(range(size))
    if partial is None:
        partial = {}

    missing_indices = [idx for idx in indices if idx not in partial]
    if not missing_indices:
        return [partial[idx] for idx in indices]

    idx_to_item = dict(zip(indices, batch))
    sub_batch = [idx_to_item[idx] for idx in missing_indices]
    current_error = last_error
    try:
        translated = _translate_batch_with_retry(
            sub_batch, source_lang, target_lang, api_key,
            engine_config=engine_config, cancel_check=cancel_check,
        )
        if 0 < len(translated) <= len(missing_indices):
            new_added = False
            for idx, res in zip(missing_indices[:len(translated)], translated):
                if idx not in partial:
                    partial[idx] = res
                    new_added = True
            if new_added and on_batch is not None and total_count > 0:
                on_batch(completed_offset + len(partial), total_count)
        if len(translated) == len(missing_indices):
            return [partial[idx] for idx in indices]
    except Exception as exc:
        err_code = getattr(exc, "code", None)
        if err_code in (
            "unauthorized", "missing_api_key", "rate_limited", "insufficient_quota",
            # 上游/网络错误已经在 _translate_batch_with_retry 中完成有限重试。
            # 继续拆分只会重复请求并掩盖真实故障。
            *_RETRYABLE_ERROR_CODES,
        ):
            raise
        current_error = exc

    if size <= 1:
        if partial:
            # A partial response may leave this slot without a translation. Keep
            # the subtitle in the output rather than discarding successful slots.
            idx = indices[0]
            partial[idx] = batch[0]
            if on_batch is not None and total_count > 0:
                on_batch(completed_offset + len(partial), total_count)
            return [partial[item_idx] for item_idx in indices]

        line_num = completed_offset + indices[0] + 1
        if current_error:
            err_msg = str(current_error)
            err_code = getattr(current_error, "code", "invalid_response")
        else:
            err_msg = "即使单条也无法获得匹配结果"
            err_code = "invalid_response"
        raise TranslateError(
            f"第 {line_num} 条字幕翻译失败：{err_msg}",
            code=err_code,
        ) from current_error

    # 减半拆分：递归处理两个子批
    half = max(1, size // 2)
    logger.warning("批量 %d 翻译结果不匹配，拆分为 %d + %d 重试", size, half, size - half)
    left = _translate_with_fallback(
        batch[:half], source_lang, target_lang, api_key,
        engine_config=engine_config, partial=partial, indices=indices[:half],
        on_batch=on_batch, total_count=total_count, completed_offset=completed_offset,
        last_error=current_error, cancel_check=cancel_check,
    )
    right = _translate_with_fallback(
        batch[half:], source_lang, target_lang, api_key,
        engine_config=engine_config, partial=partial, indices=indices[half:],
        on_batch=on_batch, total_count=total_count, completed_offset=completed_offset,
        last_error=current_error, cancel_check=cancel_check,
    )
    return left + right


def _translate_batch_with_retry(
    batch: List[str],
    source_lang: str,
    target_lang: str,
    api_key: str,
    *,
    engine_config=None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> List[str]:
    """对暂时性接口错误做有限重试，避免触发批量减半递归。

    5xx 使用 5/15/60 秒退避（初次请求后最多重试三次）；网络异常立即重试三次。
    认证、限流、配额和响应格式错误交由上层原有快失败/拆分逻辑处理。
    """
    upstream_attempt = 0
    network_attempt = 0
    while True:
        try:
            return _translate_batch(batch, source_lang, target_lang, api_key, engine_config=engine_config)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == "upstream_error" and upstream_attempt < len(_UPSTREAM_RETRY_DELAYS):
                delay = _UPSTREAM_RETRY_DELAYS[upstream_attempt]
                upstream_attempt += 1
                logger.warning("翻译上游错误，%d 秒后重试（第 %d/%d 次）", delay, upstream_attempt, len(_UPSTREAM_RETRY_DELAYS))
                _sleep_with_cancel(delay, cancel_check)
                continue
            if code == "network_error" and network_attempt < _NETWORK_RETRY_COUNT:
                network_attempt += 1
                logger.warning("翻译网络错误，立即重试（第 %d/%d 次）", network_attempt, _NETWORK_RETRY_COUNT)
                continue
            raise


def _translate_batch(batch: List[str], source_lang: str, target_lang: str, api_key: str, *, engine_config=None) -> List[str]:
    tgt = _lang_name(target_lang)
    src = _lang_name(source_lang)
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(batch))
    system = (
        "You are a professional subtitle translator. "
        f"Translate each line from {src} to {tgt}. "
        "Keep the translation natural and concise, suitable for on-screen subtitles. "
        "Do not merge or split lines, do not add explanations."
    )
    user = (
        f"Translate these {len(batch)} subtitle lines to {tgt}. "
        f"Return ONLY a JSON array of exactly {len(batch)} strings, in the same order, "
        "with no extra text:\n\n"
        f"{numbered}"
    )
    if engine_config is None:
        content = _call_deepseek(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            api_key=api_key, base_url=settings.deepseek_base_url,
            model=settings.deepseek_model, timeout=settings.translate_timeout,
        )
    else:
        try:
            content = make_engine_client(engine_config).complete(system, user)
        except TranslationEngineError as exc:
            raise TranslateError(str(exc), code=exc.code) from exc
    return _parse_translation_response(content, len(batch))


def _parse_array_elements(text: str) -> List[str]:
    """尝试从包含 '[' 的文本中逐个解析顶层 JSON 数组元素。"""
    start = text.find("[")
    if start == -1:
        return []

    decoder = json.JSONDecoder()
    pos = start + 1
    recovered: List[str] = []

    while pos < len(text):
        while pos < len(text) and text[pos] in " \t\n\r,":
            pos += 1
        if pos >= len(text) or text[pos] == "]":
            break

        try:
            val, end_idx = decoder.raw_decode(text, pos)
            recovered.append(str(val) if not isinstance(val, str) else val)
            pos = end_idx
        except json.JSONDecodeError:
            break

    return recovered


def _parse_translation_response(content: str, expected: int) -> List[str]:
    """解析模型回复：优先 JSON 数组，回退到按行去编号。"""
    text = content.strip()
    # 去掉 ```json ... ``` 代码围栏
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    try:
        arr = json.loads(text)
        if isinstance(arr, list):
            return [str(x) for x in arr]
    except json.JSONDecodeError:
        pass

    # 若 JSON 解析失败，但包含 '['，尝试从截断的 JSON 数组中提取已完整闭合的字符串元素
    if "[" in text:
        recovered = _parse_array_elements(text)
        if recovered:
            if len(recovered) != expected:
                raise TranslateError(
                    f"JSON 截断，只恢复了 {len(recovered)}/{expected} 条",
                    code="invalid_response",
                )
            return recovered

    # 回退：按非空行拆，去掉行首 "1. " / "1) " 编号
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return [re.sub(r"^\s*\d+[\.\)]\s*", "", ln).strip() for ln in lines]


def translate_srt(
    srt_path: Path | str,
    task_id: str,
    source_lang: str,
    target_lang: str,
    *,
    mode: str = "mono",
    on_progress: Optional[ProgressHook] = None,
    api_key: Optional[str] = None,
    engine_config=None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> TranslateResult:
    """翻译 SRT 文件到 data/{task_id}/translated.srt，时间轴保持不变。

    Args:
        mode: "mono" 仅译文；"bilingual" 原文 + 译文两行。

    Raises:
        TranslateError: 输入不存在、缺 Key、或翻译失败。
    """
    srt_path = Path(srt_path)
    if not srt_path.exists():
        raise TranslateError(f"输入字幕不存在: {srt_path}", code="invalid_argument")

    bilingual = mode == "bilingual"
    out_dir = ensure_task_dir(task_id)
    out_path = out_dir / TRANSLATED_SRT

    subs = parse_srt(srt_path)
    if not subs:
        write_srt([], out_path)
        return TranslateResult(srt_path=out_path, count=0, bilingual=bilingual)

    def _on_batch(done: int, total: int) -> None:
        if on_progress is not None:
            on_progress(TranslateProgress(
                percent=round(done / total * 100, 1),
                done=done,
                total=total,
            ))

    logger.info("开始翻译: task=%s 条数=%d -> %s", task_id, len(subs), target_lang)
    translated = translate_texts(
        [s.text for s in subs], source_lang, target_lang,
        api_key=api_key, on_batch=_on_batch, engine_config=engine_config,
        cancel_check=cancel_check,
    )

    out_subs = [
        Subtitle(
            index=sub.index,
            start=sub.start,
            end=sub.end,
            text=f"{sub.text}\n{tr}" if bilingual else tr,
        )
        for sub, tr in zip(subs, translated)
    ]
    write_srt(out_subs, out_path)
    logger.info("翻译完成: task=%s -> %s", task_id, out_path.name)
    return TranslateResult(srt_path=out_path, count=len(out_subs), bilingual=bilingual)


if __name__ == "__main__":
    # 独立测试入口：python -m src.core.translator <srt> [task_id] [target_lang] [mode]
    import os
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # 加载项目根 .env（settings 导入时已读过环境变量，这里取 key 显式传入）
    _env = Path(__file__).resolve().parents[2] / ".env"
    if _env.exists():
        for _line in _env.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

    if len(sys.argv) < 2:
        print("用法: python -m src.core.translator <srt_path> [task_id] [target_lang] [mode]")
        raise SystemExit(1)

    srt = sys.argv[1]
    task = sys.argv[2] if len(sys.argv) > 2 else "manual_test"
    target = sys.argv[3] if len(sys.argv) > 3 else "zh-CN"
    m = sys.argv[4] if len(sys.argv) > 4 else "mono"
    api_key = os.getenv("SUBTRANS_DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY")

    def _p(p: TranslateProgress) -> None:
        print(f"\r翻译中 {p.percent:5.1f}% ({p.done}/{p.total})", end="", flush=True)

    res = translate_srt(srt, task, "auto", target, mode=m, on_progress=_p, api_key=api_key)
    print(f"\n翻译完成: {res.srt_path}（{res.count} 条，双语={res.bilingual}）")
