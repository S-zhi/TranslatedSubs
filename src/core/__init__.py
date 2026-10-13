"""核心业务层：流水线各阶段（下载 / 提取音频 / 识别 / 翻译 / 烧录）。

每个模块遵循「输入明确、输出明确」的解耦原则，可单独测试、单独重跑。
"""

from .downloader import (
    DownloadError,
    DownloadProgress,
    DownloadResult,
    download_video,
)
from .audio_extractor import (
    AudioExtractError,
    AudioProgress,
    AudioResult,
    extract_audio,
)
from .srt_utils import (
    Subtitle,
    decode_srt_bytes,
    format_timestamp,
    parse_srt,
    parse_timestamp,
    read_srt_content,
    write_srt,
)
from .transcriber import (
    TranscribeError,
    TranscribeProgress,
    TranscribeResult,
    TranscribeRequest,
    TranscribeResponse,
    TranscriberBackend,
    TranscriberService,
    HttpTranscriber,
    LocalWhisperTranscriber,
    transcribe,
)
from .translator import (
    TranslateError,
    TranslateProgress,
    TranslateResult,
    translate_srt,
    translate_texts,
)
from .subtitle_burner import (
    BurnError,
    BurnProgress,
    BurnResult,
    burn_subtitles,
)
from .vocal_separator import (
    VocalSeparationError,
    VocalSeparationCancelledError,
    separate_vocals,
)

__all__ = [
    # ① 下载
    "download_video", "DownloadResult", "DownloadProgress", "DownloadError",
    # ② 提取音频
    "extract_audio", "AudioResult", "AudioProgress", "AudioExtractError",
    # SRT 工具
    "Subtitle", "decode_srt_bytes", "format_timestamp", "parse_timestamp", "read_srt_content", "write_srt", "parse_srt",
    # ③ 语音识别
    "transcribe", "TranscribeResult", "TranscribeProgress", "TranscribeError",
    "TranscribeRequest", "TranscribeResponse", "TranscriberBackend", "TranscriberService",
    "HttpTranscriber",
    "LocalWhisperTranscriber",
    # ④ 翻译
    "translate_srt", "translate_texts", "TranslateResult", "TranslateProgress", "TranslateError",
    # ⑤ 烧录
    "burn_subtitles", "BurnResult", "BurnProgress", "BurnError",
    "separate_vocals", "VocalSeparationError", "VocalSeparationCancelledError",
]
