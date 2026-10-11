"""TOML-backed application configuration loading and validation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


CONFIG_VERSION = 1
_MISSING = object()
_STRING_MAP = object()


class ConfigError(ValueError):
    """Raised when an application TOML configuration is invalid."""


# The schema describes the supported shape and types. Defaults intentionally live
# in config/default.toml; this module only owns the contract and validation.
_SCHEMA: dict[str, Any] = {
    "config_version": int,
    "paths": {
        "data_dir": str,
        "db_path": str,
    },
    "pipeline": {
        "workers": int,
    },
    "service": {
        "stream_timeout_sec": int,
        "readiness_ttl_sec": int,
        "probe_cache_ttl_sec": int,
        "cors_allow_origins": (list, str),
        "max_upload_mb": int,
        "max_video_minutes": int,
    },
    "download": {
        "workers": int,
        "concurrent_fragments": int,
        "quality": str,
        "format": str,
        "socket_timeout": int,
        "proxy": str,
        "container": str,
        "cookies_file": str,
        "allow_cookies_from_browser": bool,
        "startup_probe_enabled": bool,
        "retries": int,
    },
    "tools": {
        "ffmpeg": str,
        "ffprobe": str,
    },
    "audio": {
        "sample_rate": int,
        "channels": int,
    },
    "vocal_separation": {
        "enabled": bool,
        "backend": str,
        "model": str,
        "threads": int,
        "command": str,
        "timeout": int,
    },
    "transcriber": {
        "backend": str,
        "url": str,
        "timeout": int,
    },
    "local_whisper": {
        "model": str,
        "device": str,
        "compute_type": str,
        "download_root": str,
        "beam_size": int,
    },
    "replicate": {
        "model": str,
        "timeout": int,
        "retries": int,
        "retry_interval": (int, float),
        "poll_interval": (int, float),
    },
    "translation": {
        "base_url": str,
        "model": str,
        "batch_size": int,
        "timeout": int,
        "target_languages": (list, str),
        "lang_names": _STRING_MAP,
    },
}


def _format_path(parts: tuple[str, ...]) -> str:
    return ".".join(parts)


def _is_type(value: Any, expected: Any) -> bool:
    if isinstance(expected, tuple) and expected and expected[0] is list:
        return isinstance(value, list) and all(isinstance(item, expected[1]) for item in value)
    if expected is _STRING_MAP:
        return isinstance(value, dict) and all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.items()
        )
    # bool is an int subclass, but TOML booleans must not satisfy integer keys.
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(expected, tuple):
        return isinstance(value, expected) and not isinstance(value, bool)
    return isinstance(value, expected)


def _validate_mapping(
    mapping: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    path: tuple[str, ...] = (),
    partial: bool,
) -> None:
    for key, value in mapping.items():
        if key not in schema:
            raise ConfigError(f"未知配置键: {_format_path(path + (key,))}")
        expected = schema[key]
        if isinstance(expected, dict):
            if not isinstance(value, dict):
                raise ConfigError(
                    f"配置键 {_format_path(path + (key,))} 必须是 TOML section"
                )
            if expected:
                _validate_mapping(value, expected, path=path + (key,), partial=partial)
            continue
        if not _is_type(value, expected):
            if expected is _STRING_MAP:
                expected_name = "字符串到字符串的映射"
            elif isinstance(expected, tuple) and expected and expected[0] is list:
                expected_name = f"字符串列表"
            else:
                expected_name = getattr(expected, "__name__", str(expected))
            raise ConfigError(
                f"配置键 {_format_path(path + (key,))} 类型错误，期望 {expected_name}"
            )

    if partial:
        return
    for key, expected in schema.items():
        if key not in mapping:
            raise ConfigError(f"缺少配置键: {_format_path(path + (key,))}")
        if isinstance(expected, dict):
            _validate_mapping(mapping[key], expected, path=path + (key,), partial=False)


def _get(mapping: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = mapping
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _require_number(mapping: Mapping[str, Any], path: tuple[str, ...], *, minimum: float | None = None, maximum: float | None = None) -> None:
    value = _get(mapping, path)
    if value is _MISSING or isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    if minimum is not None and value < minimum:
        raise ConfigError(f"配置键 {_format_path(path)} 必须大于等于 {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"配置键 {_format_path(path)} 必须小于等于 {maximum}")


def _validate_semantics(mapping: Mapping[str, Any]) -> None:
    for path, minimum, maximum in (
        (("pipeline", "workers"), 1, None),
        (("service", "stream_timeout_sec"), 0, None),
        (("service", "readiness_ttl_sec"), 0, None),
        (("service", "probe_cache_ttl_sec"), 0, None),
        (("service", "max_upload_mb"), 1, None),
        (("service", "max_video_minutes"), 1, None),
        (("download", "workers"), 1, None),
        (("download", "concurrent_fragments"), 1, None),
        (("download", "socket_timeout"), 5, None),
        (("download", "retries"), 0, None),
        (("audio", "sample_rate"), 1, None),
        (("audio", "channels"), 1, None),
        (("vocal_separation", "threads"), 1, 8),
        (("vocal_separation", "timeout"), 60, 86400),
        (("local_whisper", "beam_size"), 1, None),
        (("replicate", "timeout"), 1, None),
        (("replicate", "retries"), 0, None),
        (("replicate", "retry_interval"), 0, None),
        (("replicate", "poll_interval"), 0, None),
        (("transcriber", "timeout"), 1, None),
        (("translation", "batch_size"), 1, None),
        (("translation", "timeout"), 1, None),
    ):
        _require_number(mapping, path, minimum=minimum, maximum=maximum)

    version = _get(mapping, ("config_version",))
    if version != CONFIG_VERSION:
        raise ConfigError(
            f"不支持的 config_version: {version!r}，当前支持版本为 {CONFIG_VERSION}"
        )


def _resolve_path(value: str, backend_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else backend_dir / path


def _read_file(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise ConfigError(f"缺少主配置文件: {path}")
        return {}
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置文件 {path}: {exc}") from exc
    if not isinstance(data, dict):  # pragma: no cover - tomllib always returns dict
        raise ConfigError(f"配置文件根节点必须是 TOML table: {path}")
    return data


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


@dataclass(frozen=True)
class ConfigSnapshot:
    """Validated, startup-time application configuration."""

    values: Mapping[str, Any]
    backend_dir: Path
    default_path: Path
    override_path: Path | None
    explicit_override: bool
    version: int = CONFIG_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(deepcopy(dict(self.values))))

    def get(self, *path: str, default: Any = _MISSING) -> Any:
        value = _get(self.values, path)
        if value is _MISSING:
            if default is not _MISSING:
                return default
            raise KeyError(".".join(path))
        return value

    def source_info(self) -> dict[str, Any]:
        return {
            "config_version": self.version,
            "default_file": str(self.default_path),
            "override_file": str(self.override_path) if self.override_path else None,
            "override_present": self.override_path is not None,
            "override_explicit": self.explicit_override,
        }


def load_config(backend_dir: Path, *, config_file: str | Path | None = None) -> ConfigSnapshot:
    """Load and strictly validate the main service TOML configuration.

    ``config_file`` is intended for tests and explicit callers. For normal
    startup, ``SUBTRANS_CONFIG_FILE`` is the only non-secret environment entry
    point; absent that variable, an optional root ``config.local.toml`` is used.
    """
    backend_dir = Path(backend_dir).expanduser().resolve()
    default_path = backend_dir / "config" / "default.toml"
    default_data = _read_file(default_path, required=True)
    _validate_mapping(default_data, _SCHEMA, partial=False)
    _validate_semantics(default_data)

    explicit = config_file is not None or bool(os.getenv("SUBTRANS_CONFIG_FILE", "").strip())
    raw_override = config_file if config_file is not None else os.getenv("SUBTRANS_CONFIG_FILE")
    if raw_override:
        override_path = _resolve_path(str(raw_override), backend_dir)
        override_data = _read_file(override_path, required=True)
    elif explicit:
        override_path = None
        override_data = {}
    else:
        local_path = backend_dir / "config.local.toml"
        override_path = local_path if local_path.is_file() else None
        override_data = _read_file(local_path, required=False)

    if override_data:
        _validate_mapping(override_data, _SCHEMA, partial=True)
    merged = _deep_merge(default_data, override_data)
    _validate_mapping(merged, _SCHEMA, partial=False)
    _validate_semantics(merged)
    return ConfigSnapshot(
        values=merged,
        backend_dir=backend_dir,
        default_path=default_path,
        override_path=override_path,
        explicit_override=explicit,
    )


__all__ = ["CONFIG_VERSION", "ConfigError", "ConfigSnapshot", "load_config"]
