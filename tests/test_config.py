"""配置层并发与动态环境变量测试。"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from src.config import config


def test_database_defaults_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("SUBTRANS_DB", raising=False)
    monkeypatch.delenv("SUBTRANS_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings(backend_dir=tmp_path)
    assert settings.db_path == tmp_path / "data" / "db" / "app.db"

    custom_data_dir = tmp_path / "custom-data"
    monkeypatch.setenv("SUBTRANS_DATA_DIR", str(custom_data_dir))
    assert settings.db_path == custom_data_dir / "db" / "app.db"

    custom_db = tmp_path / "elsewhere" / "app.db"
    monkeypatch.setenv("SUBTRANS_DB", str(custom_db))
    assert settings.db_path == custom_db


def test_browser_cookie_probe_setting_defaults_off_and_reads_boolean_env(monkeypatch):
    monkeypatch.delenv("SUBTRANS_ALLOW_COOKIES_FROM_BROWSER", raising=False)
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)
    settings = config.Settings()
    assert settings.allow_cookies_from_browser is False

    monkeypatch.setenv("SUBTRANS_ALLOW_COOKIES_FROM_BROWSER", "yes")
    assert settings.allow_cookies_from_browser is True
    replaced = dataclasses.replace(settings, _allow_cookies_from_browser=False)
    assert replaced.allow_cookies_from_browser is False


def test_startup_probe_setting_defaults_on_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv("SUBTRANS_STARTUP_PROBE_ENABLED", raising=False)
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)
    settings = config.Settings()
    assert settings.startup_probe_enabled is True

    monkeypatch.setenv("SUBTRANS_STARTUP_PROBE_ENABLED", "0")
    assert settings.startup_probe_enabled is False
    replaced = dataclasses.replace(settings, _startup_probe_enabled=True)
    assert replaced.startup_probe_enabled is True


def test_download_format_defaults_to_480p_cap():
    assert (
        config.Settings().download_format
        == "bv*[height<=480]+ba/b[height<=480]"
    )


def test_concurrency_defaults(monkeypatch):
    monkeypatch.delenv("SUBTRANS_WORKERS", raising=False)
    monkeypatch.delenv("SUBTRANS_DOWNLOAD_WORKERS", raising=False)
    monkeypatch.delenv("SUBTRANS_DL_CONCURRENT_FRAGMENTS", raising=False)
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings()

    assert settings.pipeline_workers == 8
    assert settings.download_workers == 2
    assert settings.download_concurrent_fragments == 4


def test_concurrency_values_are_read_dynamically(monkeypatch):
    monkeypatch.setenv("SUBTRANS_WORKERS", "5")
    monkeypatch.setenv("SUBTRANS_DOWNLOAD_WORKERS", "3")
    monkeypatch.setenv("SUBTRANS_DL_CONCURRENT_FRAGMENTS", "6")
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings()

    assert settings.pipeline_workers == 5
    assert settings.download_workers == 3
    assert settings.download_concurrent_fragments == 6


def test_dynamic_settings_read_from_env(monkeypatch):
    """验证翻译、目标语言、限制、ffmpeg 等配置项能从环境变量动态读取。"""
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_API_KEY", "sk-dynamic-key")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_BASE_URL", "https://proxy.example.com")
    monkeypatch.setenv("SUBTRANS_DEEPSEEK_MODEL", "deepseek-coder")
    monkeypatch.setenv("SUBTRANS_TRANSLATE_BATCH", "16")
    monkeypatch.setenv("SUBTRANS_TRANSLATE_TIMEOUT", "120")
    monkeypatch.setenv("SUBTRANS_TARGET_LANGUAGES", "zh-CN,en,bn,th,ur,vi,ar")
    monkeypatch.setenv("SUBTRANS_MAX_UPLOAD_MB", "4096")
    monkeypatch.setenv("SUBTRANS_MAX_VIDEO_MINUTES", "300")
    monkeypatch.setenv("SUBTRANS_AUDIO_SR", "24000")
    monkeypatch.setenv("SUBTRANS_AUDIO_CH", "2")
    monkeypatch.setenv("SUBTRANS_DL_RETRIES", "5")
    monkeypatch.setenv("SUBTRANS_DL_CONTAINER", "mkv")
    monkeypatch.setenv("SUBTRANS_FFMPEG", "/custom/ffmpeg")
    monkeypatch.setenv("SUBTRANS_FFPROBE", "/custom/ffprobe")
    monkeypatch.setenv("SUBTRANS_STREAM_TIMEOUT_SEC", "3600")
    monkeypatch.setenv("SUBTRANS_READINESS_TTL_SEC", "120")
    monkeypatch.setenv("SUBTRANS_PROBE_CACHE_TTL_SEC", "600")
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings()

    assert settings.deepseek_api_key == "sk-dynamic-key"
    assert settings.deepseek_base_url == "https://proxy.example.com"
    assert settings.deepseek_model == "deepseek-coder"
    assert settings.translate_batch_size == 16
    assert settings.translate_timeout == 120
    assert settings.target_languages == ("zh-CN", "en", "th", "vi", "ar")
    assert settings.max_upload_mb == 4096
    assert settings.max_video_minutes == 300
    assert settings.audio_sample_rate == 24000
    assert settings.audio_channels == 2
    assert settings.download_retries == 5
    assert settings.merge_output_format == "mkv"
    assert settings.ffmpeg_bin == "/custom/ffmpeg"
    assert settings.ffprobe_bin == "/custom/ffprobe"
    assert settings.stream_timeout_sec == 3600
    assert settings.readiness_ttl_sec == 120
    assert settings.probe_cache_ttl_sec == 600


def test_vocal_separation_settings_read_from_env(monkeypatch):
    """人声分离配置应按请求动态读取，并规范化 backend / command。"""
    monkeypatch.setenv("SUBTRANS_VOCAL_SEPARATION", "yes")
    monkeypatch.setenv("SUBTRANS_VOCAL_SEPARATION_BACKEND", " DEMUCS ")
    monkeypatch.setenv("SUBTRANS_VOCAL_SEPARATION_COMMAND", "python -m demucs.separate")
    monkeypatch.setenv("SUBTRANS_VOCAL_SEPARATION_TIMEOUT", "45")
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings()

    assert settings.vocal_separation_enabled is True
    assert settings.vocal_separation_backend == "demucs"
    assert settings.vocal_separation_command == "python -m demucs.separate"
    # 分离超时不能低于安全下限，避免误配置成几秒导致批任务反复失败。
    assert settings.vocal_separation_timeout == 60


def test_vocal_separation_settings_have_safe_fallbacks(monkeypatch):
    monkeypatch.setenv("SUBTRANS_VOCAL_SEPARATION", "off")
    monkeypatch.setenv("SUBTRANS_VOCAL_SEPARATION_TIMEOUT", "not-a-number")
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings()

    assert settings.vocal_separation_enabled is False
    assert settings.vocal_separation_backend == "demucs"
    assert settings.vocal_separation_command == "python -m demucs.separate"
    assert settings.vocal_separation_timeout == 1800


def test_transcriber_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("SUBTRANS_TRANSCRIBER_BACKEND", "http")
    monkeypatch.setenv("SUBTRANS_TRANSCRIBER_URL", "https://stt.example.test/transcribe")
    monkeypatch.setenv("SUBTRANS_TRANSCRIBER_API_KEY", "custom-key")
    monkeypatch.setenv("SUBTRANS_TRANSCRIBER_TIMEOUT", "90")
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings()

    assert settings.transcriber_backend == "http"
    assert settings.transcriber_url == "https://stt.example.test/transcribe"
    assert settings.transcriber_api_key == "custom-key"
    assert settings.transcriber_timeout == 90


def test_transcriber_defaults_use_local_tiny(monkeypatch):
    monkeypatch.delenv("SUBTRANS_TRANSCRIBER_BACKEND", raising=False)
    monkeypatch.delenv("SUBTRANS_LOCAL_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings()

    assert settings.transcriber_backend == "local_whisper"
    assert settings.local_whisper_model == "tiny"


def test_local_whisper_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("SUBTRANS_LOCAL_WHISPER_MODEL", "tiny")
    monkeypatch.setenv("SUBTRANS_LOCAL_WHISPER_DEVICE", "cuda")
    monkeypatch.setenv("SUBTRANS_LOCAL_WHISPER_COMPUTE_TYPE", "float16")
    monkeypatch.setenv("SUBTRANS_LOCAL_WHISPER_DOWNLOAD_ROOT", "/models")
    monkeypatch.setenv("SUBTRANS_LOCAL_WHISPER_BEAM_SIZE", "3")
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings()

    assert settings.local_whisper_model == "tiny"
    assert settings.local_whisper_device == "cuda"
    assert settings.local_whisper_compute_type == "float16"
    assert settings.local_whisper_download_root == "/models"
    assert settings.local_whisper_beam_size == 3


def test_dataclasses_replace_compatibility(monkeypatch):
    """验证 dataclasses.replace 对所有字段仍然成立（如单元测试使用）。"""
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings()
    replaced = dataclasses.replace(
        settings,
        _deepseek_api_key="sk-override-key",
        _max_upload_mb=1024,
        _target_languages=("zh-CN", "ja"),
        _data_dir=Path("/custom/data"),
        _vocal_separation_enabled=True,
        _vocal_separation_timeout=900,
    )

    assert replaced.deepseek_api_key == "sk-override-key"
    assert replaced.max_upload_mb == 1024
    assert replaced.target_languages == ("zh-CN", "ja")
    assert replaced.data_dir == Path("/custom/data")
    assert replaced.vocal_separation_enabled is True
    assert replaced.vocal_separation_timeout == 900

    # 原始 settings 实例不受影响
    assert settings.max_upload_mb == 2048


def test_local_translation_batch_is_independent_and_defaults_to_sixteen(monkeypatch):
    monkeypatch.setenv("SUBTRANS_TRANSLATE_BATCH", "8")
    monkeypatch.setenv("SUBTRANS_LOCAL_TRANSLATE_BATCH", "16")
    monkeypatch.setattr(config, "_sync_env_file", lambda: None)

    settings = config.Settings()

    assert settings.translate_batch_size == 8
    assert settings.local_translate_batch_size == 16

    monkeypatch.setenv("SUBTRANS_LOCAL_TRANSLATE_BATCH", "32")
    assert config.Settings().local_translate_batch_size == 32
