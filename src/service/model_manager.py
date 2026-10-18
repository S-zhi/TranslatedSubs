"""管理官方模型的显式下载，以及用户提供的本地 Whisper 模型。"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import threading
from contextlib import contextmanager, nullcontext
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from src.config import settings

MODEL_CATALOG: tuple[dict[str, Any], ...] = (
    {"name": "tiny", "label": "Whisper Tiny", "size": "~75 MB"},
    {"name": "tiny.en", "label": "Whisper Tiny · English", "size": "~75 MB"},
    {"name": "base", "label": "Whisper Base", "size": "~145 MB"},
    {"name": "base.en", "label": "Whisper Base · English", "size": "~145 MB"},
    {"name": "small", "label": "Whisper Small", "size": "~465 MB"},
    {"name": "small.en", "label": "Whisper Small · English", "size": "~465 MB"},
    {"name": "medium", "label": "Whisper Medium", "size": "~1.5 GB"},
    {"name": "medium.en", "label": "Whisper Medium · English", "size": "~1.5 GB"},
    {"name": "large-v1", "label": "Whisper Large V1", "size": "~3 GB"},
    {"name": "large-v2", "label": "Whisper Large V2", "size": "~3 GB"},
    {"name": "large-v3", "label": "Whisper Large V3", "size": "~3 GB"},
    {"name": "large-v3-turbo", "label": "Whisper Large V3 Turbo", "size": "~1.6 GB"},
)
MODEL_NAMES = frozenset(item["name"] for item in MODEL_CATALOG)
# Verified against upstream commit metadata (2026-10-17), without fetching weights:
# https://huggingface.co/Systran/faster-whisper-base.en/commit/3d3d5dee26484f91867d81cb899cfcf72b96be6c
OFFICIAL_MODEL_REVISIONS = {
    "base.en": "3d3d5dee26484f91867d81cb899cfcf72b96be6c",
}
_OFFICIAL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
_DOWNLOAD_MANIFEST = ".subtrans-download.json"
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
_WEIGHT = re.compile(r"^(pytorch_model(?:-\d{5}-of-\d{5})?\.bin|model(?:-\d{5}-of-\d{5})?\.safetensors)$")
_SIDECARS = frozenset({
    "config.json", "generation_config.json", "preprocessor_config.json",
    "tokenizer_config.json", "tokenizer.json", "vocab.json", "merges.txt",
    "normalizer.json", "special_tokens_map.json", "added_tokens.json",
    "pytorch_model.bin.index.json", "model.safetensors.index.json",
})
_HF_MARKER = ".subtrans-hf.json"


class ModelValidationError(ValueError):
    """上传文件或模型行为不符合本地 Whisper 协议。"""


class ModelDependencyError(RuntimeError):
    """检查环境缺少可选的 Hugging Face 推理依赖。"""


def _model_endpoint(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None
    try:
        url = urlsplit(endpoint)
        valid = (
            url.scheme == "https" and url.hostname and (url.port is None or url.port > 0)
            and url.username is None and url.password is None and not url.query
            and not url.fragment and url.path in ("", "/")
            and not any(c.isspace() or ord(c) < 32 for c in endpoint)
            and "\\" not in endpoint
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ModelValidationError("模型源必须是无凭据、路径或查询参数的 HTTPS origin")
    return endpoint.rstrip("/")


def _regular_size(path: Path) -> int | None:
    try:
        info = path.lstat()
        return info.st_size if stat.S_ISREG(info.st_mode) else None
    except FileNotFoundError:
        return None


def _download_manifest(path: Path) -> dict[str, Any]:
    marker = path / _DOWNLOAD_MANIFEST
    if path.is_symlink() or _regular_size(marker) is None:
        return {}
    try:
        if marker.stat().st_size > 16384:
            return {}
        value = json.loads(marker.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _file_stamps(path: Path, names) -> dict[str, list[int]]:
    """Cheap offline change detection after verified publication, no weight reads."""
    result = {}
    for name in names:
        if Path(name).name != name:
            raise ValueError("Invalid model manifest filename")
        info = (path / name).lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Model payload must be a regular file")
        result[name] = [info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    return result


def _stamps_match(path: Path, stamps) -> bool:
    try:
        return bool(stamps) and isinstance(stamps, dict) and _file_stamps(path, stamps) == stamps
    except (OSError, TypeError, ValueError):
        return False


def _publish_model(stage: Path, destination: Path) -> None:
    """Publish a verified directory; retain desktop predecessors for live readers.

    Failed publication rolls back. A failed rollback deliberately keeps the
    uniquely named backup outside staging so cleanup cannot destroy it.
    """
    from src.desktop.models import model_install_guard

    backup_root = None
    with model_install_guard() if destination.exists() else nullcontext():
        if destination.is_symlink():
            raise ModelValidationError("模型目录不能是符号链接")
        if destination.exists():
            backup_root = Path(tempfile.mkdtemp(prefix=f".previous-{destination.name}-", dir=destination.parent))
            destination.rename(backup_root / "model")
        try:
            stage.rename(destination)
        except Exception:
            if backup_root is not None:
                (backup_root / "model").rename(destination)
                backup_root.rmdir()
            raise
        if backup_root is not None and os.environ.get("SUBTRANS_DESKTOP") != "1":
            shutil.rmtree(backup_root)


def _manifest_ready(path: Path, manifest: dict[str, Any]) -> bool:
    sizes = manifest.get("files")
    return (
        manifest.get("validated") == "cpu/int8"
        and isinstance(manifest.get("revision"), str)
        and re.fullmatch(r"[0-9a-f]{40}", manifest["revision"]) is not None
        and isinstance(sizes, dict) and set(sizes) == set(_OFFICIAL_FILES)
        and all(type(size) is int and size > 0 and _regular_size(path / name) == size
                for name, size in sizes.items())
        and ("stamps" not in manifest and os.environ.get("SUBTRANS_DESKTOP") != "1"
             or (isinstance(manifest.get("stamps"), dict)
                 and set(manifest["stamps"]) == set(_OFFICIAL_FILES)
                 and _stamps_match(path, manifest.get("stamps"))))
    )


def _downloaded_bytes(stage: Path) -> int:
    """Bounded local-dir accounting: payloads plus HF partials, never cache metadata."""
    if stage.is_symlink() or not stage.is_dir():
        return 0
    sizes = {name: _regular_size(stage / name) for name in _OFFICIAL_FILES}
    # Xet can reconstruct chunks out of order, so an incomplete file's logical
    # length is not evidence of bytes received. In that mode count only files
    # atomically completed by the Hub; never claim sparse/preallocated bytes.
    try:
        from huggingface_hub.utils._runtime import is_xet_available
        if is_xet_available():
            return sum(size or 0 for size in sizes.values())
    except ImportError:
        return sum(size or 0 for size in sizes.values())
    # Hub 0.36 hashes the metadata filename; older versions used the filename.
    prefixes = {
        name: (name + ".", base64.urlsafe_b64encode(
            hashlib.sha1(f"{name}.metadata".encode()).digest()
        ).decode() + ".") for name in _OFFICIAL_FILES
    }
    cache = stage
    for part in (".cache", "huggingface", "download"):
        cache /= part
        if cache.is_symlink() or not cache.is_dir():
            return sum(size or 0 for size in sizes.values())
    # Four payloads produce at most a few entries each; never recursively scan a cache.
    with os.scandir(cache) as entries:
        for index, entry in enumerate(entries):
            if index >= 64:
                break
            if not entry.name.endswith(".incomplete") or not entry.is_file(follow_symlinks=False):
                continue
            for name in _OFFICIAL_FILES:
                if sizes[name] is None and entry.name.startswith(prefixes[name]):
                    try:
                        sizes[name] = entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        # Hub atomically renamed this partial since scandir.
                        sizes[name] = _regular_size(stage / name)
                    break
    return sum(size or 0 for size in sizes.values())


def _remove_download_stage(stage: Path) -> None:
    if stage.is_symlink():
        stage.unlink()
    elif stage.exists():
        # rmtree does not descend into directory symlinks.
        shutil.rmtree(stage)


@contextmanager
def _desktop_stage(root: Path, name: str):
    """Only reclaim this model's desktop stage, under a cross-process lock."""
    import fcntl

    if any(parent.is_symlink() for parent in (root, *root.parents)):
        raise ModelValidationError("模型存储路径不能包含符号链接")
    lock_path = root / f".download-{name}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ModelValidationError("模型下载锁无效")
        fcntl.flock(fd, fcntl.LOCK_EX)
        stage = root / f".download-{name}-desktop"
        _remove_download_stage(stage)
        stage.mkdir(mode=0o700)
        try:
            yield stage
        finally:
            _remove_download_stage(stage)
    finally:
        os.close(fd)


