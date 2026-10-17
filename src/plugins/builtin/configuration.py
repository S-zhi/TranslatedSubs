"""Configuration and lightweight dependency checks owned by the built-in plugins."""

from __future__ import annotations

import importlib.util
import shutil
from collections.abc import Mapping
from typing import Any

from src.plugin_system.configuration import validate_fields
from src.plugin_system.errors import PluginError


def _integer(label: str, minimum: int = 1, maximum: int = 86400, default: int | None = None):
    result = {"type": "integer", "label": label, "minimum": minimum, "maximum": maximum}
    if default is not None:
        result["default"] = default
    return result


def _string(label: str, *, nullable: bool = False, **extra):
    return {"type": "string", "label": label, "nullable": nullable, **extra}


_DOWNLOAD = {
    "quality": _string("画质"), "format_selector": _string("下载格式"),
    "proxy": _string("代理地址", nullable=True, format="url"),
    "socket_timeout": _integer("连接超时（秒）", default=30),
    "cookies_ref": _string("Cookie 引用", nullable=True),
    "merge_output_format": _string("封装格式"),
    "retries": _integer("重试次数", 0, 100, 5),
    "concurrent_fragments": _integer("分片并发", 1, 32, 4),
}
_AUDIO = {"sample_rate": _integer("采样率", 8000, 192000), "channels": _integer("声道", 1, 2)}
_SEPARATE = {
    "backend": _string("分离后端", enum=["demucs"]), "model": _string("模型"),
    "threads": _integer("线程", 1, 8, 1), "command": _string("启动命令"),
    "timeout_seconds": _integer("超时（秒）", 60, 86400, 1800),
}
_TRANSCRIBE = {
    "backend": _string("转写后端", enum=["local_whisper", "local", "whisper", "faster_whisper", "http", "custom", "custom_http"]),
    "model": _string("模型"), "service_url": _string("转写服务地址", nullable=True, format="url"),
    "credential_ref": _string("凭据引用", nullable=True),
    "timeout_seconds": _integer("超时（秒）", default=1800),
    "device": _string("设备", enum=["cpu"]), "compute_type": _string("精度", enum=["int8"]),
    "download_root": _string("模型目录", nullable=True), "beam_size": _integer("Beam size", 1, 20, 5),
}
_TRANSLATE = {
    "engine_id": _string("引擎"),
    "api_type": _string("接口类型", enum=["local_ct2", "openai_compatible", "anthropic_compatible"]),
    "base_url": _string("服务地址", format="url"), "model": _string("模型"),
    "credential_ref": _string("凭据引用", nullable=True),
    "batch_size": _integer("翻译批量", 1, 200, 20),
    "local_batch_size": _integer("本地翻译批量", 1, 200, 8),
    "timeout_seconds": _integer("超时（秒）", default=60),
}
_TTS = {
    "voice": _string("音色"), "target_lang": _string("目标语言"),
    "bilingual": {"type": "boolean", "label": "双语字幕"},
    "speed": {"type": "number", "label": "语速", "minimum": 0.1, "maximum": 4, "default": 1.0},
    "model_id": _string("Kokoro 模型", nullable=True, enum=[None, "kokoro-v1", "hexgrad/Kokoro-82M-v1.1-zh"]),
    "output_format": _string("音频格式", enum=["wav"]),
}

BUILTIN_CONFIGS = {
    "builtin.ytdlp": ("yt-dlp 下载", "下载视频并探测媒体信息", _DOWNLOAD,
                      ("proxy", "socket_timeout", "retries", "concurrent_fragments"), ()),
    "builtin.ffmpeg_audio": ("FFmpeg 音频提取", "为转写和配音提取所需音轨", _AUDIO, (), ()),
    "builtin.vocal_demucs": ("Demucs 人声分离", "分离人声与背景音轨", _SEPARATE,
                            ("model", "threads", "timeout_seconds"), ()),
    "builtin.whisper": ("Whisper 转写", "本地或 HTTP 服务转写字幕", _TRANSCRIBE,
                        ("backend", "service_url", "timeout_seconds", "beam_size"), ("whisper",)),
    "builtin.translation_engine": ("翻译引擎", "使用已配置的引擎或模型卡翻译字幕", _TRANSLATE,
                                   ("batch_size", "local_batch_size", "timeout_seconds"), ("engine", "llm")),
    "builtin.kokoro": ("Kokoro 配音", "按字幕时间线合成中文或英文配音", _TTS,
                       ("speed", "model_id"), ("tts",)),
    "builtin.ffmpeg_render": ("FFmpeg 字幕与配音封装", "生成硬字幕、软字幕及配音视频", {}, (), ()),
}


