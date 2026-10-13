"""Redis-backed shared settings and machine-local path configuration."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .bootstrap import load_bootstrap
from .config_repository import read_config_snapshot, get_config_provider
from .schema import (
    BUSINESS_DEFAULTS,
    SECRET_KEYS,
    effective_business_values,
    validate_business_value,
)

_SETTINGS_SCOPE: ContextVar[dict[str, Any] | None] = ContextVar("settings_scope", default=None)

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
def get_runtime_settings() -> dict[str, Any]:
    return settings.capture_business_snapshot()


def update_runtime_settings(values: dict[str, Any]) -> dict[str, Any]:
    clean = _validate_business_patch(values)
    def apply(document: dict[str, Any]) -> dict[str, Any]:
        business = document["business"]
        for key, value in clean.items():
            if value is None:
                business.pop(key, None)
            else:
                business[key] = value
        return document
    snapshot = get_config_provider().mutate(apply)
    return settings.capture_business_snapshot(snapshot)


def _validate_business_patch(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("business settings patch must be an object")
    clean: dict[str, Any] = {}
    for key, value in values.items():
        validate_business_value(key, value, allow_reset=True)
        clean[key] = value
    return clean


@contextmanager
def settings_scope(values: dict[str, Any]):
    """Apply one business snapshot to the current task context."""
    if not isinstance(values, dict):
        raise TypeError("settings_scope expects a business settings object")
    token = _SETTINGS_SCOPE.set(dict(values))
    try:
        yield
    finally:
        _SETTINGS_SCOPE.reset(token)


_UNSET = object()

_ALIAS_MAP = {
    "data_dir": "_data_dir",
    "db_path": "_db_path",
    "pipeline_workers": "_pipeline_workers_override",
    "download_workers": "_download_workers_override",
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
    "debug": "_debug",
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
    _debug: Any = field(default=_UNSET, repr=False)
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
        _debug: Any = _UNSET,
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
            "_debug": _debug,
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

    def __getattribute__(self, name: str) -> Any:
        if name in BUSINESS_DEFAULTS:
            try:
                private = _ALIAS_MAP.get(name, "_" + name)
                explicit = object.__getattribute__(self, private)
                if explicit is not _UNSET and not (name in {"pipeline_workers", "download_workers"} and explicit is None):
                    return explicit
            except AttributeError:
                pass
            scoped = _SETTINGS_SCOPE.get()
            if scoped is not None and name in scoped:
                effective = effective_business_values(scoped)
                value = effective[name]
                return tuple(value) if name == "target_languages" else value
            snapshot = read_config_snapshot()
            business = effective_business_values(snapshot.values["business"])
            if name in SECRET_KEYS:
                return business.get(name)
            value = business[name]
            return tuple(value) if name == "target_languages" else value
        return object.__getattribute__(self, name)

    def capture_business_snapshot(self, config_snapshot=None) -> dict[str, Any]:
        snapshot = config_snapshot or read_config_snapshot()
        business = effective_business_values(snapshot.values["business"])
        return {key: value for key, value in business.items() if key not in SECRET_KEYS}

    # 所有任务产物的根目录，按 data/{task_id}/ 组织
    @property
    def data_dir(self) -> Path:
        if self._data_dir is not _UNSET:
            return self._data_dir
        configured = Path(load_bootstrap()["paths"]["dataDir"]).expanduser()
        return configured if configured.is_absolute() else self.backend_dir / configured

    # SQLite 任务库文件
    @property
    def db_path(self) -> Path:
        if self._db_path is not _UNSET:
            return self._db_path
        configured = Path(load_bootstrap()["paths"]["dbPath"]).expanduser()
        return configured if configured.is_absolute() else self.backend_dir / configured



settings = Settings()