def _verify_official_files(stage: Path, entries: dict[str, Any]) -> dict[str, int]:
    sizes = {}
    for name in _OFFICIAL_FILES:
        path = stage / name
        size = _regular_size(path)
        if not size:
            raise ModelValidationError(f"官方模型文件不完整: {name}")
        metadata = entries[name]
        expected = getattr(metadata, "size", None)
        if expected is not None and size != expected:
            raise ModelValidationError(f"官方模型文件大小不匹配: {name}")
        lfs = getattr(metadata, "lfs", None)
        digest = (lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None))
        blob = getattr(metadata, "blob_id", None)
        if digest or blob:
            hasher = hashlib.sha256() if digest else hashlib.sha1()
            if not digest:
                hasher.update(f"blob {size}\0".encode())
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            if hasher.hexdigest() != (digest or blob):
                raise ModelValidationError(f"官方模型文件校验失败: {name}")
        sizes[name] = size
    for name in ("config.json", "tokenizer.json"):
        with (stage / name).open(encoding="utf-8") as handle:
            if not isinstance(json.load(handle), dict):
                raise ModelValidationError(f"官方模型 JSON 无效: {name}")
    return sizes


def _safe_name(name: str) -> str:
    if not _NAME.fullmatch(name) or name.startswith(".") or name in MODEL_NAMES:
        raise ModelValidationError("模型名称只能由字母、数字、点、下划线和连字符组成，且不能与内置模型重名")
    return name


