#!/usr/bin/env python3
"""Migrate legacy application environment settings into config.local.toml.

This script intentionally has no import from ``src.config`` so it can run while
an old .env still contains the variables that the new loader rejects.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable


SECRET_KEYS = {
    "SUBTRANS_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "REPLICATE_API_TOKEN",
    "SUBTRANS_TRANSCRIBER_API_KEY",
    "SUBTRANS_API_TOKEN",
}


def _identity(value: str) -> Any:
    return value.strip()


def _int(value: str) -> int:
    return int(value.strip())


def _float(value: str) -> float:
    return float(value.strip())


def _bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"布尔值无效: {value!r}")


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _json_dict(value: str) -> dict[str, str]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in parsed.items()
    ):
        raise ValueError("必须是字符串到字符串的 JSON 对象")
    return parsed


# (TOML path, converter). The mapping is deliberately explicit so new settings
# cannot silently enter the migration surface.
MAPPING: dict[str, tuple[tuple[str, ...], Callable[[str], Any]]] = {
    "SUBTRANS_DATA_DIR": (("paths", "data_dir"), _identity),
    "SUBTRANS_DB": (("paths", "db_path"), _identity),
    "SUBTRANS_WORKERS": (("pipeline", "workers"), _int),
    "SUBTRANS_DOWNLOAD_WORKERS": (("download", "workers"), _int),
    "SUBTRANS_DL_CONCURRENT_FRAGMENTS": (("download", "concurrent_fragments"), _int),
    "SUBTRANS_DL_QUALITY": (("download", "quality"), _identity),
    "SUBTRANS_DL_FORMAT": (("download", "format"), _identity),
    "SUBTRANS_DL_SOCKET_TIMEOUT": (("download", "socket_timeout"), _int),
    "SUBTRANS_PROXY": (("download", "proxy"), _identity),
    "SUBTRANS_DL_CONTAINER": (("download", "container"), _identity),
    "SUBTRANS_COOKIES": (("download", "cookies_file"), _identity),
    "SUBTRANS_ALLOW_COOKIES_FROM_BROWSER": (("download", "allow_cookies_from_browser"), _bool),
    "SUBTRANS_STARTUP_PROBE_ENABLED": (("download", "startup_probe_enabled"), _bool),
    "SUBTRANS_DL_RETRIES": (("download", "retries"), _int),
    "SUBTRANS_FFMPEG": (("tools", "ffmpeg"), _identity),
    "SUBTRANS_FFPROBE": (("tools", "ffprobe"), _identity),
    "SUBTRANS_AUDIO_SR": (("audio", "sample_rate"), _int),
    "SUBTRANS_AUDIO_CH": (("audio", "channels"), _int),
    "SUBTRANS_VOCAL_SEPARATION": (("vocal_separation", "enabled"), _bool),
    "SUBTRANS_VOCAL_SEPARATION_BACKEND": (("vocal_separation", "backend"), _identity),
    "SUBTRANS_VOCAL_SEPARATION_MODEL": (("vocal_separation", "model"), _identity),
    "SUBTRANS_VOCAL_SEPARATION_THREADS": (("vocal_separation", "threads"), _int),
    "SUBTRANS_VOCAL_SEPARATION_COMMAND": (("vocal_separation", "command"), _identity),
    "SUBTRANS_VOCAL_SEPARATION_TIMEOUT": (("vocal_separation", "timeout"), _int),
    "SUBTRANS_TRANSCRIBER_BACKEND": (("transcriber", "backend"), _identity),
    "SUBTRANS_TRANSCRIBER_URL": (("transcriber", "url"), _identity),
    "SUBTRANS_TRANSCRIBER_TIMEOUT": (("transcriber", "timeout"), _int),
    "SUBTRANS_LOCAL_WHISPER_MODEL": (("local_whisper", "model"), _identity),
    "SUBTRANS_LOCAL_WHISPER_DEVICE": (("local_whisper", "device"), _identity),
    "SUBTRANS_LOCAL_WHISPER_COMPUTE_TYPE": (("local_whisper", "compute_type"), _identity),
    "SUBTRANS_LOCAL_WHISPER_DOWNLOAD_ROOT": (("local_whisper", "download_root"), _identity),
    "SUBTRANS_LOCAL_WHISPER_BEAM_SIZE": (("local_whisper", "beam_size"), _int),
    "SUBTRANS_WHISPER_MODEL": (("replicate", "model"), _identity),
    "SUBTRANS_REPLICATE_TIMEOUT": (("replicate", "timeout"), _int),
    "SUBTRANS_REPLICATE_RETRIES": (("replicate", "retries"), _int),
    "SUBTRANS_REPLICATE_RETRY_INTERVAL": (("replicate", "retry_interval"), _float),
    "SUBTRANS_REPLICATE_POLL_INTERVAL": (("replicate", "poll_interval"), _float),
    "SUBTRANS_STREAM_TIMEOUT_SEC": (("service", "stream_timeout_sec"), _int),
    "SUBTRANS_READINESS_TTL_SEC": (("service", "readiness_ttl_sec"), _int),
    "SUBTRANS_PROBE_CACHE_TTL_SEC": (("service", "probe_cache_ttl_sec"), _int),
    "SUBTRANS_CORS_ORIGINS": (("service", "cors_allow_origins"), _list),
    "SUBTRANS_MAX_UPLOAD_MB": (("service", "max_upload_mb"), _int),
    "SUBTRANS_MAX_VIDEO_MINUTES": (("service", "max_video_minutes"), _int),
    "SUBTRANS_DEEPSEEK_BASE_URL": (("translation", "base_url"), _identity),
    "SUBTRANS_DEEPSEEK_MODEL": (("translation", "model"), _identity),
    "SUBTRANS_TRANSLATE_BATCH": (("translation", "batch_size"), _int),
    "SUBTRANS_TRANSLATE_TIMEOUT": (("translation", "timeout"), _int),
    "SUBTRANS_TARGET_LANGUAGES": (("translation", "target_languages"), _list),
    "SUBTRANS_LANG_NAMES": (("translation", "lang_names"), _json_dict),
}


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{line_number} 缺少 '='")
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _put(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = target
    for part in path[:-1]:
        current = current.setdefault(part, {})
    current[path[-1]] = value


def _toml_key(key: str) -> str:
    if key.isidentifier() and key.isascii():
        return key
    return json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"无法写入 TOML 值: {value!r}")


def _render_table(table: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[str]:
    lines: list[str] = []
    scalars = [(key, value) for key, value in table.items() if not isinstance(value, dict)]
    tables = [(key, value) for key, value in table.items() if isinstance(value, dict)]
    for key, value in scalars:
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, value in tables:
        if lines and lines[-1] != "":
            lines.append("")
        section = prefix + (key,)
        lines.append("[" + ".".join(_toml_key(part) for part in section) + "]")
        lines.extend(_render_table(value, section))
    return lines


def _render_config(values: dict[str, Any]) -> str:
    document = {"config_version": 1, **values}
    return "# Generated by scripts/migrate_env.py; review before starting the service.\n\n" + "\n".join(_render_table(document)) + "\n"


def migrate(env_path: Path, output_path: Path, *, write: bool, force: bool) -> int:
    values = _parse_env(env_path)
    migrated: dict[str, Any] = {}
    skipped_secrets: list[str] = []
    unsupported: list[str] = []
    failures: list[str] = []

    for key, raw_value in values.items():
        if not raw_value:
            continue
        if key in SECRET_KEYS:
            skipped_secrets.append(key)
            continue
        entry = MAPPING.get(key)
        if entry is None:
            if key.startswith("SUBTRANS_"):
                unsupported.append(key)
            continue
        path, converter = entry
        try:
            converted = converter(raw_value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{key}: {exc}")
            continue
        _put(migrated, path, converted)
        print(f"{key} -> {'.'.join(path)}")

    for key in skipped_secrets:
        if key == "DEEPSEEK_API_KEY":
            print(f"{key}: legacy secret; rename it to SUBTRANS_DEEPSEEK_API_KEY manually", file=sys.stderr)
        else:
            print(f"{key}: secret skipped; keep it in .env or inject it externally")
    for key in unsupported:
        print(f"{key}: no longer supported; migrate manually or remove it", file=sys.stderr)
    for failure in failures:
        print(f"转换失败: {failure}", file=sys.stderr)

    if unsupported or failures:
        return 1
    if not migrated:
        print("没有发现可迁移的非秘密配置。")
        return 0

    rendered = _render_config(migrated)
    print("\n生成内容预览：")
    print(rendered, end="")
    if not write:
        print("\n当前为 dry-run；加 --write 才会写入配置文件。")
        return 0
    if output_path.exists() and not force:
        print(f"拒绝覆盖已有文件：{output_path}（需要 --force）", file=sys.stderr)
        return 1
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(output_path)
    output_path.chmod(0o600)
    print(f"已写入 {output_path}")
    return 0


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="将旧环境变量迁移到 config.local.toml")
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    parser.add_argument("--output", type=Path, default=root / "config.local.toml")
    parser.add_argument("--write", action="store_true", help="确认写入输出文件；默认只预览")
    parser.add_argument("--force", action="store_true", help="允许覆盖已有输出文件")
    args = parser.parse_args()
    env_path = args.env_file.expanduser()
    output_path = args.output.expanduser()
    if not env_path.is_file():
        print(f"找不到环境文件：{env_path}", file=sys.stderr)
        return 1
    try:
        return migrate(env_path, output_path, write=args.write, force=args.force)
    except (OSError, ValueError) as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