class BuiltinFactory:
    """Metadata and preflight never construct a session or load model weights."""

    config_schema_version = 1

    def describe(self) -> dict[str, Any]:
        name, description, fields, editable, bindings = BUILTIN_CONFIGS[self.manifest.plugin_id]
        return {
            "name": name, "description": description,
            "config_schema": {key: dict(fields[key]) for key in editable},
            "bindings": list(bindings), "config_schema_version": self.config_schema_version,
        }

    def validate_config(self, config: Mapping[str, Any], *, purpose: str | None = None, partial: bool = True):
        _, _, fields, editable, _ = BUILTIN_CONFIGS[self.manifest.plugin_id]
        if purpose is None:
            fields = {key: fields[key] for key in editable}
        elif self.stage == "render":
            fields = {"mode": _string("字幕模式", enum=["hard", "soft"])} if purpose == "render.subtitle" else {
                "original_voice_mode": _string("原声音轨", enum=["keep", "lower", "replace"])
            }
        return validate_fields(config, fields, partial=partial)

    def preflight(self, config, *, operation, purpose, source_lang="", target_lang=""):
        self.validate_config(config, purpose=purpose)
        if self.stage in {"download", "audio_extract", "render"}:
            from src.config import settings
            self._binary(settings.ffmpeg_bin, "FFmpeg")
            self._binary(settings.ffprobe_bin, "FFprobe")
        if self.stage == "download":
            self._module("yt_dlp", "yt-dlp")
        elif self.stage == "transcribe":
            backend = str(config.get("backend") or "local_whisper").lower()
            if backend in {"http", "custom", "custom_http"}:
                if not config.get("service_url"):
                    self._fail("invalid_input", "HTTP 转写未配置服务地址")
                self._module("httpx", "httpx")
            else:
                if config.get("service_url") is not None or config.get("credential_ref") is not None:
                    self._fail("invalid_input", "本地 Whisper 不能配置 HTTP 服务地址或凭据")
                self._module("faster_whisper", "faster-whisper")
                from src.service.model_manager import model_manager
                name = str(config.get("model") or "").removeprefix("local:")
                if not model_manager.is_ready(name):
                    self._fail("dependency_missing", "本地 Whisper 模型未就绪")
        elif self.stage == "translate":
            if config.get("api_type") == "local_ct2":
                if source_lang != "en" or target_lang.lower() not in {"zh", "zh-cn"}:
                    self._fail("invalid_input", "本地翻译仅支持英文到中文")
                from src.service.translation_model_manager import get_translation_model_manager
                manager = get_translation_model_manager()
                if not manager.is_ready() or not manager.dependency_status().get("ready"):
                    self._fail("dependency_missing", "本地翻译模型或依赖未就绪")
            else:
                self._module("httpx", "httpx")
        elif self.stage == "tts":
            language = str(config.get("target_lang") or target_lang).lower()
            voice = config.get("voice", "auto")
            if not language.startswith(("zh", "en")):
                self._fail("invalid_input", "Kokoro 配音目前支持中文和英文目标语")
            if voice not in {"auto", "zf_001", "zm_010", "af_maple", "af_sol"}:
                self._fail("invalid_input", "不支持的 Kokoro 音色")
            if voice in {"zf_001", "zm_010"} and not language.startswith("zh") or voice in {"af_maple", "af_sol"} and not language.startswith("en"):
                self._fail("invalid_input", "Kokoro 音色与目标语言不匹配")
            self._module("kokoro", "Kokoro；uv sync --extra tts")
        elif self.stage == "vocal_separate":
            self._module("demucs", "Demucs")
        if self.stage == "render" and operation == "subtitle" and config.get("mode") == "hard":
            from src.config import settings
            from src.core.ffmpeg_utils import has_subtitles_filter
            if not has_subtitles_filter(settings.ffmpeg_bin):
                self._fail("dependency_missing", "FFmpeg 缺少硬字幕所需的 libass subtitles 滤镜")

    def _module(self, module: str, label: str) -> None:
        try:
            available = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError, ModuleNotFoundError):
            available = False
        if not available:
            self._fail("dependency_missing", f"缺少运行依赖：{label}")

    def _binary(self, executable: str, label: str) -> None:
        if not shutil.which(executable):
            self._fail("dependency_missing", f"找不到 {label} 可执行文件")

    def _fail(self, code: str, message: str) -> None:
        raise PluginError(code, self.stage, self.manifest.plugin_id, message)