def _safe_filename(filename: str) -> str:
    if not filename or filename != Path(filename).name or "\\" in filename or filename.startswith("."):
        raise ModelValidationError("模型文件必须位于目录根部，且不能包含路径")
    if not (_WEIGHT.fullmatch(filename) or filename in _SIDECARS):
        raise ModelValidationError(f"不支持的模型文件: {filename}")
    return filename


def _weight_files(path: Path) -> list[Path]:
    weights = sorted(p for p in path.iterdir() if p.is_file() and _WEIGHT.fullmatch(p.name))
    if not weights:
        raise ModelValidationError("缺少 .bin 或 .safetensors 权重")
    suffixes = {p.suffix for p in weights}
    if len(suffixes) != 1:
        raise ModelValidationError("不能混用 .bin 和 .safetensors 权重")
    index_name = "pytorch_model.bin.index.json" if ".bin" in suffixes else "model.safetensors.index.json"
    index = path / index_name
    if len(weights) > 1 or any("-of-" in p.name for p in weights):
        if not index.is_file():
            raise ModelValidationError("分片权重缺少索引文件")
        try:
            mapping = json.loads(index.read_text(encoding="utf-8"))["weight_map"]
            declared = set(mapping.values())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ModelValidationError("权重索引格式无效") from exc
        if declared != {p.name for p in weights}:
            raise ModelValidationError("权重分片与索引不一致")
    elif index.exists():
        raise ModelValidationError("单文件权重不应带分片索引")
    return weights


