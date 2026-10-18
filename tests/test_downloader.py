"""① 下载视频 的单元测试。

全程 mock yt_dlp，不依赖网络：验证产物路径解析、结果构造、错误包装、
进度回调适配等纯逻辑。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError as YtDlpDownloadError

from src.core import downloader
from src.core.downloader import (
    DownloadError,
    DownloadProgress,
    download_video,
    _make_progress_adapter,
    _resolve_output_path,
)


def make_fake_ydl(on_extract):
    """构造一个可作为上下文管理器使用的假 YoutubeDL，extract_info 行为由回调决定。"""

    class _FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return on_extract(url, download, self.opts)

    return _FakeYDL


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    """每个单测运行前自动清空探针缓存，规避单测间因相同伪 URL 产生的跨测试缓存影响。"""
    clear_probe_cache()
    yield
    clear_probe_cache()


@pytest.fixture
def task_path(tmp_path, monkeypatch):
    """隔离任务目录：patch downloader.ensure_task_dir 指向临时目录。"""
    d = tmp_path / "task1"
    d.mkdir()
    monkeypatch.setattr(downloader, "ensure_task_dir", lambda task_id: d)
    return d


# ---------- download_video 主流程 ----------

def test_download_success(task_path, monkeypatch):
    src = task_path / "source.mp4"

    def on_extract(url, download, opts):
        assert download is True
        src.write_bytes(b"fake video data")
        return {
            "title": "My Video",
            "duration": 12.5,
            "width": 640,
            "height": 360,
            "requested_downloads": [{"filepath": str(src)}],
        }

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    res = download_video("http://example.com/v", "task1")

    assert res.video_path == src
    assert res.title == "My Video"
    assert res.duration == 12.5
    assert res.ext == "mp4"
    assert res.width == 640 and res.height == 360
    assert res.filesize == len(b"fake video data")
    assert res.source_url == "http://example.com/v"


def test_download_title_falls_back_to_stem(task_path, monkeypatch):
    src = task_path / "source.mp4"

    def on_extract(url, download, opts):
        src.write_bytes(b"x")
        return {"requested_downloads": [{"filepath": str(src)}]}  # 无 title

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    res = download_video("http://x", "task1")
    assert res.title == "source"


def test_download_wraps_ytdlp_error(task_path, monkeypatch):
    def on_extract(url, download, opts):
        raise YtDlpDownloadError("boom")

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    with pytest.raises(DownloadError, match="下载失败"):
        download_video("http://x", "task1")


def test_download_wraps_generic_error(task_path, monkeypatch):
    def on_extract(url, download, opts):
        raise ValueError("unexpected")

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    with pytest.raises(DownloadError, match="出错"):
        download_video("http://x", "task1")


def test_download_missing_output_file(task_path, monkeypatch):
    def on_extract(url, download, opts):
        # 返回一个不存在的路径，模拟"下载完成但产物缺失"
        return {"requested_downloads": [{"filepath": str(task_path / "nope.mp4")}]}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    with pytest.raises(DownloadError, match="未找到产物"):
        download_video("http://x", "task1")


def test_download_passes_progress_hook(task_path, monkeypatch):
    """传入 on_progress 时，opts 里应注册 progress_hooks。"""
    src = task_path / "source.mp4"
    captured = {}

    def on_extract(url, download, opts):
        captured["opts"] = opts
        src.write_bytes(b"x")
        return {"requested_downloads": [{"filepath": str(src)}]}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    download_video("http://x", "task1", on_progress=lambda p: None)
    assert "progress_hooks" in captured["opts"]
    assert len(captured["opts"]["progress_hooks"]) == 1
    assert captured["opts"]["concurrent_fragment_downloads"] == (
        downloader.settings.download_concurrent_fragments
    )


def test_download_explicit_none_and_frozen_options_override_settings(task_path, monkeypatch):
    import dataclasses

    src = task_path / "source.mkv"
    captured = {}
    monkeypatch.setattr(
        downloader,
        "settings",
        dataclasses.replace(
            downloader.settings,
            _download_proxy="https://global-proxy.example",
            _cookies_file=task_path / "global-cookies.txt",
            _download_socket_timeout=90,
            _merge_output_format="mp4",
            _download_retries=10,
            _download_concurrent_fragments=12,
        ),
    )

    def on_extract(url, download, opts):
        captured.update(opts)
        src.write_bytes(b"x")
        return {"requested_downloads": [{"filepath": str(src)}]}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    download_video(
        "https://example.test/video", "task1",
        cookies_file=None,
        proxy=None,
        socket_timeout=17,
        merge_output_format="mkv",
        retries=4,
        concurrent_fragments=3,
    )

    assert "cookiefile" not in captured
    assert "proxy" not in captured
    assert captured["socket_timeout"] == 17
    assert captured["merge_output_format"] == "mkv"
    assert captured["retries"] == 4
    assert captured["concurrent_fragment_downloads"] == 3


def test_download_progress_cancellation_is_not_swallowed(task_path, monkeypatch):
    from src.plugin_system import PluginCancelled

    checks = 0

    def cancel_check():
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise PluginCancelled("cancelled")

    def on_extract(url, download, opts):
        opts["progress_hooks"][0]({
            "status": "downloading", "downloaded_bytes": 1, "total_bytes": 2,
        })
        return {}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    with pytest.raises(PluginCancelled, match="cancelled"):
        download_video(
            "https://example.test/video", "task1",
            on_progress=lambda progress: None,
            cancel_check=cancel_check,
        )


def test_download_limiter_releases_after_media_download(tmp_path, monkeypatch):
    """下载完成后立即释放名额，后续任务不必等待前一个任务的流水线结束。"""
    task_dirs = {}
    for task_id in ("task1", "task2", "task3"):
        task_dirs[task_id] = tmp_path / task_id
        task_dirs[task_id].mkdir()
    monkeypatch.setattr(downloader, "ensure_task_dir", lambda task_id: task_dirs[task_id])

    limiter = downloader._DownloadConcurrencyLimiter(lambda: 2)
    monkeypatch.setattr(downloader, "_download_limiter", limiter)

    lock = threading.Lock()
    first_two_started = threading.Event()
    release_downloads = threading.Event()
    active = 0
    max_active = 0
    started = []

    def on_extract(url, download, opts):
        nonlocal active, max_active
        task_id = url.rsplit("/", 1)[-1]
        output = task_dirs[task_id] / "source.mp4"
        with lock:
            active += 1
            max_active = max(max_active, active)
            started.append(task_id)
            if len(started) == 2:
                first_two_started.set()
        try:
            assert release_downloads.wait(timeout=2)
            output.write_bytes(task_id.encode())
            return {"requested_downloads": [{"filepath": str(output)}]}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(download_video, f"http://example/{task_id}", task_id)
            for task_id in ("task1", "task2", "task3")
        ]
        try:
            assert first_two_started.wait(timeout=2)
            time.sleep(0.05)
            assert len(started) == 2
        finally:
            release_downloads.set()
        assert [future.result().video_path.name for future in futures] == [
            "source.mp4",
            "source.mp4",
            "source.mp4",
        ]

    assert max_active == 2
    assert limiter.active == 0


def test_download_limiter_releases_after_error(tmp_path, monkeypatch):
    """yt-dlp 失败也必须释放下载名额，避免后续任务永久阻塞。"""
    task_dirs = {"failed": tmp_path / "failed", "next": tmp_path / "next"}
    for path in task_dirs.values():
        path.mkdir()
    monkeypatch.setattr(downloader, "ensure_task_dir", lambda task_id: task_dirs[task_id])
    limiter = downloader._DownloadConcurrencyLimiter(lambda: 1)
    monkeypatch.setattr(downloader, "_download_limiter", limiter)

    calls = []

    def on_extract(url, download, opts):
        task_id = url.rsplit("/", 1)[-1]
        calls.append(task_id)
        if task_id == "failed":
            raise ValueError("network failed")
        output = task_dirs[task_id] / "source.mp4"
        output.write_bytes(b"ok")
        return {"requested_downloads": [{"filepath": str(output)}]}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    with pytest.raises(DownloadError, match="出错"):
        download_video("http://example/failed", "failed")

    result = download_video("http://example/next", "next")
    assert result.video_path.exists()
    assert calls == ["failed", "next"]
    assert limiter.active == 0


# ---------- _resolve_output_path 回退逻辑 ----------

def test_resolve_uses_requested_downloads(tmp_path):
    target = tmp_path / "a.mp4"
    info = {"requested_downloads": [{"filepath": str(target)}]}
    assert _resolve_output_path(info, tmp_path) == target


def test_resolve_uses_top_level_filepath(tmp_path):
    target = tmp_path / "b.mp4"
    info = {"filepath": str(target)}
    assert _resolve_output_path(info, tmp_path) == target


def test_resolve_glob_prefers_merge_container(tmp_path):
    (tmp_path / "source.mp4").write_bytes(b"x")
    assert _resolve_output_path({}, tmp_path) == tmp_path / "source.mp4"


def test_resolve_glob_other_ext(tmp_path):
    (tmp_path / "source.mkv").write_bytes(b"x")
    assert _resolve_output_path({}, tmp_path) == tmp_path / "source.mkv"


def test_resolve_returns_none_when_nothing(tmp_path):
    assert _resolve_output_path({}, tmp_path) is None


# ---------- _make_progress_adapter 进度映射 ----------

def test_progress_downloading_with_total():
    seen = []
    hook = _make_progress_adapter(seen.append)
    hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200})
    assert len(seen) == 1
    p = seen[0]
    assert isinstance(p, DownloadProgress)
    assert p.percent == 25.0
    assert p.total_bytes == 200
    assert p.downloaded_bytes == 50


def test_progress_uses_estimate_when_no_total():
    seen = []
    hook = _make_progress_adapter(seen.append)
    hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes_estimate": 100})
    assert seen[0].percent == 50.0


def test_progress_finished_is_100():
    seen = []
    hook = _make_progress_adapter(seen.append)
    hook({"status": "finished", "downloaded_bytes": 200, "total_bytes": 200})
    assert seen[0].percent == 100.0
    assert seen[0].status == "finished"


def test_progress_ignores_unknown_status():
    seen = []
    hook = _make_progress_adapter(seen.append)
    hook({"status": "error"})
    assert seen == []


def test_progress_swallows_callback_exception():
    def bad(_p):
        raise RuntimeError("callback blew up")

    hook = _make_progress_adapter(bad)
    # 不应抛出
    hook({"status": "finished"})


# ---------- probe_video / 探针辅助函数 ----------

from src.core.downloader import (
    ProbeResult,
    _clip_error,
    _count_formats,
    _is_probe_url,
    _probe_failure_reason,
    clear_probe_cache,
    probe_video,
)


# ---------- _is_probe_url URL 校验 ----------

def test_is_probe_url_accepts_http():
    assert _is_probe_url("http://example.com/v") is True


def test_is_probe_url_accepts_https():
    assert _is_probe_url("https://www.youtube.com/watch?v=abc") is True


def test_is_probe_url_rejects_empty_string():
    assert _is_probe_url("") is False


def test_is_probe_url_rejects_ftp():
    assert _is_probe_url("ftp://example.com/v") is False


def test_is_probe_url_rejects_file_scheme():
    assert _is_probe_url("file:///etc/passwd") is False


def test_is_probe_url_rejects_scheme_without_netloc():
    assert _is_probe_url("http://") is False


def test_is_probe_url_rejects_garbage_string():
    assert _is_probe_url("not a url") is False


# ---------- _count_formats 格式数统计 ----------

def test_count_formats_uses_formats_list():
    assert _count_formats({"formats": [{}, {}, {}, {}]}) == 4


def test_count_formats_uses_requested_formats_when_no_formats():
    assert _count_formats({"requested_formats": [{}, {}]}) == 2


def test_count_formats_falls_back_to_url_only():
    # 没有 formats/requested_formats 时，存在 url 也算 1
    assert _count_formats({"url": "https://cdn/stream"}) == 1


def test_count_formats_returns_zero_when_nothing():
    assert _count_formats({}) == 0
    assert _count_formats({"formats": "not a list", "url": None}) == 0


def test_count_formats_empty_lists():
    """formats 与 requested_formats 都是空列表、且无 url 时视为 0。"""
    assert _count_formats({"formats": [], "requested_formats": []}) == 0


# ---------- _probe_failure_reason 错误归类 ----------

def test_probe_failure_reason_unsupported_url():
    assert _probe_failure_reason("Unsupported URL: https://x/") == "yt-dlp 暂不支持这个网站或链接"


def test_probe_failure_reason_private_login_cookie():
    assert _probe_failure_reason("Private video. Sign in if you've access") == "该链接可能需要登录态或 cookies"


def test_probe_failure_reason_login_keyword():
    assert _probe_failure_reason("Login required") == "该链接可能需要登录态或 cookies"


def test_probe_failure_reason_no_video_formats():
    msg = "No video formats found"
    assert _probe_failure_reason(msg) == "未找到匹配当前配置的可下载格式"


def test_probe_failure_reason_requested_format_not_available():
    msg = "The requested format is not available"
    assert _probe_failure_reason(msg) == "未找到匹配当前配置的可下载格式"


def test_probe_failure_reason_404():
    assert _probe_failure_reason("HTTP Error 404: Not Found") == "视频不可用或链接已失效"


def test_probe_failure_reason_not_available_keyword():
    assert _probe_failure_reason("Video is not available in your region") == "视频不可用或链接已失效"


def test_probe_failure_reason_default_fallback():
    assert _probe_failure_reason("Some other random failure") == "yt-dlp 无法解析这个链接"


# ---------- _clip_error 截断 ----------

def test_clip_error_short_passes_through():
    assert _clip_error("boom") == "boom"


def test_clip_error_truncates_to_500():
    msg = "x" * 800
    out = _clip_error(msg)
    assert len(out) == 500
    assert out == "x" * 500


def test_clip_error_custom_limit():
    out = _clip_error("abcdefghij", limit=3)
    assert out == "abc"


# ---------- probe_video 主流程 ----------

def test_probe_rejects_invalid_url_without_calling_ydl(monkeypatch):
    """非法 URL 应在调用 yt-dlp 之前直接返回 ok=False。"""
    called = []

    class _ShouldNotUse:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, *a, **k):
            called.append((a, k))
            return {}

    monkeypatch.setattr(downloader, "YoutubeDL", _ShouldNotUse)

    res = probe_video("not a url")
    assert res.ok is False
    assert res.reason == "请输入有效的视频链接"
    assert called == []


def test_probe_success_with_formats(monkeypatch):
    """解析出 formats 列表 -> ok=True 且 formats_count 与元信息透传。"""

    def on_extract(url, download, opts):
        # 探针模式必须传 download=False
        assert download is False
        # 探测场景应不开 check_formats（见 downloader.py 注释）
        assert "check_formats" not in opts
        return {
            "title": "Probe Title",
            "extractor_key": "Youtube",
            "duration": 123.4,
            "webpage_url": "https://example.com/v",
            "formats": [{}, {}, {}],
        }

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    res = probe_video("https://example.com/v")
    assert res.ok is True
    assert res.title == "Probe Title"
    assert res.extractor == "Youtube"
    assert res.duration == 123.4
    assert res.formats_count == 3
    assert res.webpage_url == "https://example.com/v"
    assert res.reason is None and res.detail is None


def test_probe_success_falls_back_to_extractor(monkeypatch):
    """无 extractor_key 时回退到 extractor。"""

    def on_extract(url, download, opts):
        return {"title": "T", "extractor": "Generic", "url": "https://x"}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    res = probe_video("https://x/v")
    assert res.ok is True
    assert res.extractor == "Generic"
    assert res.formats_count == 1


def test_probe_no_formats_returns_failure_with_metadata(monkeypatch):
    """formats_count=0 且无 url -> ok=False，并把元信息回填。"""

    def on_extract(url, download, opts):
        return {"title": "T", "extractor_key": "X", "duration": 10, "webpage_url": "https://x/v"}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    res = probe_video("https://x/v")
    assert res.ok is False
    assert res.reason == "未找到可下载的视频格式"
    # 元信息回填：title / extractor / duration / webpage_url
    assert res.title == "T" and res.extractor == "X" and res.duration == 10
    assert res.webpage_url == "https://x/v"


def test_probe_no_formats_falls_back_webpage_url_to_input(monkeypatch):
    """解析结果无 webpage_url 时回填到入参 URL。"""

    def on_extract(url, download, opts):
        return {"title": "T"}  # 无 formats / 无 url / 无 webpage_url

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    res = probe_video("https://x/v")
    assert res.ok is False
    assert res.webpage_url == "https://x/v"


def test_probe_wraps_ytdlp_download_error(monkeypatch):
    """yt-dlp DownloadError 归类失败原因并把原文裁剪后放到 detail。"""

    def on_extract(url, download, opts):
        raise YtDlpDownloadError("Unsupported URL: https://x/")

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    res = probe_video("https://x/")
    assert res.ok is False
    assert res.reason == "yt-dlp 暂不支持这个网站或链接"
    assert res.detail == "Unsupported URL: https://x/"


def test_probe_wraps_generic_exception(monkeypatch):
    """非 yt-dlp 异常走通用兜底：reason=链接探测失败，detail=裁剪后原文。"""

    def on_extract(url, download, opts):
        raise ValueError("network gone")

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    res = probe_video("https://x/")
    assert res.ok is False
    assert res.reason == "链接探测失败"
    assert res.detail == "network gone"


def test_probe_truncates_long_error_detail(monkeypatch):
    """底层长错误应被裁剪到 500 字符以内。"""
    long_msg = "y" * 1000

    def on_extract(url, download, opts):
        raise YtDlpDownloadError(long_msg)

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    res = probe_video("https://x/")
    assert res.ok is False
    assert len(res.detail) == 500


def test_probe_passes_cookies_file(monkeypatch, tmp_path):
    """显式 cookies_file 应写入 cookiefile；settings.cookies_file 优先取本次参数。"""
    captured = {}
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape")

    def on_extract(url, download, opts):
        captured["opts"] = opts
        return {"title": "T", "url": "https://x"}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    probe_video("https://x/v", cookies_file=cookies)
    assert captured["opts"]["cookiefile"] == str(cookies)


def test_probe_explicit_none_and_frozen_transport_settings(monkeypatch):
    import dataclasses

    captured = {}
    monkeypatch.setattr(
        downloader,
        "settings",
        dataclasses.replace(
            downloader.settings,
            _download_proxy="https://global-proxy.example",
            _cookies_file=Path("/tmp/global-cookies.txt"),
            _download_socket_timeout=90,
        ),
    )

    def on_extract(url, download, opts):
        captured.update(opts)
        return {"title": "T", "url": "https://example.test/video"}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    probe_video(
        "https://example.test/video",
        cookies_file=None,
        format_selector="bv*[height<=720]+ba/b[height<=720]",
        proxy=None,
        socket_timeout=19,
    )

    assert "cookiefile" not in captured
    assert "proxy" not in captured
    assert captured["format"] == "bv*[height<=720]+ba/b[height<=720]"
    assert captured["socket_timeout"] == 19


def test_probe_uses_browser_cookies_without_cookiefile_or_cache(monkeypatch):
    """浏览器 Cookie 只用于显式 probe，不与文件 Cookie 合并，也不写入缓存。"""
    import dataclasses

    captured = {}
    calls = []
    monkeypatch.setattr(
        downloader,
        "settings",
        dataclasses.replace(downloader.settings, _allow_cookies_from_browser=True),
    )

    def on_extract(url, download, opts):
        calls.append(url)
        captured["opts"] = opts
        return {"title": "T", "url": "https://x"}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    first = probe_video("https://x/v", cookies_from_browser="chrome", ttl_sec=60)
    second = probe_video("https://x/v", cookies_from_browser="chrome", ttl_sec=60)

    assert first.ok is True and second.ok is True
    assert second.cached is False
    assert len(calls) == 2
    assert captured["opts"]["cookiesfrombrowser"] == ("chrome",)
    assert "cookiefile" not in captured["opts"]


def test_probe_rejects_unsupported_browser_cookie_source(monkeypatch):
    called = False

    def on_extract(url, download, opts):
        nonlocal called
        called = True
        return {"title": "T", "url": "https://x"}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    result = probe_video("https://x/v", cookies_from_browser="/tmp/profile")
    assert result.ok is False
    assert result.reason == "不支持的浏览器 Cookie 来源"
    assert called is False


def test_probe_browser_cookie_errors_do_not_expose_profile_path(monkeypatch):
    import dataclasses

    monkeypatch.setattr(
        downloader,
        "settings",
        dataclasses.replace(downloader.settings, _allow_cookies_from_browser=True),
    )

    def on_extract(url, download, opts):
        raise YtDlpDownloadError("Could not open /Users/alice/Library/Cookies.sqlite")

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    result = probe_video("https://x/v", cookies_from_browser="chrome", ttl_sec=0)
    assert result.ok is False
    assert "/Users/alice" not in (result.detail or "")
    assert "Cookies.sqlite" not in (result.detail or "")


    """format_selector 覆盖 settings.download_format。"""
    captured = {}

    def on_extract(url, download, opts):
        captured["opts"] = opts
        return {"title": "T", "url": "https://x"}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    probe_video("https://x/v", format_selector="best[height<=480]")
    assert captured["opts"]["format"] == "best[height<=480]"


def test_probe_uses_settings_format_when_no_override(monkeypatch):
    """未传 format_selector 时退到 settings.download_format。"""
    captured = {}

    def on_extract(url, download, opts):
        captured["opts"] = opts
        return {"title": "T", "url": "https://x"}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    probe_video("https://x/v")
    assert captured["opts"]["format"] == downloader.settings.download_format


def test_probe_dataclass_defaults():
    """ProbeResult 仅 ok 必填，其它字段均有默认值。"""
    r = ProbeResult(ok=True)
    assert r.title is None
    assert r.extractor is None
    assert r.duration is None
    assert r.formats_count == 0
    assert r.webpage_url is None
    assert r.reason is None
    assert r.detail is None
    assert r.cached is False


def test_probe_video_ttl_caching(monkeypatch):
    """probe_video 针对相同 URL 支持 TTL 内缓存，再次调用返回 cached=True 且不重新调 YoutubeDL。"""
    clear_probe_cache()
    calls = []

    def on_extract(url, download, opts):
        calls.append(url)
        return {"title": "Cached Title", "formats": [{}]}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    url = "https://example.com/video1"
    res1 = probe_video(url, ttl_sec=60)
    assert res1.ok is True
    assert res1.title == "Cached Title"
    assert res1.cached is False
    assert len(calls) == 1

    # 再次探测同一 URL，命中缓存
    res2 = probe_video(url, ttl_sec=60)
    assert res2.ok is True
    assert res2.title == "Cached Title"
    assert res2.cached is True
    assert len(calls) == 1  # 依然是 1，未调 extract_info

    # 强制刷新 force_refresh=True 绕过缓存
    res3 = probe_video(url, ttl_sec=60, force_refresh=True)
    assert res3.ok is True
    assert res3.cached is False
    assert len(calls) == 2

    # 清空缓存后再次调用
    clear_probe_cache()
    res4 = probe_video(url, ttl_sec=60)
    assert res4.ok is True
    assert res4.cached is False
    assert len(calls) == 3


def test_probe_cache_keeps_distinct_downloader_network_configs(monkeypatch):
    calls = []

    def on_extract(url, download, opts):
        calls.append((opts.get("proxy"), opts["socket_timeout"]))
        return {"title": str(calls[-1]), "formats": [{}]}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    url = "https://example.com/network-config"
    first = probe_video(url, ttl_sec=60, proxy="http://proxy-a:8080", socket_timeout=5)
    assert first.cached is False
    assert probe_video(url, ttl_sec=60, proxy="http://proxy-a:8080", socket_timeout=5).cached is True
    assert probe_video(url, ttl_sec=60, proxy="http://proxy-a:8080", socket_timeout=30).cached is False
    assert probe_video(url, ttl_sec=60, proxy="http://proxy-b:8080", socket_timeout=5).cached is False
    assert calls == [("http://proxy-a:8080", 5), ("http://proxy-a:8080", 30), ("http://proxy-b:8080", 5)]


def test_probe_video_ttl_expiry(monkeypatch):
    """TTL 过期后，probe_video 重新调用 YoutubeDL。"""
    import time
    clear_probe_cache()
    calls = []

    def on_extract(url, download, opts):
        calls.append(url)
        return {"title": "Expiry Test", "formats": [{}]}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    now = 1000.0
    monkeypatch.setattr(time, "time", lambda: now)

    url = "https://example.com/video2"
    res1 = probe_video(url, ttl_sec=10)
    assert res1.cached is False
    assert len(calls) == 1

    # 在 TTL 内 (now = 1005s)
    now = 1005.0
    res2 = probe_video(url, ttl_sec=10)
    assert res2.cached is True
    assert len(calls) == 1

    # 超过 TTL (now = 1011s)
    now = 1011.0
    res3 = probe_video(url, ttl_sec=10)
    assert res3.cached is False
    assert len(calls) == 2


def test_probe_cache_evicts_oldest_entry_when_full(monkeypatch):
    """探测缓存超过容量时淘汰最旧条目，避免模块级字典无界增长。"""
    calls = []

    def on_extract(url, download, opts):
        calls.append(url)
        return {"title": url, "formats": [{}]}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    monkeypatch.setattr(downloader, "_PROBE_CACHE_MAX_SIZE", 2)

    probe_video("https://example.com/old", ttl_sec=60)
    probe_video("https://example.com/hot", ttl_sec=60)
    assert probe_video("https://example.com/hot", ttl_sec=60).cached is True
    probe_video("https://example.com/new", ttl_sec=60)

    assert len(downloader._probe_cache) == 2
    assert probe_video("https://example.com/old", ttl_sec=60).cached is False
    assert len(calls) == 4


def test_probe_cache_removes_expired_entry_before_inserting(monkeypatch):
    """过期 key 在重新探测前移除，确保容量由有效访问持续维护。"""
    def on_extract(url, download, opts):
        return {"title": url, "formats": [{}]}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    monkeypatch.setattr(downloader, "_PROBE_CACHE_MAX_SIZE", 2)
    current_time = iter((1000.0, 1002.0, 1002.0))
    monkeypatch.setattr(time, "time", lambda: next(current_time))

    probe_video("https://example.com/expired", ttl_sec=1)
    probe_video("https://example.com/other", ttl_sec=60)
    probe_video("https://example.com/expired", ttl_sec=1)

    assert len(downloader._probe_cache) == 2
    assert any(key[0] == "https://example.com/expired" for key in downloader._probe_cache)


def test_probe_sniffs_language_from_metadata(monkeypatch):
    def on_extract(url, download, opts):
        return {"title": "Japanese", "language": "ja-JP", "formats": [{}]}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    downloader.clear_probe_cache()
    result = downloader.probe_video("https://example.com/lang", ttl_sec=0)
    assert result.language == "ja"


def test_resolve_download_format_presets():
    assert downloader.resolve_download_format(quality="best") == "bv*+ba/b"
    assert downloader.resolve_download_format(quality="1080p") == "bv*[height<=1080]+ba/b[height<=1080]"
    assert downloader.resolve_download_format(quality="720p") == "bv*[height<=720]+ba/b[height<=720]"
    assert downloader.resolve_download_format(quality="480p") == "bv*[height<=480]+ba/b[height<=480]"
    assert downloader.resolve_download_format(quality="audio_only") == "ba/b"
    assert downloader.resolve_download_format(quality="1440p") == "bv*[height<=1440]+ba/b[height<=1440]"
    assert downloader.resolve_download_format(format_selector="worst") == "worst"


def test_extract_supported_contents():
    fake_info = {
        "title": "Sample",
        "thumbnail": "https://img.example.com/cover.jpg",
        "uploader": "Sample Creator",
        "formats": [
            {"format_id": "1", "ext": "mp4", "height": 1080, "width": 1920, "vcodec": "avc1", "acodec": "mp4a", "tbr": 2500, "filesize": 1000000},
            {"format_id": "2", "ext": "mp4", "height": 720, "width": 1280, "vcodec": "avc1", "acodec": "mp4a", "tbr": 1200, "filesize": 500000},
            {"format_id": "3", "ext": "mp4", "height": 480, "width": 854, "vcodec": "avc1", "acodec": "mp4a", "tbr": 600, "filesize": 250000},
            {"format_id": "audio", "ext": "m4a", "height": None, "vcodec": "none", "acodec": "mp4a", "abr": 128, "filesize": 50000},
        ],
    }
    qualities, formats = downloader.extract_supported_contents(fake_info)
    assert "best" in qualities
    assert "1080p" in qualities
    assert "720p" in qualities
    assert "480p" in qualities
    assert "audio_only" in qualities
    assert len(formats) == 4
    assert formats[0]["resolution"] == "1080p"
    assert formats[3]["resolution"] == "音频"


def test_download_passes_quality_and_network_options(task_path, monkeypatch):
    src = task_path / "source.mp4"
    captured_opts = {}

    def on_extract(url, download, opts):
        captured_opts.update(opts)
        src.write_bytes(b"data")
        return {"requested_downloads": [{"filepath": str(src)}]}

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))

    res = download_video(
        "http://x",
        "task1",
        quality="1080p",
        proxy="http://127.0.0.1:7890",
        socket_timeout=45,
    )
    assert res.video_path == src
    assert captured_opts["format"] == "bv*[height<=1080]+ba/b[height<=1080]"
    assert captured_opts["proxy"] == "http://127.0.0.1:7890"
    assert captured_opts["socket_timeout"] == 45
    assert captured_opts["fragment_retries"] == 10
    assert captured_opts["skip_unavailable_fragments"] is True


def test_probe_video_extracts_supported_contents(monkeypatch):
    fake_info = {
        "title": "Probe Sample",
        "thumbnail": "https://img.example.com/thumb.jpg",
        "uploader": "Test Channel",
        "formats": [
            {"format_id": "v1080", "ext": "mp4", "height": 1080, "vcodec": "avc1", "acodec": "mp4a", "tbr": 2000},
            {"format_id": "a1", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 128},
        ],
    }

    def on_extract(url, download, opts):
        return fake_info

    monkeypatch.setattr(downloader, "YoutubeDL", make_fake_ydl(on_extract))
    downloader.clear_probe_cache()

    result = downloader.probe_video("https://example.com/content-test", ttl_sec=0)
    assert result.ok is True
    assert result.thumbnail == "https://img.example.com/thumb.jpg"
    assert result.uploader == "Test Channel"
    assert "1080p" in result.available_qualities
    assert "audio_only" in result.available_qualities
    assert len(result.formats) == 2
