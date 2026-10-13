"""④ 翻译 单测。mock DeepSeek 的 _call_deepseek，不依赖网络 / httpx。"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from src.core import translator
from src.core.srt_utils import Subtitle, parse_srt, write_srt
from src.core.translator import (
    TranslateError,
    translate_srt,
    translate_texts,
    _parse_translation_response,
)


@pytest.fixture
def fake_settings(monkeypatch):
    s = SimpleNamespace(
        deepseek_api_key="test-key",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
        translate_batch_size=2,
        translate_timeout=10,
    )
    monkeypatch.setattr(translator, "settings", s)
    engine = SimpleNamespace(
        api_type="openai_compatible", api_key="test-key",
        base_url="https://api.deepseek.com", model="deepseek-chat", timeout=10,
    )

    class FakeEngineClient:
        def complete(self, system, user):
            return translator._call_deepseek(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                api_key=engine.api_key, base_url=engine.base_url,
                model=engine.model, timeout=engine.timeout,
            )

    monkeypatch.setattr(translator, "make_engine_client", lambda _config: FakeEngineClient())
    s.engine_config = engine
    return s


def _echo_call(messages, **kwargs):
    """假的 DeepSeek：从用户消息里抽出编号行，按数量返回 JSON 数组。"""
    user = messages[-1]["content"]
    items = re.findall(r"^\d+\.\s*(.*)$", user, re.M)
    return json.dumps([f"T-{x}" for x in items])


# ---------- _parse_translation_response ----------

def test_parse_plain_json_array():
    assert _parse_translation_response('["a", "b"]', 2) == ["a", "b"]


def test_parse_code_fenced_json():
    content = "```json\n[\"x\", \"y\"]\n```"
    assert _parse_translation_response(content, 2) == ["x", "y"]


def test_parse_truncated_json_array_returns_error():
    content = '["hello", "world", "partial'
    with pytest.raises(TranslateError) as exc_info:
        _parse_translation_response(content, 3)
    assert exc_info.value.code == "invalid_response"
    assert "JSON 截断，只恢复了 2/3 条" in str(exc_info.value)


def test_parse_truncated_json_array_exact_match():
    # If the JSON array was truncated at closing bracket but all expected elements were recovered
    content = '["hello", "world"'
    assert _parse_translation_response(content, 2) == ["hello", "world"]


def test_parse_numbered_fallback():
    content = "1. first\n2. second"
    assert _parse_translation_response(content, 2) == ["first", "second"]


# ---------- translate_texts ----------

def test_translate_texts_batches(fake_settings, monkeypatch):
    monkeypatch.setattr(translator, "_call_deepseek", _echo_call)
    seen = []
    out = translate_texts(
        ["a", "b", "c"], "auto", "zh-CN",
        on_batch=lambda done, total: seen.append((done, total)),
        engine_config=fake_settings.engine_config,
    )
    assert out == ["T-a", "T-b", "T-c"]
    # batch_size=2 -> 两批：(2,3) 和 (3,3)
    assert seen == [(2, 3), (3, 3)]


def test_translate_texts_empty():
    assert translate_texts([], "auto", "zh-CN") == []


def test_translate_texts_missing_key(monkeypatch):
    monkeypatch.setattr(
        translator, "settings",
        SimpleNamespace(deepseek_api_key=None, translate_batch_size=2),
    )
    with pytest.raises(TranslateError, match="API Key"):
        translate_texts(["a"], "auto", "zh-CN")


def test_translate_texts_retry_on_mismatch_then_fail(fake_settings, monkeypatch):
    """数量不匹配时自动减半重试；即使单条也失败时才抛错。"""
    monkeypatch.setattr(translator, "_call_deepseek", lambda *a, **k: json.dumps([]))
    with pytest.raises(TranslateError, match="单条"):
        translate_texts(["a", "b"], "auto", "zh-CN", engine_config=fake_settings.engine_config)


def test_translate_texts_retry_succeeds_after_halving(fake_settings, monkeypatch):
    """批量2失败但单条成功时，应自动减半并合并结果。"""
    called_batches = []

    def fake(messages, **kw):
        # 从 user message 里数编号
        user = messages[1]["content"]
        count = len([ln for ln in user.splitlines() if ln.strip() and ln[0].isdigit()])
        called_batches.append(count)
        if count == 2:
            return json.dumps(["only-one"])  # 不够
        return json.dumps(["ok"] * count)

    monkeypatch.setattr(translator, "_call_deepseek", fake)
    result = translate_texts(["a", "b"], "auto", "zh-CN", engine_config=fake_settings.engine_config)
    # 第一一次调用 (batch size 2) 返回了 ["only-one"]，对齐到了 index 0 ("a")，
    # 拆分后 right 子批 ("b") 仅调用 1 次，left 子批 ("a") 命中了 partial 缓存无需再次调用 LLM！
    assert result == ["only-one", "ok"]
    assert called_batches == [2, 1]


def test_translate_texts_dedup_partial_hit(fake_settings, monkeypatch):
    """测试部分命中 partial 缓存时，已成功项不再调用 LLM。"""
    called_items = []

    def fake(messages, **kw):
        user = messages[1]["content"]
        items = re.findall(r"^\d+\.\s*(.*)$", user, re.M)
        called_items.append(items)
        if len(items) == 4:
            # 4 条 batch 仅返回前 2 条
            return json.dumps(["T-1", "T-2"])
        return json.dumps([f"T-{x}" for x in items])

    monkeypatch.setattr(translator, "_call_deepseek", fake)
    # batch_size 为 4
    monkeypatch.setattr(translator.settings, "translate_batch_size", 4)
    result = translate_texts(["item1", "item2", "item3", "item4"], "auto", "zh-CN", engine_config=fake_settings.engine_config)

    assert result == ["T-1", "T-2", "T-item3", "T-item4"]
    # 第一次 4 条 -> 只命中前 2 条 (T-1, T-2)
    # 拆分: left 子批 ([0, 1]) 两个都命中 -> 0 次 LLM
    # right 子批 ([2, 3]) 缺 2 条 -> 1 次 LLM (调用 2 条)
    assert len(called_items) == 2
    assert len(called_items[0]) == 4
    assert len(called_items[1]) == 2


def test_translate_texts_partial_response_falls_back_without_losing_results(fake_settings, monkeypatch):
    """截断批次留下空槽时，保留已翻译项并回填原文。"""
    called_batches = []

    def fake(messages, **kw):
        user = messages[1]["content"]
        items = re.findall(r"^\d+\.\s*(.*)$", user, re.M)
        called_batches.append(items)
        if len(items) == 4:
            return json.dumps(["translated-1", "translated-2"])
        if len(items) == 2:
            return json.dumps(["translated-3"])
        return json.dumps([])

    monkeypatch.setattr(translator, "_call_deepseek", fake)
    monkeypatch.setattr(translator.settings, "translate_batch_size", 4)
    progress = []

    result = translate_texts(
        ["item1", "item2", "item3", "item4"],
        "auto",
        "zh-CN",
        on_batch=lambda done, total: progress.append((done, total)),
        engine_config=fake_settings.engine_config,
    )

    assert result == ["translated-1", "translated-2", "translated-3", "item4"]
    assert progress == [(2, 4), (3, 4), (4, 4)]
    assert called_batches == [["item1", "item2", "item3", "item4"], ["item3", "item4"], ["item4"]]



# ---------- translate_srt ----------

def _make_srt(tmp_path):
    p = tmp_path / "original.srt"
    write_srt(
        [Subtitle(1, 0.0, 1.0, "hello"), Subtitle(2, 1.0, 2.0, "world")],
        p,
    )
    return p


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    d = tmp_path / "out"
    monkeypatch.setattr(translator, "ensure_task_dir", lambda task_id: (d.mkdir(exist_ok=True) or d))
    return d


def test_translate_srt_mono(tmp_path, out_dir, monkeypatch):
    src = _make_srt(tmp_path)
    monkeypatch.setattr(translator, "translate_texts", lambda texts, s, t, **k: [f"译:{x}" for x in texts])

    res = translate_srt(src, "task1", "auto", "zh-CN", mode="mono")

    assert res.count == 2
    assert res.bilingual is False
    subs = parse_srt(res.srt_path)
    assert subs[0].text == "译:hello"
    # 时间轴保持不变
    assert subs[0].start == 0.0 and subs[1].end == 2.0


def test_translate_srt_bilingual(tmp_path, out_dir, monkeypatch):
    src = _make_srt(tmp_path)
    monkeypatch.setattr(translator, "translate_texts", lambda texts, s, t, **k: [f"译:{x}" for x in texts])

    res = translate_srt(src, "task1", "auto", "zh-CN", mode="bilingual")

    assert res.bilingual is True
    subs = parse_srt(res.srt_path)
    assert subs[0].text == "hello\n译:hello"


def test_translate_srt_missing_input(tmp_path):
    with pytest.raises(TranslateError, match="不存在"):
        translate_srt(tmp_path / "nope.srt", "task1", "auto", "zh-CN")


def test_translate_srt_empty(tmp_path, out_dir):
    p = tmp_path / "original.srt"
    p.write_text("", encoding="utf-8")
    res = translate_srt(p, "task1", "auto", "zh-CN")
    assert res.count == 0
    assert res.srt_path.exists()


def test_translate_texts_uses_explicit_remote_batch_size(monkeypatch):
    calls = []

    def fake_fallback(batch, source_lang, target_lang, api_key, **kwargs):
        calls.append(list(batch))
        return [f"译:{text}" for text in batch]

    monkeypatch.setattr(translator, "_translate_with_fallback", fake_fallback)
    monkeypatch.setattr(
        translator,
        "settings",
        SimpleNamespace(translate_batch_size=99, deepseek_api_key="legacy"),
    )

    translated = translator.translate_texts(
        ["a", "b", "c", "d", "e"], "en", "zh-CN",
        api_key="snapshot-key",
        engine_config=SimpleNamespace(
            api_type="openai_compatible", api_key="snapshot-key"
        ),
        batch_size=2,
    )

    assert translated == ["译:a", "译:b", "译:c", "译:d", "译:e"]
    assert calls == [["a", "b"], ["c", "d"], ["e"]]


def test_translate_texts_uses_explicit_local_batch_size(monkeypatch):
    calls = []

    class FakeManager:
        def translate_texts(self, texts, *, cancel_check=None):
            calls.append(list(texts))
            return [f"译:{text}" for text in texts]

    monkeypatch.setattr(translator, "get_translation_model_manager", FakeManager)
    monkeypatch.setattr(
        translator,
        "settings",
        SimpleNamespace(local_translate_batch_size=99),
    )

    translated = translator.translate_texts(
        ["a", "b", "c", "d", "e"], "en", "zh-CN",
        engine_config=SimpleNamespace(api_type="local_ct2"),
        local_batch_size=2,
    )

    assert translated == ["译:a", "译:b", "译:c", "译:d", "译:e"]
    assert calls == [["a", "b"], ["c", "d"], ["e"]]


def test_translation_fallback_prioritizes_cancellation(monkeypatch):
    from src.plugin_system import PluginCancelled

    monkeypatch.setattr(
        translator,
        "_translate_batch_with_retry",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            TranslateError("bad response", code="invalid_response")
        ),
    )

    def cancel_check():
        raise PluginCancelled("cancelled")

    with pytest.raises(PluginCancelled, match="cancelled"):
        translator._translate_with_fallback(
            ["hello"], "en", "zh-CN", "key", cancel_check=cancel_check,
        )


def test_lang_name_lookup_and_config_override(monkeypatch):
    """测试 ISO 语言名称的转换以及 SUBTRANS_LANG_NAMES 覆盖/扩展能力。"""
    from src.config.config import Settings, DEFAULT_LANG_NAMES
    # 内置新增常用语言测试
    assert translator._lang_name("id") == DEFAULT_LANG_NAMES["id"]
    assert translator._lang_name("hi") == DEFAULT_LANG_NAMES["hi"]
    assert translator._lang_name("nl") == DEFAULT_LANG_NAMES["nl"]
    assert translator._lang_name("uk") == DEFAULT_LANG_NAMES["uk"]
    assert translator._lang_name("unknown_code") == "unknown_code"

    # 测试通过 Settings 自定义 lang_names
    custom_s = SimpleNamespace(
        lang_names={
            "id": "Indonesian Custom",
            "custom-code": "Custom Language Name",
        }
    )
    monkeypatch.setattr(translator, "settings", custom_s)
    assert translator._lang_name("id") == "Indonesian Custom"
    assert translator._lang_name("custom-code") == "Custom Language Name"


# ---------- error code mapping & fast fail tests ----------

def test_call_deepseek_http_errors(fake_settings, monkeypatch):
    import httpx

    def mock_post_401(*a, **k):
        request = httpx.Request("POST", "https://api.deepseek.com")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)

    monkeypatch.setattr(httpx, "post", mock_post_401)
    with pytest.raises(TranslateError) as exc_info:
        translate_texts(["hello"], "en", "zh-CN", engine_config=fake_settings.engine_config)
    assert exc_info.value.code == "unauthorized"
    assert "HTTP 401" in str(exc_info.value)


def test_call_deepseek_rate_limit(fake_settings, monkeypatch):
    import httpx

    def mock_post_429(*a, **k):
        request = httpx.Request("POST", "https://api.deepseek.com")
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError("Rate Limit", request=request, response=response)

    monkeypatch.setattr(httpx, "post", mock_post_429)
    with pytest.raises(TranslateError) as exc_info:
        translate_texts(["hello"], "en", "zh-CN", engine_config=fake_settings.engine_config)
    assert exc_info.value.code == "rate_limited"


def test_call_deepseek_network_error(fake_settings, monkeypatch):
    import httpx

    def mock_post_network(*a, **k):
        raise httpx.RequestError("Network connection failed")

    monkeypatch.setattr(httpx, "post", mock_post_network)
    with pytest.raises(TranslateError) as exc_info:
        translate_texts(["hello"], "en", "zh-CN", engine_config=fake_settings.engine_config)
    assert exc_info.value.code == "network_error"


@pytest.mark.parametrize("status_code,expected_code", [
    (401, "unauthorized"),
    (403, "unauthorized"),
    (429, "rate_limited"),
])
def test_fast_fail_on_permanent_errors(fake_settings, monkeypatch, status_code, expected_code):
    import httpx
    called_count = 0

    def mock_post_err(*a, **k):
        nonlocal called_count
        called_count += 1
        request = httpx.Request("POST", "https://api.deepseek.com")
        response = httpx.Response(status_code, request=request)
        raise httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)

    monkeypatch.setattr(httpx, "post", mock_post_err)
    with pytest.raises(TranslateError) as exc_info:
        translate_texts(["a", "b", "c", "d"], "en", "zh-CN", engine_config=fake_settings.engine_config)
    assert exc_info.value.code == expected_code
    # 应立即抛出，不触发拆分重试 (只调用一次)
    assert called_count == 1


def test_fast_fail_on_insufficient_quota_error(fake_settings, monkeypatch):
    called_count = 0

    def fake_call(*a, **k):
        nonlocal called_count
        called_count += 1
        raise TranslateError("Quota exceeded", code="insufficient_quota")

    monkeypatch.setattr(translator, "_call_deepseek", fake_call)
    with pytest.raises(TranslateError) as exc_info:
        translate_texts(["a", "b", "c", "d"], "en", "zh-CN", engine_config=fake_settings.engine_config)
    assert exc_info.value.code == "insufficient_quota"
    assert called_count == 1


def test_transient_error_continues_to_halve(fake_settings, monkeypatch):
    called_count = 0
    sleeps = []

    def fake_call(*a, **k):
        nonlocal called_count
        called_count += 1
        raise TranslateError("Transient upstream error", code="upstream_error")

    monkeypatch.setattr(translator, "_call_deepseek", fake_call)
    monkeypatch.setattr(translator.time, "sleep", sleeps.append)
    with pytest.raises(TranslateError) as exc_info:
        translate_texts(["a", "b"], "en", "zh-CN", engine_config=fake_settings.engine_config)
    assert exc_info.value.code == "upstream_error"
    # 5xx 只在原批次上做有限退避重试，不再拆分成单条请求。
    assert called_count == 4
    assert sleeps == [0.5] * 10 + [0.5] * 30 + [0.5] * 120


def test_upstream_retry_sleep_honors_cancellation(fake_settings, monkeypatch):
    def fake_batch(*args, **kwargs):
        raise TranslateError("Transient upstream error", code="upstream_error")

    monkeypatch.setattr(translator, "_translate_batch", fake_batch)
    sleeps = []
    monkeypatch.setattr(translator.time, "sleep", sleeps.append)
    checks = 0

    def cancel_check():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        translator._translate_batch_with_retry(
            ["a"], "en", "zh-CN", "test-key", cancel_check=cancel_check,
        )
    assert sleeps == [0.5]
    assert checks == 2


def test_network_error_retries_without_batch_fallback(fake_settings, monkeypatch):
    called_count = 0

    def fake_call(*a, **k):
        nonlocal called_count
        called_count += 1
        raise TranslateError("network down", code="network_error")

    monkeypatch.setattr(translator, "_call_deepseek", fake_call)
    with pytest.raises(TranslateError) as exc_info:
        translate_texts(["a", "b"], "en", "zh-CN", engine_config=fake_settings.engine_config)
    assert exc_info.value.code == "network_error"
    # 初次请求 + 3 次重试；失败后不把批次递归减半。
    assert called_count == 4


def test_single_item_failure_line_index_message(fake_settings, monkeypatch):
    called_count = 0

    def fake_call(*a, **k):
        nonlocal called_count
        called_count += 1
        raise TranslateError("JSON decode error", code="invalid_response")

    monkeypatch.setattr(translator, "_call_deepseek", fake_call)
    with pytest.raises(TranslateError) as exc_info:
        translate_texts(["a"], "en", "zh-CN", engine_config=fake_settings.engine_config)
    assert exc_info.value.code == "invalid_response"
    assert "第 1 条字幕翻译失败" in str(exc_info.value)
    assert called_count == 1