def validate_hf_model(path: Path) -> None:
    """离线验证官方 Whisper 文件、权重和 ASR 输入输出契约。"""
    if path.is_symlink() or not path.is_dir():
        raise ModelValidationError("模型目录无效")
    for entry in path.iterdir():
        if entry.name == _HF_MARKER and entry.is_file() and not entry.is_symlink():
            continue
        if entry.is_symlink() or not entry.is_file() or (entry.name not in _SIDECARS and not _WEIGHT.fullmatch(entry.name)):
            raise ModelValidationError(f"模型目录包含不支持的文件: {entry.name}")
        if entry.stat().st_size == 0:
            raise ModelValidationError(f"模型文件为空: {entry.name}")
    try:
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelValidationError("缺少有效的 config.json") from exc
    if not isinstance(config, dict) or config.get("model_type") != "whisper":
        raise ModelValidationError("只支持 Hugging Face Whisper 语音识别模型")
    if not (path / "preprocessor_config.json").is_file():
        raise ModelValidationError("缺少 preprocessor_config.json")
    if not ((path / "tokenizer.json").is_file() or (path / "vocab.json").is_file() and (path / "merges.txt").is_file()):
        raise ModelValidationError("缺少 Whisper tokenizer 文件")
    weights = _weight_files(path)

    try:
        if weights[0].suffix == ".safetensors":
            from safetensors import safe_open
            for weight in weights:
                with safe_open(weight, framework="pt", device="cpu") as handle:
                    if not handle.keys():
                        raise ModelValidationError(f"权重文件为空: {weight.name}")
        else:
            import torch
            for weight in weights:
                state = torch.load(weight, map_location="cpu", weights_only=True)
                if not isinstance(state, dict) or not state:
                    raise ModelValidationError(f"权重结构无效: {weight.name}")
                del state
        from transformers import WhisperForConditionalGeneration, WhisperProcessor, pipeline

        processor = WhisperProcessor.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
        model, loading = WhisperForConditionalGeneration.from_pretrained(
            str(path), local_files_only=True, trust_remote_code=False,
            use_safetensors=weights[0].suffix == ".safetensors", output_loading_info=True,
        )
        if loading.get("missing_keys") or loading.get("mismatched_keys"):
            raise ModelValidationError("权重缺少 Whisper 所需参数或参数形状不匹配")
        recognizer = pipeline(
            "automatic-speech-recognition", model=model,
            tokenizer=processor.tokenizer, feature_extractor=processor.feature_extractor,
            device="cpu",
        )
        import numpy as np
        result = recognizer(
            {"array": np.zeros(16000, dtype=np.float32), "sampling_rate": 16000},
            return_timestamps=True, generate_kwargs={"task": "transcribe"},
        )
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise ModelValidationError("模型输出不符合 Whisper ASR 协议")
        chunks = result.get("chunks")
        if not isinstance(chunks, list) or (result["text"].strip() and not chunks):
            raise ModelValidationError("模型输出缺少时间戳片段")
        for chunk in chunks:
            if not isinstance(chunk.get("text"), str) or not isinstance(chunk.get("timestamp"), (tuple, list)) or len(chunk["timestamp"]) != 2:
                raise ModelValidationError("模型时间戳输出无效")
            start, end = chunk["timestamp"]
            if start is None or not isinstance(start, (float, int)) or not math.isfinite(start) or start < 0 or end is not None and (not isinstance(end, (float, int)) or not math.isfinite(end) or end < start):
                raise ModelValidationError("模型时间戳输出无效")
    except ModelValidationError:
        raise
    except ImportError as exc:
        raise ModelDependencyError("请先启用 Hugging Face 模型依赖：uv sync --extra hf-models") from exc
    except Exception as exc:
        raise ModelValidationError(f"模型离线加载或推理检查失败: {exc}") from exc


