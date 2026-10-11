"""全局配置。

支持通过环境变量覆盖，方便在不同机器 / 部署环境调整，
本机开发直接用默认值即可。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_LANG_NAMES: dict[str, str] = {
    "auto": "the source language",
    "zh-CN": "Simplified Chinese (简体中文)",
    "zh-TW": "Traditional Chinese (繁體中文)",
    "zh": "Chinese (中文)",
    "en": "English",
    "ja": "Japanese (日本語)",
    "ko": "Korean (한국어)",
    "es": "Spanish (Español)",
    "fr": "French (Français)",
    "de": "German (Deutsch)",
    "ru": "Russian (Русский)",
    "it": "Italian (Italiano)",
    "pt": "Portuguese (Português)",
    "vi": "Vietnamese (Tiếng Việt)",
    "th": "Thai (ไทย)",
    "ar": "Arabic (العربية)",
    "id": "Indonesian (Bahasa Indonesia)",
    "hi": "Hindi (हिन्दी)",
    "nl": "Dutch (Nederlands)",
    "pl": "Polish (Polski)",
    "tr": "Turkish (Türkçe)",
    "sv": "Swedish (Svenska)",
    "uk": "Ukrainian (Українська)",
    "cs": "Czech (Čeština)",
    "da": "Danish (Dansk)",
    "fi": "Finnish (Suomi)",
    "el": "Greek (Ελληνικά)",
    "he": "Hebrew (עברית)",
    "hu": "Hungarian (Magyar)",
    "no": "Norwegian (Norsk)",
    "ro": "Romanian (Română)",
    "sk": "Slovak (Slovenčina)",
    "af": "Afrikaans",
    "ca": "Catalan (Català)",
    "bg": "Bulgarian (Български)",
    "hr": "Croatian (Hrvatski)",
    "ms": "Malay (Bahasa Melayu)",
    "fa": "Persian (فارسی)",
    "ur": "Urdu (اردو)",
    "bn": "Bengali (বাংলা)",
    "ta": "Tamil (தமிழ்)",
    "sw": "Swahili (Kiswahili)",
}

DISABLED_LANGUAGES = frozenset({"bn", "ur"})

# 项目根目录（本文件位于 src/config/config.py，向上两级）
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_CORS_ORIGINS = (
    "http://localhost:5273", "http://127.0.0.1:5273",
    "http://localhost:8000", "http://127.0.0.1:8000",
)
_DEFAULT_REPLICATE_MODEL = (
    "stayallive/whisper-subtitles:"
    "b97ba81004e7132181864c885a76cae0e56bc61caa4190a395f6d8ba45b7a969"
)


def _bootstrap_env() -> None:
    """在读取任何环境变量前，先把项目根 .env 加载进来。

    必须在 class Settings 定义之前调用：dataclass 的字段默认值
    （os.getenv(...)）是在类定义期求值的。这样 uvicorn / pytest / CLI
    各入口都能拿到 .env 里的 key，无需各自再 load。
    setdefault 语义：不覆盖外部已设置的环境变量。
    """
    env_path = _BACKEND_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and val:
            os.environ.setdefault(key, val)


_bootstrap_env()

_last_env_mtime: float = -1.0

# Settings are normally sourced from environment variables.  The web UI also
# needs safe, persistent runtime overrides so changing an audio or Replicate
# option does not require restarting the process (and does not get lost when
# the process has already loaded .env into os.environ).
_RUNTIME_SETTINGS_LOCK = threading.RLock()
_RUNTIME_SETTINGS_LOADED = False
_RUNTIME_SETTINGS_SOURCE: Optional[Path] = None
_RUNTIME_SETTINGS: dict[str, Any] = {}
_RUNTIME_SETTING_KEYS = {
    "vocal_separation_enabled",
    "vocal_separation_backend",
    "vocal_separation_model",
    "vocal_separation_threads",
    "vocal_separation_command",
    "vocal_separation_timeout",
    "replicate_api_token",
    "replicate_whisper_model",
    "replicate_timeout",
    "replicate_retries",
    "replicate_retry_interval",
    "replicate_poll_interval",
}
_RUNTIME_SECRET_KEYS = {"replicate_api_token"}


def _runtime_settings_path() -> Path:
    raw = os.getenv("SUBTRANS_RUNTIME_SETTINGS_FILE")
    if raw:
        return Path(raw).expanduser()
    data_dir = os.getenv("SUBTRANS_DATA_DIR")
    root = Path(data_dir).expanduser() if data_dir else _BACKEND_DIR / "data"
    return root / ".runtime-settings.json"


def _load_runtime_settings() -> None:
    global _RUNTIME_SETTINGS_LOADED, _RUNTIME_SETTINGS_SOURCE
    with _RUNTIME_SETTINGS_LOCK:
        path = _runtime_settings_path()
        if _RUNTIME_SETTINGS_LOADED and _RUNTIME_SETTINGS_SOURCE == path:
            return
        _RUNTIME_SETTINGS_LOADED = True
        _RUNTIME_SETTINGS_SOURCE = path
        _RUNTIME_SETTINGS.clear()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _RUNTIME_SETTINGS.update({k: data[k] for k in _RUNTIME_SETTING_KEYS if k in data})
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return


def get_runtime_settings() -> dict[str, Any]:
    """Return persisted runtime overrides (never includes secrets)."""
    _load_runtime_settings()
    with _RUNTIME_SETTINGS_LOCK:
        return {
            key: value
            for key, value in _RUNTIME_SETTINGS.items()
            if key not in _RUNTIME_SECRET_KEYS
        }


def update_runtime_settings(values: dict[str, Any]) -> dict[str, Any]:
    """Persist validated runtime overrides and make them immediately effective."""
    _load_runtime_settings()
    clean = {k: values[k] for k in values if k in _RUNTIME_SETTING_KEYS}
    with _RUNTIME_SETTINGS_LOCK:
        for key, value in clean.items():
            # None removes an override so the corresponding environment value
            # becomes effective again. Other values are persisted as-is.
            if value is None:
                _RUNTIME_SETTINGS.pop(key, None)
            else:
                _RUNTIME_SETTINGS[key] = value
        path = _runtime_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(_RUNTIME_SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return {
            key: value
            for key, value in _RUNTIME_SETTINGS.items()
            if key not in _RUNTIME_SECRET_KEYS
        }


def _runtime_value(key: str) -> Any:
    _load_runtime_settings()
    with _RUNTIME_SETTINGS_LOCK:
        return _RUNTIME_SETTINGS.get(key, _UNSET)


def _sync_env_file() -> None:
    """如果 .env 文件存在且被修改，重新同步环境变量到 os.environ。"""
    global _last_env_mtime
    env_path = _BACKEND_DIR / ".env"
    if not env_path.exists():
        return
    try:
        mtime = env_path.stat().st_mtime
        if mtime != _last_env_mtime:
            _last_env_mtime = mtime
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and val:
                    os.environ[key] = val
    except Exception:
        pass


def _env_path(key: str, default: Path) -> Path:
    val = os.getenv(key)
    return Path(val).expanduser() if val else default


def _opt_env_path(key: str) -> Optional[Path]:
    val = os.getenv(key)
    return Path(val).expanduser() if val else None


def _env_list(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """读取逗号分隔的环境变量列表，空值回退到默认列表。"""
    val = os.getenv(key)
    if not val:
        return default
    items = tuple(item.strip() for item in val.split(",") if item.strip())
    return items or default


def _env_json_dict(key: str, default: dict[str, str]) -> dict[str, str]:
    """读取 JSON 字典格式的环境变量，合并到默认字典。非合法 JSON 则使用默认字典。"""
    val = os.getenv(key)
    if not val:
        return default
    try:
        data = json.loads(val)
        if isinstance(data, dict):
            merged = dict(default)
            merged.update({str(k): str(v) for k, v in data.items()})
            return merged
    except Exception:
        pass
    return default


_UNSET = object()

_ALIAS_MAP = {
    "data_dir": "_data_dir",
    "db_path": "_db_path",
    "pipeline_workers_override": "_pipeline_workers_override",
    "download_workers_override": "_download_workers_override",
    "download_concurrent_fragments": "_download_concurrent_fragments",
    "stream_timeout_sec": "_stream_timeout_sec",
    "readiness_ttl_sec": "_readiness_ttl_sec",
    "probe_cache_ttl_sec": "_probe_cache_ttl_sec",
    "cors_allow_origins": "_cors_allow_origins",
    "download_format": "_download_format",
    "download_quality": "_download_quality",
    "download_socket_timeout": "_download_socket_timeout",
    "download_proxy": "_download_proxy",
    "merge_output_format": "_merge_output_format",
    "cookies_file": "_cookies_file",
    "allow_cookies_from_browser": "_allow_cookies_from_browser",
    "startup_probe_enabled": "_startup_probe_enabled",
    "download_retries": "_download_retries",
    "max_upload_mb": "_max_upload_mb",
    "max_video_minutes": "_max_video_minutes",
    "ffmpeg_bin": "_ffmpeg_bin",
    "ffprobe_bin": "_ffprobe_bin",
    "audio_sample_rate": "_audio_sample_rate",
    "audio_channels": "_audio_channels",
    "vocal_separation_enabled": "_vocal_separation_enabled",
    "vocal_separation_backend": "_vocal_separation_backend",
    "vocal_separation_model": "_vocal_separation_model",
    "vocal_separation_threads": "_vocal_separation_threads",
    "vocal_separation_command": "_vocal_separation_command",
    "vocal_separation_timeout": "_vocal_separation_timeout",
    "replicate_whisper_model": "_replicate_whisper_model",
    "replicate_api_token": "_replicate_api_token",
    "replicate_timeout": "_replicate_timeout",
    "replicate_retries": "_replicate_retries",
    "replicate_retry_interval": "_replicate_retry_interval",
    "replicate_poll_interval": "_replicate_poll_interval",
    "transcriber_backend": "_transcriber_backend",
    "transcriber_url": "_transcriber_url",
    "transcriber_api_key": "_transcriber_api_key",
    "transcriber_timeout": "_transcriber_timeout",
    "local_whisper_model": "_local_whisper_model",
    "local_whisper_device": "_local_whisper_device",
    "local_whisper_compute_type": "_local_whisper_compute_type",
    "local_whisper_download_root": "_local_whisper_download_root",
    "local_whisper_beam_size": "_local_whisper_beam_size",
    "deepseek_api_key": "_deepseek_api_key",
    "deepseek_base_url": "_deepseek_base_url",
    "deepseek_model": "_deepseek_model",
    "translate_batch_size": "_translate_batch_size",
    "local_translate_batch_size": "_local_translate_batch_size",
    "translate_timeout": "_translate_timeout",
    "target_languages": "_target_languages",
    "lang_names": "_lang_names",
}


@dataclass(frozen=True, init=False)
class Settings:
    # 后端根目录
    backend_dir: Path = _BACKEND_DIR

    # 包含所有字段的私有 override 参数，允许 dataclasses.replace(settings, ...) 正常覆盖
    _data_dir: Any = field(default=_UNSET, repr=False)
    _db_path: Any = field(default=_UNSET, repr=False)
    _pipeline_workers_override: Optional[int] = field(default=None, repr=False)
    _download_workers_override: Optional[int] = field(default=None, repr=False)
    _download_concurrent_fragments: Any = field(default=_UNSET, repr=False)
    _stream_timeout_sec: Any = field(default=_UNSET, repr=False)
    _readiness_ttl_sec: Any = field(default=_UNSET, repr=False)
    _probe_cache_ttl_sec: Any = field(default=_UNSET, repr=False)
    _cors_allow_origins: Any = field(default=_UNSET, repr=False)
    _download_format: Any = field(default=_UNSET, repr=False)
    _download_quality: Any = field(default=_UNSET, repr=False)
    _download_socket_timeout: Any = field(default=_UNSET, repr=False)
    _download_proxy: Any = field(default=_UNSET, repr=False)
    _merge_output_format: Any = field(default=_UNSET, repr=False)
    _cookies_file: Any = field(default=_UNSET, repr=False)
    _allow_cookies_from_browser: Any = field(default=_UNSET, repr=False)
    _startup_probe_enabled: Any = field(default=_UNSET, repr=False)
    _download_retries: Any = field(default=_UNSET, repr=False)
    _max_upload_mb: Any = field(default=_UNSET, repr=False)
    _max_video_minutes: Any = field(default=_UNSET, repr=False)
    _ffmpeg_bin: Any = field(default=_UNSET, repr=False)
    _ffprobe_bin: Any = field(default=_UNSET, repr=False)
    _audio_sample_rate: Any = field(default=_UNSET, repr=False)
    _audio_channels: Any = field(default=_UNSET, repr=False)
    _vocal_separation_enabled: Any = field(default=_UNSET, repr=False)
    _vocal_separation_backend: Any = field(default=_UNSET, repr=False)
    _vocal_separation_model: Any = field(default=_UNSET, repr=False)
    _vocal_separation_threads: Any = field(default=_UNSET, repr=False)
    _vocal_separation_command: Any = field(default=_UNSET, repr=False)
    _vocal_separation_timeout: Any = field(default=_UNSET, repr=False)
    _replicate_whisper_model: Any = field(default=_UNSET, repr=False)
    _replicate_api_token: Any = field(default=_UNSET, repr=False)
    _replicate_timeout: Any = field(default=_UNSET, repr=False)
    _replicate_retries: Any = field(default=_UNSET, repr=False)
    _replicate_retry_interval: Any = field(default=_UNSET, repr=False)
    _replicate_poll_interval: Any = field(default=_UNSET, repr=False)
    _transcriber_backend: Any = field(default=_UNSET, repr=False)
    _transcriber_url: Any = field(default=_UNSET, repr=False)
    _transcriber_api_key: Any = field(default=_UNSET, repr=False)
    _transcriber_timeout: Any = field(default=_UNSET, repr=False)
    _local_whisper_model: Any = field(default=_UNSET, repr=False)
    _local_whisper_device: Any = field(default=_UNSET, repr=False)
    _local_whisper_compute_type: Any = field(default=_UNSET, repr=False)
    _local_whisper_download_root: Any = field(default=_UNSET, repr=False)
    _local_whisper_beam_size: Any = field(default=_UNSET, repr=False)
    _deepseek_api_key: Any = field(default=_UNSET, repr=False)
    _deepseek_base_url: Any = field(default=_UNSET, repr=False)
    _deepseek_model: Any = field(default=_UNSET, repr=False)
    _translate_batch_size: Any = field(default=_UNSET, repr=False)
    _local_translate_batch_size: Any = field(default=_UNSET, repr=False)
    _translate_timeout: Any = field(default=_UNSET, repr=False)
    _target_languages: Any = field(default=_UNSET, repr=False)
    _lang_names: Any = field(default=_UNSET, repr=False)

    def __init__(
        self,
        backend_dir: Path = _BACKEND_DIR,
        _data_dir: Any = _UNSET,
        _db_path: Any = _UNSET,
        _pipeline_workers_override: Optional[int] = None,
        _download_workers_override: Optional[int] = None,
        _download_concurrent_fragments: Any = _UNSET,
        _stream_timeout_sec: Any = _UNSET,
        _readiness_ttl_sec: Any = _UNSET,
        _probe_cache_ttl_sec: Any = _UNSET,
        _cors_allow_origins: Any = _UNSET,
        _download_format: Any = _UNSET,
        _download_quality: Any = _UNSET,
        _download_socket_timeout: Any = _UNSET,
        _download_proxy: Any = _UNSET,
        _merge_output_format: Any = _UNSET,
        _cookies_file: Any = _UNSET,
        _allow_cookies_from_browser: Any = _UNSET,
        _startup_probe_enabled: Any = _UNSET,
        _download_retries: Any = _UNSET,
        _max_upload_mb: Any = _UNSET,
        _max_video_minutes: Any = _UNSET,
        _ffmpeg_bin: Any = _UNSET,
        _ffprobe_bin: Any = _UNSET,
        _audio_sample_rate: Any = _UNSET,
        _audio_channels: Any = _UNSET,
        _vocal_separation_enabled: Any = _UNSET,
        _vocal_separation_backend: Any = _UNSET,
        _vocal_separation_model: Any = _UNSET,
        _vocal_separation_threads: Any = _UNSET,
        _vocal_separation_command: Any = _UNSET,
        _vocal_separation_timeout: Any = _UNSET,
        _replicate_whisper_model: Any = _UNSET,
        _replicate_api_token: Any = _UNSET,
        _replicate_timeout: Any = _UNSET,
        _replicate_retries: Any = _UNSET,
        _replicate_retry_interval: Any = _UNSET,
        _replicate_poll_interval: Any = _UNSET,
        _transcriber_backend: Any = _UNSET,
        _transcriber_url: Any = _UNSET,
        _transcriber_api_key: Any = _UNSET,
        _transcriber_timeout: Any = _UNSET,
        _local_whisper_model: Any = _UNSET,
        _local_whisper_device: Any = _UNSET,
        _local_whisper_compute_type: Any = _UNSET,
        _local_whisper_download_root: Any = _UNSET,
        _local_whisper_beam_size: Any = _UNSET,
        _deepseek_api_key: Any = _UNSET,
        _deepseek_base_url: Any = _UNSET,
        _deepseek_model: Any = _UNSET,
        _translate_batch_size: Any = _UNSET,
        _local_translate_batch_size: Any = _UNSET,
        _translate_timeout: Any = _UNSET,
        _target_languages: Any = _UNSET,
        _lang_names: Any = _UNSET,
        **kwargs: Any,
    ) -> None:
        object.__setattr__(self, "backend_dir", backend_dir)
        mapped = {
            "_data_dir": _data_dir,
            "_db_path": _db_path,
            "_pipeline_workers_override": _pipeline_workers_override,
            "_download_workers_override": _download_workers_override,
            "_download_concurrent_fragments": _download_concurrent_fragments,
            "_stream_timeout_sec": _stream_timeout_sec,
            "_readiness_ttl_sec": _readiness_ttl_sec,
            "_probe_cache_ttl_sec": _probe_cache_ttl_sec,
            "_cors_allow_origins": _cors_allow_origins,
            "_download_format": _download_format,
            "_download_quality": _download_quality,
            "_download_socket_timeout": _download_socket_timeout,
            "_download_proxy": _download_proxy,
            "_merge_output_format": _merge_output_format,
            "_cookies_file": _cookies_file,
            "_allow_cookies_from_browser": _allow_cookies_from_browser,
            "_startup_probe_enabled": _startup_probe_enabled,
            "_download_retries": _download_retries,
            "_max_upload_mb": _max_upload_mb,
            "_max_video_minutes": _max_video_minutes,
            "_ffmpeg_bin": _ffmpeg_bin,
            "_ffprobe_bin": _ffprobe_bin,
            "_audio_sample_rate": _audio_sample_rate,
            "_audio_channels": _audio_channels,
            "_vocal_separation_enabled": _vocal_separation_enabled,
            "_vocal_separation_backend": _vocal_separation_backend,
            "_vocal_separation_model": _vocal_separation_model,
            "_vocal_separation_threads": _vocal_separation_threads,
            "_vocal_separation_command": _vocal_separation_command,
            "_vocal_separation_timeout": _vocal_separation_timeout,
            "_replicate_whisper_model": _replicate_whisper_model,
            "_replicate_api_token": _replicate_api_token,
            "_replicate_timeout": _replicate_timeout,
            "_replicate_retries": _replicate_retries,
            "_replicate_retry_interval": _replicate_retry_interval,
            "_replicate_poll_interval": _replicate_poll_interval,
            "_transcriber_backend": _transcriber_backend,
            "_transcriber_url": _transcriber_url,
            "_transcriber_api_key": _transcriber_api_key,
            "_transcriber_timeout": _transcriber_timeout,
            "_local_whisper_model": _local_whisper_model,
            "_local_whisper_device": _local_whisper_device,
            "_local_whisper_compute_type": _local_whisper_compute_type,
            "_local_whisper_download_root": _local_whisper_download_root,
            "_local_whisper_beam_size": _local_whisper_beam_size,
            "_deepseek_api_key": _deepseek_api_key,
            "_deepseek_base_url": _deepseek_base_url,
            "_deepseek_model": _deepseek_model,
            "_translate_batch_size": _translate_batch_size,
            "_local_translate_batch_size": _local_translate_batch_size,
            "_translate_timeout": _translate_timeout,
            "_target_languages": _target_languages,
            "_lang_names": _lang_names,
        }
        for k, v in kwargs.items():
            if k in _ALIAS_MAP:
                mapped[_ALIAS_MAP[k]] = v
            elif k in mapped:
                mapped[k] = v

        for field_name, val in mapped.items():
            object.__setattr__(self, field_name, val)

    # 所有任务产物的根目录，按 data/{task_id}/ 组织
    @property
    def data_dir(self) -> Path:
        if self._data_dir is not _UNSET:
            return self._data_dir
        _sync_env_file()
        return _env_path("SUBTRANS_DATA_DIR", self.backend_dir / "data")

    # SQLite 任务库文件
    @property
    def db_path(self) -> Path:
        if self._db_path is not _UNSET:
            return self._db_path
        _sync_env_file()
        return _env_path("SUBTRANS_DB", self.data_dir / "db" / "app.db")

    # API 鉴权 Token（动态读取 SUBTRANS_API_TOKEN 或编辑 .env）
    @property
    def api_token(self) -> Optional[str]:
        _sync_env_file()
        val = (os.getenv("SUBTRANS_API_TOKEN") or "").strip()
        return val if val else None

    # 后台流水线并发数（方案 A：线程池，动态读取，支持运行期调整 SUBTRANS_WORKERS 或编辑 .env）
    @property
    def pipeline_workers(self) -> int:
        if self._pipeline_workers_override is not None:
            return max(1, self._pipeline_workers_override)
        _sync_env_file()
        val = os.getenv("SUBTRANS_WORKERS", "8")
        try:
            return max(1, int(val))
        except (ValueError, TypeError):
            return 8

    # 下载阶段并发数：只限制 yt-dlp 真正下载媒体的阶段。
    # 流水线线程池可以大于这个值，使下载结束后的 SRT/烧录阶段不占下载名额。
    @property
    def download_workers(self) -> int:
        if self._download_workers_override is not None:
            return max(1, self._download_workers_override)
        _sync_env_file()
        val = os.getenv("SUBTRANS_DOWNLOAD_WORKERS", "2")
        try:
            return max(1, int(val))
        except (ValueError, TypeError):
            return 2

    # HLS/DASH 单个媒体任务的分片并发数；yt-dlp 默认值为 1。
    @property
    def download_concurrent_fragments(self) -> int:
        if self._download_concurrent_fragments is not _UNSET:
            return self._download_concurrent_fragments
        _sync_env_file()
        val = os.getenv("SUBTRANS_DL_CONCURRENT_FRAGMENTS", "4")
        try:
            return max(1, int(val))
        except (ValueError, TypeError):
            return 4

    # SSE 进度流连接超时上限（秒），默认 7200 秒（2 小时）
    @property
    def stream_timeout_sec(self) -> int:
        if self._stream_timeout_sec is not _UNSET:
            return self._stream_timeout_sec
        _sync_env_file()
        val = os.getenv("SUBTRANS_STREAM_TIMEOUT_SEC", "7200")
        try:
            return int(val)
        except (ValueError, TypeError):
            return 7200

    # Readiness / Replicate 余额检查 TTL 缓存时长（秒），默认 60 秒
    @property
    def readiness_ttl_sec(self) -> int:
        if self._readiness_ttl_sec is not _UNSET:
            return self._readiness_ttl_sec
        _sync_env_file()
        val = os.getenv("SUBTRANS_READINESS_TTL_SEC", "60")
        try:
            return int(val)
        except (ValueError, TypeError):
            return 60

    # 视频探测 network TTL 缓存时长（秒），默认 300 秒（5 分钟）
    @property
    def probe_cache_ttl_sec(self) -> int:
        if self._probe_cache_ttl_sec is not _UNSET:
            return self._probe_cache_ttl_sec
        _sync_env_file()
        val = os.getenv("SUBTRANS_PROBE_CACHE_TTL_SEC", "300")
        try:
            return int(val)
        except (ValueError, TypeError):
            return 300

    # 允许访问本地 API 的前端来源，逗号分隔覆盖
    @property
    def cors_allow_origins(self) -> tuple[str, ...]:
        if self._cors_allow_origins is not _UNSET:
            return self._cors_allow_origins
        _sync_env_file()
        return _env_list("SUBTRANS_CORS_ORIGINS", _DEFAULT_CORS_ORIGINS)

    # yt-dlp 默认清晰度策略（best / 1080p / 720p / 480p / 360p / audio_only）
    @property
    def download_quality(self) -> str:
        if self._download_quality is not _UNSET:
            return self._download_quality
        _sync_env_file()
        return os.getenv("SUBTRANS_DL_QUALITY", "480p").strip().lower()

    # yt-dlp 单连接网络超时时间（秒），默认 30 秒
    @property
    def download_socket_timeout(self) -> int:
        if self._download_socket_timeout is not _UNSET:
            return self._download_socket_timeout
        _sync_env_file()
        val = os.getenv("SUBTRANS_DL_SOCKET_TIMEOUT", "30")
        try:
            return max(5, int(val))
        except (ValueError, TypeError):
            return 30

    # 可选代理配置（例如 http://127.0.0.1:7890）
    @property
    def download_proxy(self) -> Optional[str]:
        if self._download_proxy is not _UNSET:
            return self._download_proxy
        _sync_env_file()
        val = os.getenv("SUBTRANS_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        return val.strip() if val and val.strip() else None

    # yt-dlp 格式选择：优先读取 SUBTRANS_DL_FORMAT；未显式指定时根据 download_quality 动态映射
    @property
    def download_format(self) -> str:
        if self._download_format is not _UNSET:
            return self._download_format
        _sync_env_file()
        explicit = os.getenv("SUBTRANS_DL_FORMAT")
        if explicit and explicit.strip():
            return explicit.strip()
        quality_map = {
            "best": "bv*+ba/b",
            "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
            "720p": "bv*[height<=720]+ba/b[height<=720]",
            "480p": "bv*[height<=480]+ba/b[height<=480]",
            "360p": "bv*[height<=360]+ba/b[height<=360]",
            "audio_only": "ba/b",
        }
        return quality_map.get(self.download_quality, "bv*[height<=480]+ba/b[height<=480]")

    # 合并后的容器格式
    @property
    def merge_output_format(self) -> str:
        if self._merge_output_format is not _UNSET:
            return self._merge_output_format
        _sync_env_file()
        return os.getenv("SUBTRANS_DL_CONTAINER", "mp4")

    # 部分站点需要 cookies 通过年龄校验 / 登录，可选
    @property
    def cookies_file(self) -> Optional[Path]:
        if self._cookies_file is not _UNSET:
            return self._cookies_file
        _sync_env_file()
        return _opt_env_path("SUBTRANS_COOKIES")

    # 仅允许显式发起的探测从受限浏览器来源读取 Cookie，默认关闭
    @property
    def allow_cookies_from_browser(self) -> bool:
        if self._allow_cookies_from_browser is not _UNSET:
            return bool(self._allow_cookies_from_browser)
        _sync_env_file()
        return (os.getenv("SUBTRANS_ALLOW_COOKIES_FROM_BROWSER", "0") or "0").strip().lower() in {
            "1", "true", "yes", "on"
        }

    # 启动时自动探测固定站点目录，默认开启；测试和受限部署可关闭
    @property
    def startup_probe_enabled(self) -> bool:
        if self._startup_probe_enabled is not _UNSET:
            return bool(self._startup_probe_enabled)
        _sync_env_file()
        return (os.getenv("SUBTRANS_STARTUP_PROBE_ENABLED", "1") or "1").strip().lower() in {
            "1", "true", "yes", "on"
        }

    # 下载失败重试次数
    @property
    def download_retries(self) -> int:
        if self._download_retries is not _UNSET:
            return self._download_retries
        _sync_env_file()
        val = os.getenv("SUBTRANS_DL_RETRIES", "3")
        try:
            return int(val)
        except (ValueError, TypeError):
            return 3

    # 上传限制配置
    @property
    def max_upload_mb(self) -> int:
        if self._max_upload_mb is not _UNSET:
            return self._max_upload_mb
        _sync_env_file()
        val = os.getenv("SUBTRANS_MAX_UPLOAD_MB", "2048")
        try:
            return int(val)
        except (ValueError, TypeError):
            return 2048

    @property
    def max_video_minutes(self) -> int:
        if self._max_video_minutes is not _UNSET:
            return self._max_video_minutes
        _sync_env_file()
        val = os.getenv("SUBTRANS_MAX_VIDEO_MINUTES", "180")
        try:
            return int(val)
        except (ValueError, TypeError):
            return 180

    # ffmpeg / ffprobe 可执行文件（默认走 PATH）
    @property
    def ffmpeg_bin(self) -> str:
        if self._ffmpeg_bin is not _UNSET:
            return self._ffmpeg_bin
        _sync_env_file()
        return os.getenv("SUBTRANS_FFMPEG", "ffmpeg")

    @property
    def ffprobe_bin(self) -> str:
        if self._ffprobe_bin is not _UNSET:
            return self._ffprobe_bin
        _sync_env_file()
        return os.getenv("SUBTRANS_FFPROBE", "ffprobe")

    # 提取音频的采样率与声道：16kHz 单声道是 Whisper 的标准输入
    @property
    def audio_sample_rate(self) -> int:
        if self._audio_sample_rate is not _UNSET:
            return self._audio_sample_rate
        _sync_env_file()
        val = os.getenv("SUBTRANS_AUDIO_SR", "16000")
        try:
            return int(val)
        except (ValueError, TypeError):
            return 16000

    @property
    def audio_channels(self) -> int:
        if self._audio_channels is not _UNSET:
            return self._audio_channels
        _sync_env_file()
        val = os.getenv("SUBTRANS_AUDIO_CH", "1")
        try:
            return int(val)
        except (ValueError, TypeError):
            return 1

    @property
    def vocal_separation_enabled(self) -> bool:
        """是否在识别前抽取人声；默认关闭，保持现有 CPU 流程。"""
        if self._vocal_separation_enabled is not _UNSET:
            return bool(self._vocal_separation_enabled)
        runtime = _runtime_value("vocal_separation_enabled")
        if runtime is not _UNSET:
            return bool(runtime)
        _sync_env_file()
        return (os.getenv("SUBTRANS_VOCAL_SEPARATION", "0") or "0").strip().lower() in {
            "1", "true", "yes", "on"
        }

    @property
    def vocal_separation_backend(self) -> str:
        if self._vocal_separation_backend is not _UNSET:
            return str(self._vocal_separation_backend).strip().lower() or "demucs"
        runtime = _runtime_value("vocal_separation_backend")
        if runtime is not _UNSET:
            return str(runtime).strip().lower() or "demucs"
        _sync_env_file()
        return (os.getenv("SUBTRANS_VOCAL_SEPARATION_BACKEND", "demucs") or "demucs").strip().lower()

    @property
    def vocal_separation_model(self) -> str:
        allowed = {"htdemucs", "mdx_q"}
        if self._vocal_separation_model is not _UNSET:
            value = str(self._vocal_separation_model).strip().lower()
            return value if value in allowed else "htdemucs"
        runtime = _runtime_value("vocal_separation_model")
        if runtime is not _UNSET:
            value = str(runtime).strip().lower()
            return value if value in allowed else "htdemucs"
        _sync_env_file()
        value = (os.getenv("SUBTRANS_VOCAL_SEPARATION_MODEL", "htdemucs") or "htdemucs").strip().lower()
        return value if value in allowed else "htdemucs"

    @property
    def vocal_separation_threads(self) -> int:
        if self._vocal_separation_threads is not _UNSET:
            return max(1, min(8, int(self._vocal_separation_threads)))
        runtime = _runtime_value("vocal_separation_threads")
        if runtime is not _UNSET:
            try:
                return max(1, min(8, int(runtime)))
            except (TypeError, ValueError):
                pass
        _sync_env_file()
        try:
            return max(1, min(8, int(os.getenv("SUBTRANS_VOCAL_SEPARATION_THREADS", "1"))))
        except (TypeError, ValueError):
            return 1

    @property
    def vocal_separation_command(self) -> str:
        if self._vocal_separation_command is not _UNSET:
            return str(self._vocal_separation_command).strip() or "python -m demucs.separate"
        runtime = _runtime_value("vocal_separation_command")
        if runtime is not _UNSET:
            return str(runtime).strip() or "python -m demucs.separate"
        _sync_env_file()
        return os.getenv("SUBTRANS_VOCAL_SEPARATION_COMMAND", "python -m demucs.separate") or "python -m demucs.separate"

    @property
    def vocal_separation_timeout(self) -> int:
        if self._vocal_separation_timeout is not _UNSET:
            return max(60, min(86400, int(self._vocal_separation_timeout)))
        runtime = _runtime_value("vocal_separation_timeout")
        if runtime is not _UNSET:
            try:
                return max(60, min(86400, int(runtime)))
            except (ValueError, TypeError):
                pass
        _sync_env_file()
        try:
            return max(60, min(86400, int(os.getenv("SUBTRANS_VOCAL_SEPARATION_TIMEOUT", "1800"))))
        except (ValueError, TypeError):
            return 1800

    # --- ③ 语音识别（Replicate-hosted Whisper）---
    @property
    def replicate_api_token(self) -> Optional[str]:
        """Replicate Token，运行时页面配置优先于环境变量。"""
        if self._replicate_api_token is not _UNSET:
            value = self._replicate_api_token
            return str(value).strip() or None
        runtime = _runtime_value("replicate_api_token")
        if runtime is not _UNSET:
            return str(runtime).strip() or None
        _sync_env_file()
        value = (os.getenv("REPLICATE_API_TOKEN") or "").strip()
        return value or None

    # Replicate 模型标识（版本锁定）
    @property
    def replicate_whisper_model(self) -> str:
        if self._replicate_whisper_model is not _UNSET:
            return str(self._replicate_whisper_model).strip() or _DEFAULT_REPLICATE_MODEL
        runtime = _runtime_value("replicate_whisper_model")
        if runtime is not _UNSET:
            return str(runtime).strip() or _DEFAULT_REPLICATE_MODEL
        _sync_env_file()
        return os.getenv(
            "SUBTRANS_WHISPER_MODEL",
            _DEFAULT_REPLICATE_MODEL,
        )

    # Replicate 单次 HTTP 请求超时；prediction 的排队/运行通过短轮询跟踪
    @property
    def replicate_timeout(self) -> int:
        if self._replicate_timeout is not _UNSET:
            return int(self._replicate_timeout)
        runtime = _runtime_value("replicate_timeout")
        if runtime is not _UNSET:
            try:
                return int(runtime)
            except (ValueError, TypeError):
                pass
        _sync_env_file()
        val = os.getenv("SUBTRANS_REPLICATE_TIMEOUT", "1800")
        try:
            return int(val)
        except (ValueError, TypeError):
            return 1800

    # Replicate 创建/状态查询发生网络错误时的总尝试次数
    @property
    def replicate_retries(self) -> int:
        if self._replicate_retries is not _UNSET:
            return int(self._replicate_retries)
        runtime = _runtime_value("replicate_retries")
        if runtime is not _UNSET:
            try:
                return int(runtime)
            except (ValueError, TypeError):
                pass
        _sync_env_file()
        val = os.getenv("SUBTRANS_REPLICATE_RETRIES", "3")
        try:
            return int(val)
        except (ValueError, TypeError):
            return 3

    # 网络错误后再次请求的间隔；默认 1 小时，避免重复创建长时间排队的任务
    @property
    def replicate_retry_interval(self) -> float:
        if self._replicate_retry_interval is not _UNSET:
            return float(self._replicate_retry_interval)
        runtime = _runtime_value("replicate_retry_interval")
        if runtime is not _UNSET:
            try:
                return float(runtime)
            except (ValueError, TypeError):
                pass
        _sync_env_file()
        val = os.getenv("SUBTRANS_REPLICATE_RETRY_INTERVAL", "3600")
        try:
            return float(val)
        except (ValueError, TypeError):
            return 3600.0

    # 已取得 prediction ID 后的状态轮询间隔；轮询不会创建新任务
    @property
    def replicate_poll_interval(self) -> float:
        if self._replicate_poll_interval is not _UNSET:
            return float(self._replicate_poll_interval)
        runtime = _runtime_value("replicate_poll_interval")
        if runtime is not _UNSET:
            try:
                return float(runtime)
            except (ValueError, TypeError):
                pass
        _sync_env_file()
        val = os.getenv("SUBTRANS_REPLICATE_POLL_INTERVAL", "30")
        try:
            return float(val)
        except (ValueError, TypeError):
            return 30.0

    # --- ③ 语音识别服务选择 ---
    @property
    def transcriber_backend(self) -> str:
        """识别服务标识：local_whisper（默认）或 replicate/http。"""
        if self._transcriber_backend is not _UNSET:
            return str(self._transcriber_backend).strip().lower() or "local_whisper"
        _sync_env_file()
        return (os.getenv("SUBTRANS_TRANSCRIBER_BACKEND", "local_whisper") or "local_whisper").strip().lower()

    @property
    def transcriber_url(self) -> Optional[str]:
        """自定义 HTTP 识别服务的 POST 地址。"""
        if self._transcriber_url is not _UNSET:
            value = self._transcriber_url
        else:
            _sync_env_file()
            value = os.getenv("SUBTRANS_TRANSCRIBER_URL")
        value = (value or "").strip()
        return value or None

    @property
    def transcriber_api_key(self) -> Optional[str]:
        """自定义 HTTP 识别服务的 Bearer token。"""
        if self._transcriber_api_key is not _UNSET:
            value = self._transcriber_api_key
        else:
            _sync_env_file()
            value = os.getenv("SUBTRANS_TRANSCRIBER_API_KEY")
        value = (value or "").strip()
        return value or None

    @property
    def transcriber_timeout(self) -> int:
        if self._transcriber_timeout is not _UNSET:
            return int(self._transcriber_timeout)
        _sync_env_file()
        value = os.getenv("SUBTRANS_TRANSCRIBER_TIMEOUT", "1800")
        try:
            return int(value)
        except (ValueError, TypeError):
            return 1800

    @property
    def local_whisper_model(self) -> str:
        if self._local_whisper_model is not _UNSET:
            return str(self._local_whisper_model)
        _sync_env_file()
        return os.getenv("SUBTRANS_LOCAL_WHISPER_MODEL", "tiny") or "tiny"

    @property
    def local_whisper_device(self) -> str:
        if self._local_whisper_device is not _UNSET:
            return str(self._local_whisper_device)
        _sync_env_file()
        return os.getenv("SUBTRANS_LOCAL_WHISPER_DEVICE", "cpu") or "cpu"

    @property
    def local_whisper_compute_type(self) -> str:
        if self._local_whisper_compute_type is not _UNSET:
            return str(self._local_whisper_compute_type)
        _sync_env_file()
        return os.getenv("SUBTRANS_LOCAL_WHISPER_COMPUTE_TYPE", "int8") or "int8"

    @property
    def local_whisper_download_root(self) -> Optional[str]:
        if self._local_whisper_download_root is not _UNSET:
            value = self._local_whisper_download_root
        else:
            _sync_env_file()
            value = os.getenv("SUBTRANS_LOCAL_WHISPER_DOWNLOAD_ROOT")
        value = str(value or "").strip()
        return value or None

    @property
    def local_whisper_beam_size(self) -> int:
        if self._local_whisper_beam_size is not _UNSET:
            return int(self._local_whisper_beam_size)
        _sync_env_file()
        try:
            return max(1, int(os.getenv("SUBTRANS_LOCAL_WHISPER_BEAM_SIZE", "5")))
        except (ValueError, TypeError):
            return 5

    # --- ④ 翻译（旧版 DeepSeek 兼容配置；新配置位于 SQLite）---
    @property
    def deepseek_api_key(self) -> Optional[str]:
        if self._deepseek_api_key is not _UNSET:
            return self._deepseek_api_key
        _sync_env_file()
        val = os.getenv("SUBTRANS_DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
        return val if val else None

    @property
    def deepseek_base_url(self) -> str:
        if self._deepseek_base_url is not _UNSET:
            return self._deepseek_base_url
        _sync_env_file()
        return os.getenv("SUBTRANS_DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    @property
    def deepseek_model(self) -> str:
        if self._deepseek_model is not _UNSET:
            return self._deepseek_model
        _sync_env_file()
        return os.getenv("SUBTRANS_DEEPSEEK_MODEL", "deepseek-chat")

    # 每批翻译多少条字幕（太长模型可能截断 JSON，自动减半重试）
    @property
    def translate_batch_size(self) -> int:
        if self._translate_batch_size is not _UNSET:
            return self._translate_batch_size
        _sync_env_file()
        val = os.getenv("SUBTRANS_TRANSLATE_BATCH", "8")
        try:
            return int(val)
        except (ValueError, TypeError):
            return 8

    # 每批本地 CPU 翻译多少条字幕；独立于云端批量，默认 16。
    @property
    def local_translate_batch_size(self) -> int:
        if self._local_translate_batch_size is not _UNSET:
            try:
                return max(1, int(self._local_translate_batch_size))
            except (ValueError, TypeError):
                return 16
        _sync_env_file()
        val = os.getenv("SUBTRANS_LOCAL_TRANSLATE_BATCH", "16")
        try:
            return max(1, int(val))
        except (ValueError, TypeError):
            return 16

    @property
    def translate_timeout(self) -> int:
        if self._translate_timeout is not _UNSET:
            return self._translate_timeout
        _sync_env_file()
        val = os.getenv("SUBTRANS_TRANSLATE_TIMEOUT", "60")
        try:
            return int(val)
        except (ValueError, TypeError):
            return 60

    # 支持的翻译目标语言列表（逗号分隔）
    @property
    def target_languages(self) -> tuple[str, ...]:
        if self._target_languages is not _UNSET:
            return tuple(code for code in self._target_languages if code not in DISABLED_LANGUAGES)
        _sync_env_file()
        configured = _env_list(
            "SUBTRANS_TARGET_LANGUAGES",
            (
                "zh-CN", "zh-TW", "en", "ja", "ko", "es", "fr", "de", "ru", "it",
                "pt", "vi", "th", "ar", "id", "hi", "nl", "pl", "tr", "sv",
                "uk", "cs", "da", "fi", "el", "he", "hu", "no", "ro", "sk",
                "af", "ca", "bg", "hr", "ms", "fa", "ta", "sw",
            ),
        )
        return tuple(code for code in configured if code not in DISABLED_LANGUAGES)

    # 语言代码到名称的映射字典，可由 SUBTRANS_LANG_NAMES 环境变量（JSON 字符串）覆盖/追加
    @property
    def lang_names(self) -> dict[str, str]:
        if self._lang_names is not _UNSET:
            return self._lang_names
        _sync_env_file()
        return _env_json_dict("SUBTRANS_LANG_NAMES", DEFAULT_LANG_NAMES)


settings = Settings()
