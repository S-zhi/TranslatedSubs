"""实网集成测试：真实链接跑通 ①下载→②提取→③识别→④翻译→⑤烧录 全链路。

默认跳过。需显式开启并设环境变量：

    SUBTRANS_DEEPSEEK_API_KEY=sk-xxx SUBTRANS_LIVE_TEST=1 uv run pytest tests/test_live_pipeline.py -v -s

用 `format_selector="worst"` 拉最小清晰度，whisper 用 tiny 最快。
换示例视频时改 EXAMPLE_URL 即可。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from src.core import audio_extractor, downloader, transcriber, translator, subtitle_burner
from src.core.audio_extractor import extract_audio
from src.core.downloader import download_video
from src.core.subtitle_burner import BurnError, burn_subtitles
from src.core.transcriber import transcribe
from src.core.translator import translate_srt

LIVE = os.getenv("SUBTRANS_LIVE_TEST") == "1"
EXAMPLE_URL = "https://cn.pornhub.com/view_video.php?viewkey=6a3fcfc833753"

_FFMPEG_OK = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _load_env():
    """从项目根 .env 手动加载（不依赖 python-dotenv）。"""
    env_path = Path(__file__).resolve().parents[1] / ".env"
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


_load_env()


@pytest.mark.skipif(not LIVE, reason="需 SUBTRANS_LIVE_TEST=1 才跑真实下载")
@pytest.mark.skipif(not _FFMPEG_OK, reason="需要 ffmpeg / ffprobe")
def test_live_pipeline_full(tmp_path, monkeypatch):
    """五步全链路：下载 → 提取 → 识别 → 翻译 → 烧录。"""
    work = tmp_path / "live"
    work.mkdir()

    # 目录隔离：不污染项目 data/
    for mod in (downloader, audio_extractor, transcriber, translator, subtitle_burner):
        monkeypatch.setattr(mod, "ensure_task_dir", lambda _task_id, d=work: d)

    key = os.getenv("SUBTRANS_DEEPSEEK_API_KEY")
    if not key:
        pytest.skip("未设置 SUBTRANS_DEEPSEEK_API_KEY")

    # ① 下载（worst 清晰度，尽量小，有音轨）
    dl_progress = []
    res = download_video(
        EXAMPLE_URL, "live",
        on_progress=dl_progress.append,
        format_selector="worst[ext=mp4]",
    )
    assert res.video_path.exists()
    assert res.filesize and res.filesize > 0
    assert res.source_url == EXAMPLE_URL
    assert dl_progress, "应至少收到一次下载进度回调"
    print(f"\n① 下载完成: {res.title} ({res.filesize/1024/1024:.1f}MB)")

    # ② 提取音频
    au_progress = []
    audio = extract_audio(res.video_path, "live", on_progress=au_progress.append)
    assert audio.audio_path.exists()
    assert audio.sample_rate == 16000 and audio.channels == 1
    assert audio.filesize and audio.filesize > 0
    print(f"② 提取完成: {audio.filesize/1024/1024:.2f}MB, {audio.duration:.1f}s")

    # ③ 语音识别（本地 tiny.en；本地文件上传）
    tr_progress = []
    subs = transcribe(audio.audio_path, "live", model_name="tiny.en", language="en", on_progress=tr_progress.append)
    assert subs.srt_path.exists()
    assert subs.segment_count > 0
    assert subs.language
    print(f"③ 识别完成: {subs.language}, {subs.segment_count} 条字幕")

    # ④ 翻译（DeepSeek）
    tl_progress = []
    tl = translate_srt(
        subs.srt_path, "live",
        source_lang=subs.language, target_lang="zh-CN",
        mode="mono", on_progress=tl_progress.append, api_key=key,
    )
    assert tl.srt_path.exists()
    assert tl.count == subs.segment_count
    print(f"④ 翻译完成: {tl.count} 条")

    # ⑤ 烧录（硬字幕）
    bn_progress = []
    bn = burn_subtitles(
        res.video_path, tl.srt_path, "live",
        mode="hard", on_progress=bn_progress.append,
    )
    assert bn.output_path.exists()
    assert bn.output_path.stat().st_size > 0
    print(f"⑤ 烧录完成: {bn.output_path.stat().st_size/1024/1024:.1f}MB")

    print(f"\n全部通过: {bn.output_path}")


@pytest.mark.skipif(not LIVE, reason="需 SUBTRANS_LIVE_TEST=1")
@pytest.mark.skipif(not _FFMPEG_OK, reason="需要 ffmpeg / ffprobe")
def test_live_translate_and_burn(tmp_path, monkeypatch):
    """聚焦 ④翻译 + ⑤烧录：真实 DeepSeek + 真实 ffmpeg，小输入快速验证。"""
    import subprocess
    from src.core.srt_utils import Subtitle, write_srt, parse_srt

    work = tmp_path / "tb"
    work.mkdir()
    for mod in (translator, subtitle_burner):
        monkeypatch.setattr(mod, "ensure_task_dir", lambda _tid, d=work: d)

    key = os.getenv("SUBTRANS_DEEPSEEK_API_KEY")
    if not key:
        pytest.skip("未设置 SUBTRANS_DEEPSEEK_API_KEY")

    # 造一段真实英文字幕（时间轴落在 0-6s）
    original = work / "original.srt"
    write_srt([
        Subtitle(1, 0.0, 2.0, "Hello, how are you today?"),
        Subtitle(2, 2.0, 4.0, "I am doing great, thank you."),
        Subtitle(3, 4.0, 6.0, "Let's get started with the show."),
    ], original)

    # ④ 翻译（真实 DeepSeek）
    tl = translate_srt(original, "tb", source_lang="en", target_lang="zh-CN", mode="mono", api_key=key)
    assert tl.srt_path.exists()
    assert tl.count == 3
    translated = parse_srt(tl.srt_path)
    # 时间轴必须原样保留
    assert translated[0].start == 0.0 and translated[0].end == 2.0
    assert translated[2].start == 4.0 and translated[2].end == 6.0
    # 译文应含中文字符
    assert any("一" <= c <= "鿿" for c in translated[0].text)
    print("\n④ 翻译完成（真实 DeepSeek）:")
    for s in translated:
        print(f"   [{s.start:.0f}-{s.end:.0f}s] {s.text}")

    # 造一段 6 秒带音轨的合成视频
    video = work / "source.mp4"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-f", "lavfi", "-i", "testsrc=duration=6:size=320x180:rate=15",
        "-shortest", str(video),
    ], check=True, capture_output=True)

    # ⑤a 硬烧录（重编码，字幕烧进画面）——需 libass
    from src.core.ffmpeg_utils import has_subtitles_filter
    if has_subtitles_filter():
        bn_hard = burn_subtitles(video, tl.srt_path, "tb", mode="hard")
        assert bn_hard.output_path.exists() and bn_hard.filesize > 0
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
             "stream=codec_type", "-of", "csv=p=0", str(bn_hard.output_path)],
            capture_output=True, text=True,
        )
        assert "video" in probe.stdout
        print(f"⑤a 硬烧录完成: {bn_hard.filesize/1024:.0f}KB")
    else:
        # 缺 libass：应抛清晰错误
        with pytest.raises(BurnError, match="libass"):
            burn_subtitles(video, tl.srt_path, "tb", mode="hard")
        print("⑤a 硬烧录跳过: 当前 ffmpeg 无 libass（已给出清晰报错）")

    # ⑤b 软字幕（内封可开关，应有字幕流）
    bn_soft = burn_subtitles(video, tl.srt_path, "tb", mode="soft")
    assert bn_soft.output_path.exists()
    sub_probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
         "stream=codec_name", "-of", "csv=p=0", str(bn_soft.output_path)],
        capture_output=True, text=True,
    )
    assert sub_probe.stdout.strip(), "软字幕产物应包含字幕流"
    print(f"⑤b 软字幕完成: 字幕流={sub_probe.stdout.strip()}")
    print("\n④+⑤ 全部通过")
