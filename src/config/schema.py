"""Shared configuration document defaults and validation schema."""
from __future__ import annotations

from typing import Any

BUSINESS_DEFAULTS: dict[str, Any] = {
    "pipeline_workers": 8,
    "download_workers": 2,
    "download_concurrent_fragments": 4,
    "stream_timeout_sec": 7200,
    "readiness_ttl_sec": 60,
    "probe_cache_ttl_sec": 300,
    "cors_allow_origins": ["http://localhost:5273", "http://127.0.0.1:5273", "http://localhost:8000", "http://127.0.0.1:8000"],
    "download_format": None,
    "download_quality": "480p",
    "download_socket_timeout": 30,
    "merge_output_format": "mp4",
    "allow_cookies_from_browser": False,
    "debug": False,
    "startup_probe_enabled": True,
    "download_retries": 3,
    "max_upload_mb": 2048,
    "max_video_minutes": 180,
    "ffmpeg_bin": "ffmpeg",
    "ffprobe_bin": "ffprobe",
    "audio_sample_rate": 16000,
    "audio_channels": 1,
    "vocal_separation_enabled": False,
    "vocal_separation_backend": "demucs",
    "vocal_separation_model": "htdemucs",
    "vocal_separation_threads": 1,
    "vocal_separation_timeout": 1800,
    "vocal_separation_command": "python -m demucs.separate",
    "transcriber_backend": "local_whisper",
    "transcriber_url": None,
    "transcriber_timeout": 1800,
    "transcriber_api_key": None,
    "api_token": None,
    "cookies_file": None,
    "download_proxy": None,
    "local_whisper_model": "tiny",
    "local_whisper_device": "cpu",
    "local_whisper_compute_type": "int8",
    "local_whisper_beam_size": 5,
    "local_whisper_download_root": None,
    "translate_batch_size": 8,
    "local_translate_batch_size": 16,
    "translate_timeout": 60,
    "drive_api_base_url": "http://127.0.0.1:8787",
    "drive_timeout": 600,
    "drive_transfer_timeout": 7200,
    "local_ct2_inter_threads": 1,
    "local_ct2_intra_threads": 4,
    "retention_days": 30,
    "lang_names": {
        "auto": "the source language", "zh-CN": "Simplified Chinese (简体中文)",
        "zh-TW": "Traditional Chinese (繁體中文)", "zh": "Chinese (中文)",
        "en": "English", "ja": "Japanese (日本語)", "ko": "Korean (한국어)",
        "es": "Spanish (Español)", "fr": "French (Français)",
        "de": "German (Deutsch)", "ru": "Russian (Русский)",
        "it": "Italian (Italiano)", "pt": "Portuguese (Português)",
        "vi": "Vietnamese (Tiếng Việt)", "th": "Thai (ไทย)",
        "ar": "Arabic (العربية)", "id": "Indonesian (Bahasa Indonesia)",
        "hi": "Hindi (हिन्दी)", "nl": "Dutch (Nederlands)",
        "pl": "Polish (Polski)", "tr": "Turkish (Türkçe)",
        "sv": "Swedish (Svenska)", "uk": "Ukrainian (Українська)",
        "cs": "Czech (Čeština)", "da": "Danish (Dansk)",
        "fi": "Finnish (Suomi)", "el": "Greek (Ελληνικά)",
        "he": "Hebrew (עברית)", "hu": "Hungarian (Magyar)",
        "no": "Norwegian (Norsk)", "ro": "Romanian (Română)",
        "sk": "Slovak (Slovenčina)", "af": "Afrikaans",
        "ca": "Catalan (Català)", "bg": "Bulgarian (Български)",
        "hr": "Croatian (Hrvatski)", "ms": "Malay (Bahasa Melayu)",
        "fa": "Persian (فارسی)", "ur": "Urdu (اردو)",
        "bn": "Bengali (বাংলা)", "ta": "Tamil (தமிழ்)",
        "sw": "Swahili (Kiswahili)",
    },
    "target_languages": ["zh-CN", "zh-TW", "en", "ja", "ko", "es", "fr", "de", "ru", "it", "pt", "vi", "th", "ar", "id", "hi", "nl", "pl", "tr", "sv", "uk", "cs", "da", "fi", "el", "he", "hu", "no", "ro", "sk", "af", "ca", "bg", "hr", "ms", "fa", "ta", "sw"],
}