class LocalModelManager:
    """只管理本应用目录中的副本，用户原文件不会被清理。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="whisper-model")

    @property
    def root(self) -> Path:
        configured = getattr(settings, "local_whisper_download_root", None)
        root = Path(configured).expanduser() if configured else Path(settings.data_dir) / "models"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _disk_ready(self, name: str) -> bool:
        path = self.root / name
        if path.is_symlink() or not path.is_dir():
            return False
        if (path / _DOWNLOAD_MANIFEST).exists() or (path / _DOWNLOAD_MANIFEST).is_symlink():
            return (
                _regular_size(path / ".subtrans-ready") is not None
                and _manifest_ready(path, _download_manifest(path))
            )
        if os.environ.get("SUBTRANS_DESKTOP") == "1" and name in MODEL_NAMES:
            return False
        return path.is_dir() and not path.is_symlink() and (
            (path / _HF_MARKER).is_file() or
            (name in MODEL_NAMES and (path / ".subtrans-ready").is_file())
        )

    def is_ready(self, name: str) -> bool:
        if os.environ.get("SUBTRANS_DESKTOP") == "1" and self._states.get(name, {}).get("status") in {"DOWNLOADING", "ERROR"}:
            return False
        return bool(_NAME.fullmatch(name)) and self._disk_ready(name)

    def is_hf(self, name: str) -> bool:
        return self.is_ready(name) and (self.root / name / _HF_MARKER).is_file()

    def resolve_path(self, name: str) -> str:
        if not self.is_ready(name):
            raise RuntimeError(f"本地 Whisper 模型尚未就绪: {name}")
        return str(self.root / name)

    def list_models(self) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for item in MODEL_CATALOG:
                name = item["name"]
                state = self._states.get(name, {})
                ready = self._disk_ready(name) and state.get("status") not in {"DOWNLOADING", "ERROR"}
                path = self.root / name
                meta = _download_manifest(path) if ready else {}
                installed = sum(_regular_size(p) or 0 for p in path.rglob("*")) if ready else 0
                downloaded = state.get("downloadedBytes", 0)
                if state.get("stage"):
                    downloaded = _downloaded_bytes(Path(state["stage"]))
                if ready and meta:
                    downloaded = sum(meta["files"].values())
                result.append({
                    **item, "status": "READY" if ready else (
                        "NOT_INSTALLED" if state.get("status") == "READY" else state.get("status", "NOT_INSTALLED")
                    ),
                    "format": "ctranslate2", "source": "official", "installedBytes": installed,
                    "error": state.get("error"), "phase": "ready" if ready else state.get("phase"),
                    "downloadedBytes": downloaded,
                    "totalBytes": meta.get("totalBytes", state.get("totalBytes")),
                    "revision": meta.get("revision", state.get("revision")),
                })
            for path in sorted(self.root.iterdir()):
                if path.name in MODEL_NAMES or not path.is_dir() or path.is_symlink() or not (path / _HF_MARKER).is_file():
                    continue
                try:
                    meta = json.loads((path / _HF_MARKER).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                installed = sum(p.stat().st_size for p in path.iterdir() if p.is_file())
                result.append({"name": path.name, "label": meta.get("label", path.name), "size": None, "installedBytes": installed, "status": "READY", "format": "huggingface", "source": "imported"})
            return result

    def download(self, name: str, *, revision: str | None = None,
                 endpoint: str | None = None) -> dict[str, Any]:
        if name not in MODEL_NAMES:
            raise FileNotFoundError(f"未知官方模型: {name}")
        desktop = os.environ.get("SUBTRANS_DESKTOP") == "1"
        if desktop:
            revision = revision or OFFICIAL_MODEL_REVISIONS.get(name)
            endpoint = endpoint or os.environ.get("SUBTRANS_DESKTOP_MODEL_ENDPOINT")
            if not revision:
                raise ModelValidationError(f"桌面模型尚无已核实的固定版本: {name}")
        endpoint = _model_endpoint(endpoint)
        if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)):
            raise ModelValidationError("模型 revision 必须是完整的 40 位提交 SHA")
        with self._lock:
            if (self._states.get(name, {}).get("status") not in {"ERROR", "DOWNLOADING"}
                    and self._disk_ready(name)
                    and (not revision or _download_manifest(self.root / name).get("revision") == revision)):
                return next(item for item in self.list_models() if item["name"] == name)
            if self._states.get(name, {}).get("status") != "DOWNLOADING":
                previous = self._states.get(name, {})
                repair = previous.get("invalid", False) or previous.get("repair", False)
                self._states[name] = {
                    "status": "DOWNLOADING", "phase": "queued", "error": None,
                    "downloadedBytes": 0, "totalBytes": None, "revision": revision,
                    "repair": repair,
                }
                kwargs = {}
                if revision is not None:
                    kwargs["revision"] = revision
                if endpoint is not None:
                    kwargs["endpoint"] = endpoint
                self._executor.submit(self._download_official, name, **kwargs)
            return next(item for item in self.list_models() if item["name"] == name)

    def mark_failed(self, name: str, error: Exception) -> None:
        """Invalidate a failed offline load without removing any installed files."""
        with self._lock:
            if self._states.get(name, {}).get("status") != "DOWNLOADING":
                self._states[name] = {"status": "ERROR", "phase": "error", "error": str(error), "invalid": True}

    def _download_official(self, name: str, *, revision: str | None = None,
                           endpoint: str | None = None) -> None:
        if revision is not None or endpoint is not None:
            self._download_pinned(name, revision=revision, endpoint=endpoint)
            return
        stage = None
        try:
            from huggingface_hub import snapshot_download
            from faster_whisper import WhisperModel

            stage = Path(tempfile.mkdtemp(prefix=f".download-{name}-", dir=self.root))
            with self._lock:
                self._states[name].update(phase="downloading", stage=str(stage), revision="main")
            snapshot_download(
                repo_id=f"Systran/faster-whisper-{name}",
                revision="main",
                local_dir=str(stage),
            )
            if not (stage / "model.bin").is_file() or not (stage / "config.json").is_file():
                raise ModelValidationError("官方模型文件不完整")
            with self._lock:
                self._states[name]["phase"] = "checking"
            WhisperModel(str(stage), device="cpu", compute_type="int8", local_files_only=True)
            (stage / ".subtrans-ready").touch()
            with self._lock:
                destination = self.root / name
                if destination.exists() or destination.is_symlink():
                    raise FileExistsError(f"模型目录已存在: {name}")
                stage.rename(destination)
                stage = None
                self._states[name] = {
                    "status": "READY", "phase": "ready", "revision": "main",
                    "downloadedBytes": _downloaded_bytes(destination), "totalBytes": None,
                }
        except Exception as exc:
            with self._lock:
                self._states[name] = {
                    "status": "ERROR", "phase": "error", "error": str(exc),
                    "revision": "main", "totalBytes": None,
                    "downloadedBytes": _downloaded_bytes(stage) if stage else 0,
                }
        finally:
            if stage is not None and stage.exists():
                shutil.rmtree(stage)

    def _download_pinned(self, name: str, *, revision: str | None,
                         endpoint: str | None) -> None:
        stage = None
        try:
            from huggingface_hub import HfApi, snapshot_download
            from faster_whisper import WhisperModel

            # Metadata and payloads always use the same explicit origin. Public
            # models never inherit a user's HF token or ambient HF_ENDPOINT.
            origin = _model_endpoint(endpoint) or "https://huggingface.co"
            with _desktop_stage(self.root, name) as stage:
                with self._lock:
                    if self._disk_ready(name) and not self._states[name].get("repair"):
                        installed_revision = _download_manifest(self.root / name).get("revision")
                        if not revision or installed_revision == revision:
                            self._states.pop(name, None)
                            return
                    self._states[name].update(phase="metadata", stage=str(stage))
                info = HfApi(endpoint=origin, token=False).model_info(
                    f"Systran/faster-whisper-{name}", revision=revision or "main",
                    files_metadata=True, timeout=15,
                )
                resolved = info.sha
                if not isinstance(resolved, str) or not re.fullmatch(r"[0-9a-f]{40}", resolved):
                    raise ModelValidationError("模型源未返回有效提交 SHA")
                if revision and resolved != revision:
                    raise ModelValidationError("模型源返回的版本与固定版本不一致")
                entries = {entry.rfilename: entry for entry in info.siblings
                           if entry.rfilename in _OFFICIAL_FILES}
                if set(entries) != set(_OFFICIAL_FILES):
                    raise ModelValidationError("官方模型元数据缺少必要文件")
                sizes = [getattr(entry, "size", None) for entry in entries.values()]
                total = sum(sizes) if all(type(size) is int and size >= 0 for size in sizes) else None
                with self._lock:
                    self._states[name].update(revision=resolved, totalBytes=total)
                if total is not None and shutil.disk_usage(stage).free < total + 16 * 1024 * 1024:
                    raise ModelValidationError("磁盘可用空间不足，请释放空间后重试")
                with self._lock:
                    self._states[name]["phase"] = "downloading"
                try:
                    snapshot_download(
                        repo_id=f"Systran/faster-whisper-{name}", revision=resolved,
                        local_dir=str(stage), endpoint=origin, token=False,
                        allow_patterns=list(_OFFICIAL_FILES), max_workers=2,
                    )
                finally:
                    with self._lock:
                        self._states[name]["downloadedBytes"] = _downloaded_bytes(stage)
                with self._lock:
                    self._states[name].update(phase="checking", downloadedBytes=_downloaded_bytes(stage))
                file_sizes = _verify_official_files(stage, entries)
                model = WhisperModel(str(stage), device="cpu", compute_type="int8", local_files_only=True)
                del model
                # Only payloads and our markers are published, not Hub cache state.
                for entry in stage.iterdir():
                    if entry.name not in _OFFICIAL_FILES:
                        if entry.is_dir() and not entry.is_symlink():
                            shutil.rmtree(entry)
                        else:
                            entry.unlink()
                (stage / _DOWNLOAD_MANIFEST).write_text(json.dumps({
                    "revision": resolved, "files": file_sizes,
                    "stamps": _file_stamps(stage, _OFFICIAL_FILES),
                    "validated": "cpu/int8", "totalBytes": total,
                }), encoding="utf-8")
                (stage / ".subtrans-ready").touch()
                destination = self.root / name
                _publish_model(stage, destination)
                with self._lock:
                    self._states.pop(name, None)
        except Exception as exc:
            with self._lock:
                state = self._states.setdefault(name, {})
                state.pop("stage", None)
                state.update(status="ERROR", phase="error", error=str(exc))

    def import_files(self, name: str, label: str, files: list[tuple[str, BinaryIO]]) -> dict[str, Any]:
        _safe_name(name)
        if not files:
            raise ModelValidationError("请选择模型目录中的文件")
        with self._lock:
            destination = self.root / name
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f"模型名称已存在: {name}")
            stage = Path(tempfile.mkdtemp(prefix=".import-", dir=self.root))
            try:
                seen: set[str] = set()
                for filename, source in files:
                    filename = _safe_filename(filename)
                    if filename in seen:
                        raise ModelValidationError(f"重复的模型文件: {filename}")
                    seen.add(filename)
                    with (stage / filename).open("xb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                validate_hf_model(stage)
                (stage / _HF_MARKER).write_text(json.dumps({"label": label[:100] or name}), encoding="utf-8")
                stage.rename(destination)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
            return next(item for item in self.list_models() if item["name"] == name)

    def check(self, name: str) -> dict[str, Any]:
        _safe_name(name)
        with self._lock:
            path = self.root / name
            if not self.is_hf(name):
                raise FileNotFoundError(f"未找到受管 Hugging Face 模型: {name}")
            try:
                validate_hf_model(path)
            except ModelValidationError:
                shutil.rmtree(path)
                raise
            return next(item for item in self.list_models() if item["name"] == name)

    def delete(self, name: str) -> dict[str, Any]:
        with self._lock:
            if self._states.get(name, {}).get("status") == "DOWNLOADING":
                raise RuntimeError("模型正在下载，暂时无法删除")
            if not _NAME.fullmatch(name) or not self.is_ready(name):
                raise FileNotFoundError(f"本地模型不存在: {name}")
            shutil.rmtree(self.root / name)
            self._states.pop(name, None)
        return {"name": name, "status": "NOT_INSTALLED"}


model_manager = LocalModelManager()