SECRET_KEYS = frozenset({"api_token", "transcriber_api_key", "cookies_file", "download_proxy"})

BUSINESS_MINIMUMS = {
    "pipeline_workers": 1, "download_workers": 1,
    "download_concurrent_fragments": 1, "stream_timeout_sec": 1,
    "readiness_ttl_sec": 1, "probe_cache_ttl_sec": 0,
    "download_socket_timeout": 5, "download_retries": 0,
    "max_upload_mb": 1, "max_video_minutes": 1,
    "audio_sample_rate": 1, "transcriber_timeout": 1,
    "local_whisper_beam_size": 1, "translate_batch_size": 1,
    "local_translate_batch_size": 1, "translate_timeout": 1,
    "drive_timeout": 1, "drive_transfer_timeout": 1,
    "local_ct2_inter_threads": 1, "local_ct2_intra_threads": 1,
}

BUSINESS_ENUMS = {
    "download_quality": {"best", "1080p", "720p", "480p", "360p", "audio_only"},
    "vocal_separation_backend": {"demucs"},
    "vocal_separation_model": {"htdemucs", "mdx_q"},
    "transcriber_backend": {"local_whisper", "http"},
}

DISABLED_TARGET_LANGUAGES = frozenset({"bn", "ur"})
QUALITY_FORMATS = {
    "best": "bv*+ba/b",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
    "720p": "bv*[height<=720]+ba/b[height<=720]",
    "480p": "bv*[height<=480]+ba/b[height<=480]",
    "360p": "bv*[height<=360]+ba/b[height<=360]",
    "audio_only": "ba/b",
}


def effective_business_values(values: dict[str, Any]) -> dict[str, Any]:
    effective = dict(BUSINESS_DEFAULTS)
    effective.update({key: value for key, value in values.items() if key in BUSINESS_DEFAULTS})
    lang_names = dict(BUSINESS_DEFAULTS["lang_names"])
    lang_names.update(values.get("lang_names", {}))
    effective["lang_names"] = lang_names
    effective["target_languages"] = [
        code for code in effective["target_languages"]
        if code not in DISABLED_TARGET_LANGUAGES
    ]
    if not effective.get("download_format"):
        effective["download_format"] = QUALITY_FORMATS.get(
            effective["download_quality"], QUALITY_FORMATS["480p"]
        )
    return effective


def validate_business_value(key: str, value: Any, *, allow_reset: bool = False) -> None:
    """Validate one setting consistently for API patches and Redis documents."""
    if key not in BUSINESS_DEFAULTS:
        raise ValueError(f"unsupported business setting: {key}")
    default = BUSINESS_DEFAULTS[key]
    if value is None:
        if allow_reset or key in SECRET_KEYS or default is None or key == "retention_days":
            return
        raise ValueError(f"business setting may not be null: {key}")
    if key in SECRET_KEYS or default is None:
        if not isinstance(value, str):
            raise ValueError(f"invalid business setting type: {key}")
        return
    if isinstance(default, bool):
        valid = isinstance(value, bool)
    elif isinstance(default, int):
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif isinstance(default, list):
        valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
    elif isinstance(default, dict):
        valid = isinstance(value, dict) and all(
            isinstance(item_key, str) and isinstance(item_value, str)
            for item_key, item_value in value.items()
        )
    else:
        valid = isinstance(value, str)
    if not valid:
        raise ValueError(f"invalid business setting type: {key}")
    if key in BUSINESS_MINIMUMS and value < BUSINESS_MINIMUMS[key]:
        raise ValueError(f"business setting is below minimum: {key}")
    if key == "vocal_separation_threads" and not 1 <= value <= 8:
        raise ValueError(f"business setting is outside allowed range: {key}")
    if key == "vocal_separation_timeout" and not 60 <= value <= 86400:
        raise ValueError(f"business setting is outside allowed range: {key}")
    if key == "audio_channels" and value not in {1, 2}:
        raise ValueError("audio_channels must be 1 or 2")
    if key == "retention_days" and value < 0:
        raise ValueError("retention_days must be nonnegative or null")
    allowed = BUSINESS_ENUMS.get(key)
    if allowed is not None and value not in allowed:
        raise ValueError(f"unsupported value for business setting: {key}")


def default_document() -> dict[str, Any]:
    import copy

    return {
        "business": copy.deepcopy(BUSINESS_DEFAULTS),
        "models": {},
        "engines": {"byId": {}},
        "meta": {"legacy_sources_imported": []},
    }
