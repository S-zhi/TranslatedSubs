"""Manage the optional, offline English-to-Chinese CPU translation model."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import inspect
import json
import logging
import os
import shutil
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from src.config import settings


MODEL_REPO = "Helsinki-NLP/opus-mt-en-zh"
# Pinned to an immutable Hugging Face revision. Kept as a constant so installs
# never silently follow a moving branch.
MODEL_REVISION = "408d9bc410a388e1d9aef112a2daba955b945255"
ENGINE_ID = "local-opus-en-zh"
MODEL_NAME = "本地 CPU 英译中"
_PROVENANCE = ".subtrans-translation.json"
MODEL_STATUS_NOT_INSTALLED = "NOT_INSTALLED"
MODEL_STATUS_DOWNLOADING = "DOWNLOADING"
MODEL_STATUS_CONVERTING = "CONVERTING"
MODEL_STATUS_CHECKING = "CHECKING"
MODEL_STATUS_READY = "READY"
MODEL_STATUS_FAILED = "FAILED"
logger = logging.getLogger(__name__)

_ALLOWED_FILES = (
    "config.json",
    "pytorch_model.bin",
    "source.spm",
    "target.spm",
    "vocab.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "generation_config.json",
)
_TOKENIZER_FILES = (
    "source.spm",
    "target.spm",
    "vocab.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "generation_config.json",
)
_TARGET_PREFIX = ">>cmn_Hans<<"
_MAX_INPUT_TOKENS = 512
_LOCAL_CT2_INTER_THREADS = 1
_LOCAL_CT2_INTRA_THREADS = 4
_LOCAL_CT2_BATCH_TYPE = "tokens"
_LOCAL_CT2_MAX_BATCH_SIZE = 2048
_LOCAL_CT2_BEAM_SIZE = 2
_LOCAL_CT2_MAX_DECODING_LENGTH = 512


class TranslationModelError(RuntimeError):
    def __init__(self, message: str, *, code: str = "translation_model_error"):
        super().__init__(message)
        self.code = code


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                # A downloader can replace a file between traversal and stat.
                continue
    except FileNotFoundError:
        # Publication or cleanup can remove a directory before rglob scans it.
        pass
    return total


class TranslationModelManager:
    """Download, convert, validate and serve the one supported local model."""

    def __init__(
        self,
        model_dir: Optional[Path | str] = None,
        *,
        revision: str = MODEL_REVISION,
        snapshot_download_fn: Optional[Callable] = None,
        converter_factory: Optional[Callable] = None,
        runtime_factory: Optional[Callable] = None,
        endpoint: Optional[str] = None,
    ):
        base_dir = Path(settings.data_dir) / "models" / "translation"
        self.model_dir = Path(model_dir) if model_dir is not None else base_dir / "opus-mt-en-zh"
        self.revision = revision
        self.endpoint = endpoint
        self._snapshot_download_fn = snapshot_download_fn
        self._converter_factory = converter_factory
        self._runtime_factory = runtime_factory
        self._lock = threading.RLock()
        self._runtime_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-translation-model")
        self._future: Optional[Future] = None
        self._warm_future: Optional[Future] = None
        self._runtime: Optional[tuple[object, object]] = None
        self._runtime_key: tuple[str, int, int] | None = None
        self._status = MODEL_STATUS_READY if self._installed(self.model_dir) else MODEL_STATUS_NOT_INSTALLED
        self._error: Optional[str] = None
        self._working_dir: Optional[Path] = None
        # None means this attempt has no observable transfer counters. Never
        # derive these from staging size: conversion writes unrelated bytes.
        self._downloaded_bytes: Optional[int] = None
        self._total_bytes: Optional[int] = None

    @staticmethod
    def _installed(path: Path) -> bool:
        required = ("config.json", "model.bin", *_TOKENIZER_FILES[:4])
        if not path.is_dir():
            return False
        try:
            if not all(
                (path / name).is_file() and (path / name).stat().st_size > 0
                for name in required
            ):
                return False
            has_shared_vocabulary = (
                (path / "shared_vocabulary.json").is_file()
                and (path / "shared_vocabulary.json").stat().st_size > 0
            )
            has_separate_vocabularies = all(
                (path / name).is_file() and (path / name).stat().st_size > 0
                for name in ("source_vocabulary.json", "target_vocabulary.json")
            )
            return has_shared_vocabulary or has_separate_vocabularies
        except OSError:
            return False

    def status(self) -> dict:
        with self._lock:
            status = self._status
            if status == MODEL_STATUS_NOT_INSTALLED and self._installed(self.model_dir):
                status = MODEL_STATUS_READY
                self._status = status
            elif status == MODEL_STATUS_READY and not self._installed(self.model_dir):
                status = MODEL_STATUS_FAILED
                self._status = status
                self._error = "本地模型文件不完整或已丢失"
            provenance = self._provenance()
            if (status == MODEL_STATUS_READY and os.environ.get("SUBTRANS_DESKTOP") == "1"
                    and provenance.get("revision") != self.revision):
                status = self._status = MODEL_STATUS_FAILED
                self._error = "本地模型来源未知、版本不符或文件已变化，请重试模型准备"
            measured_dir = self._working_dir if self._working_dir and self._working_dir.exists() else self.model_dir
            restored_progress = status == MODEL_STATUS_READY and self._downloaded_bytes is None
            return {
                "model_status": status,
                "installed_bytes": _directory_size(measured_dir),
                "model_error": self._error,
                "downloaded_bytes": provenance.get("downloaded_bytes") if restored_progress else self._downloaded_bytes,
                "total_bytes": provenance.get("total_bytes") if restored_progress else self._total_bytes,
                "revision": provenance.get("revision"),
                "phase": status.lower(),
            }

    def _provenance(self) -> dict:
        """Only a persisted, unchanged, validated install can attest its pin."""
        from src.service.model_manager import _stamps_match

        marker = self.model_dir / _PROVENANCE
        try:
            if self.model_dir.is_symlink() or marker.is_symlink() or marker.stat().st_size > 16384:
                return {}
            value = json.loads(marker.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("repo") != MODEL_REPO
                    or value.get("validated") != "cpu/int8"
                    or not isinstance(value.get("revision"), str)
                    or len(value["revision"]) != 40
                    or any(c not in "0123456789abcdef" for c in value["revision"])
                    or not _stamps_match(self.model_dir, value.get("files"))):
                return {}
            required = {"config.json", "model.bin", *_TOKENIZER_FILES[:4]}
            if not required.issubset(value["files"]) or not self._installed(self.model_dir):
                return {}
            return value
        except (OSError, ValueError, TypeError):
            return {}

    def is_ready(self) -> bool:
        with self._lock:
            if os.environ.get("SUBTRANS_DESKTOP") == "1":
                return self.status()["model_status"] == MODEL_STATUS_READY
            if self._status == MODEL_STATUS_FAILED or not self._installed(self.model_dir):
                if self._status == MODEL_STATUS_READY:
                    self._status = MODEL_STATUS_FAILED
                    self._error = "本地模型文件不完整或已丢失"
                return False
            return True

    @staticmethod
    def dependency_status() -> dict[str, object]:
        """Check optional local translation modules without importing them."""
        modules = {
            "ctranslate2": "ctranslate2",
            "transformers": "transformers",
            "sentencepiece": "sentencepiece",
            "sacremoses": "sacremoses",
        }
        missing = [package for package, module in modules.items() if importlib.util.find_spec(module) is None]
        return {"ready": not missing, "missing": missing}

    def mark_failed(self, error: Exception) -> None:
        with self._lock:
            self._status = MODEL_STATUS_FAILED
            self._error = str(error)

    def start_warmup(self) -> bool:
        """Load and smoke-test an installed runtime asynchronously, once."""
        with self._lock:
            if self._future is not None and not self._future.done():
                return False
            if self._warm_future is not None and not self._warm_future.done():
                return False
            if not self.is_ready():
                return False
            try:
                dependencies = self.dependency_status()
            except Exception:
                logger.exception("本地翻译依赖状态检查失败，跳过启动预热")
                return False
            if not dependencies.get("ready", False):
                return False
            # Do not wait for a task that is already loading the runtime.  A
            # startup hook must return immediately; that task will populate the
            # same cache and the warmup worker would have no useful work left.
            if not self._runtime_lock.acquire(blocking=False):
                return False
            try:
                if self._runtime is not None:
                    return False
            finally:
                self._runtime_lock.release()
            self._warm_future = self._executor.submit(self._warm_runtime)
            return True

    def _warm_runtime(self) -> None:
        """Load the cached runtime and run a tiny local smoke test."""
        try:
            tokenizer, translator = self._get_runtime()
            result = self._translate_batch(["Hello."], tokenizer, translator)
            if len(result) != 1 or not result[0].strip():
                raise TranslationModelError(
                    "本地模型启动预热未返回译文", code="smoke_test_failed"
                )
        except Exception as exc:
            error = (
                exc if isinstance(exc, TranslationModelError)
                else TranslationModelError(
                    f"本地模型启动预热失败：{exc}", code="warmup_failed"
                )
            )
            self.mark_failed(error)
            logger.warning("本地翻译模型启动预热失败：%s", error)

    def cancel_warmup(self) -> bool:
        """Cancel a warmup task only if it has not started running yet."""
        with self._lock:
            future = self._warm_future
            if future is None:
                return False
            cancelled = future.cancel()
            if cancelled:
                self._warm_future = None
            return cancelled

    def start_download(self) -> dict:
        """Start one install in a background worker and return its current state."""
        with self._lock:
            if self._future is not None and not self._future.done():
                return self.status()
            provenance = self._provenance()
            needs_pin = os.environ.get("SUBTRANS_DESKTOP") == "1" or bool(provenance)
            if (self._status != MODEL_STATUS_FAILED and self.is_ready()
                    and (not needs_pin or provenance.get("revision") == self.revision)):
                self._status = MODEL_STATUS_READY
                self._error = None
                return self.status()
            self._status = MODEL_STATUS_DOWNLOADING
            self._error = None
            self._working_dir = None
            self._downloaded_bytes = None
            self._total_bytes = None
            self._future = self._executor.submit(self._install)
            return self.status()

    def validate_offline(self) -> None:
        """Load and run a short CPU translation without any network fallback."""
        if not self.is_ready():
            raise TranslationModelError("本地翻译模型尚未安装", code="model_not_ready")
        try:
            tokenizer, translator = self._get_runtime()
            result = self._translate_batch(["Hello."], tokenizer, translator)
            if len(result) != 1 or not result[0].strip():
                raise TranslationModelError("本地模型烟测未返回译文", code="smoke_test_failed")
        except TranslationModelError as exc:
            self.mark_failed(exc)
            raise
        except Exception as exc:
            wrapped = TranslationModelError(f"本地模型验证失败：{exc}", code="smoke_test_failed")
            self.mark_failed(wrapped)
            raise wrapped from exc

    def translate_texts(self, texts: list[str], *, cancel_check=None) -> list[str]:
        if not texts:
            return []
        if not self.is_ready():
            raise TranslationModelError("本地翻译模型尚未安装或未就绪", code="model_not_ready")
        try:
            tokenizer, translator = self._get_runtime()
            encoded_texts = [self._encode(text, tokenizer) for text in texts]
        except TranslationModelError:
            raise
        except Exception as exc:
            raise TranslationModelError(f"本地模型处理输入失败：{exc}", code="model_input_failed") from exc
        if cancel_check is not None:
            cancel_check()
        try:
            return self._translate_encoded(encoded_texts, tokenizer, translator)
        except TranslationModelError as exc:
            if exc.code in {"invalid_response", "local_model_failed"}:
                self.mark_failed(exc)
            raise

    def _install(self) -> None:
        # Separate managers/processes sharing a desktop home must not publish
        # competing conversions. No lock files are added in legacy server mode.
        try:
            with self._installation_lock():
                self._install_model()
        except Exception as exc:
            self.mark_failed(exc)

    @contextmanager
    def _installation_lock(self):
        if os.environ.get("SUBTRANS_DESKTOP") != "1":
            yield
            return
        import fcntl

        self.model_dir.parent.mkdir(parents=True, exist_ok=True)
        if any(path.is_symlink() for path in (self.model_dir, *self.model_dir.parents)):
            raise TranslationModelError("模型存储路径不能包含符号链接")
        fd = os.open(self.model_dir.parent / ".translation-install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _install_model(self) -> None:
        work_dir: Optional[Path] = None
        preserve_backup = False
        try:
            with self._lock:
                self._status = MODEL_STATUS_DOWNLOADING
            self.model_dir.parent.mkdir(parents=True, exist_ok=True)
            work_dir = Path(tempfile.mkdtemp(prefix=".opus-mt-en-zh-", dir=self.model_dir.parent))
            with self._lock:
                self._working_dir = work_dir
            snapshot_dir = work_dir / "snapshot"
            snapshot_dir.mkdir()
            snapshot_path = self._download_snapshot(snapshot_dir)

            with self._lock:
                self._status = MODEL_STATUS_CONVERTING
            # Source bytes are only a conservative conversion-space budget,
            # never reported as bytes transferred or exact output size.
            self._ensure_free_space(_directory_size(snapshot_path), work_dir)
            converted_dir = work_dir / "converted"
            converter = self._make_converter(snapshot_path)
            converter.convert(str(converted_dir), quantization="int8")
            for name in _TOKENIZER_FILES:
                source = snapshot_path / name
                if source.is_file():
                    shutil.copy2(source, converted_dir / name)

            if not self._installed(converted_dir):
                raise TranslationModelError("模型转换缺少 CTranslate2 或 Marian tokenizer 文件", code="conversion_failed")

            with self._lock:
                self._status = MODEL_STATUS_CHECKING
            inter_threads = max(1, int(settings.local_ct2_inter_threads))
            intra_threads = max(1, int(settings.local_ct2_intra_threads))
            tokenizer, translator = self._load_runtime(
                converted_dir, inter_threads=inter_threads, intra_threads=intra_threads,
            )
            smoke = self._translate_batch(["Hello."], tokenizer, translator)
            if len(smoke) != 1 or not smoke[0].strip():
                raise TranslationModelError("CPU INT8 模型烟测未返回译文", code="smoke_test_failed")

            from src.service.model_manager import _file_stamps
            from src.desktop.models import model_install_guard

            files = [path.name for path in converted_dir.iterdir() if path.is_file()]
            (converted_dir / _PROVENANCE).write_text(json.dumps({
                "repo": MODEL_REPO, "revision": self.revision, "validated": "cpu/int8",
                "files": _file_stamps(converted_dir, files),
                "downloaded_bytes": self._downloaded_bytes, "total_bytes": self._total_bytes,
            }), encoding="utf-8")

            backup = work_dir / "previous"
            with model_install_guard() if self.model_dir.exists() else nullcontext():
                with self._runtime_lock:
                    if self.model_dir.exists():
                        self.model_dir.rename(backup)
                        preserve_backup = True
                    try:
                        converted_dir.rename(self.model_dir)
                    except Exception:
                        if backup.exists() and not self.model_dir.exists():
                            try:
                                backup.rename(self.model_dir)
                                preserve_backup = False
                            except OSError as exc:
                                raise TranslationModelError(
                                    f"模型发布与恢复失败，旧模型仍保留在 {backup}", code="publish_failed"
                                ) from exc
                        raise
                    # Desktop predecessors are retained, never deleted while a
                    # pipeline might still hold its tokenizer/runtime files.
                    preserve_backup = backup.exists() and os.environ.get("SUBTRANS_DESKTOP") == "1"
                    with self._lock:
                        self._runtime = (tokenizer, translator)
                        self._runtime_key = (str(self.model_dir), inter_threads, intra_threads)
                        self._status = MODEL_STATUS_READY
                        self._error = None
        except Exception as exc:
            with self._lock:
                failed_phase = self._status.lower()
                self._status = MODEL_STATUS_FAILED
                message = "磁盘空间不足，请释放空间后重试" if (
                    isinstance(exc, OSError) and exc.errno == errno.ENOSPC
                ) else str(exc)
                self._error = f"{failed_phase}: {message}"
        finally:
            if work_dir is not None and not preserve_backup:
                shutil.rmtree(work_dir, ignore_errors=True)
            elif work_dir is not None:
                # Retain only the predecessor/recovery data, not an extra copy
                # of downloaded source weights after conversion has finished.
                shutil.rmtree(work_dir / "snapshot", ignore_errors=True)
            with self._lock:
                self._working_dir = None

    def _download_snapshot(self, local_dir: Path) -> Path:
        endpoint = self._download_endpoint()
        if self._snapshot_download_fn is None:
            returned = self._download_http_snapshot(local_dir, endpoint)
        else:
            download_fn = self._snapshot_download_fn
            kwargs = dict(
                repo_id=MODEL_REPO, revision=self.revision,
                allow_patterns=list(_ALLOWED_FILES), local_dir=str(local_dir),
            )
            # Legacy injected functions accept exactly these four arguments.
            # Opt-in progress callbacks report absolute transfer bytes.
            try:
                parameters = inspect.signature(download_fn).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "progress_callback" in parameters:
                kwargs["progress_callback"] = self._record_download_progress
            if endpoint is not None:
                if "endpoint" not in parameters and not any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()
                ):
                    raise TranslationModelError(
                        "注入的下载器不支持配置模型源", code="unsupported_endpoint"
                    )
                kwargs["endpoint"] = endpoint
            returned = download_fn(**kwargs)
        snapshot_path = Path(returned) if returned else local_dir
        if not snapshot_path.is_dir():
            snapshot_path = local_dir
        if not (snapshot_path / "config.json").is_file() or not (snapshot_path / "pytorch_model.bin").is_file():
            raise TranslationModelError("模型仓库缺少所需权重或配置文件", code="download_failed")
        return snapshot_path

    def _download_endpoint(self) -> Optional[str]:
        endpoint = self.endpoint
        if endpoint is None and os.environ.get("SUBTRANS_DESKTOP") == "1":
            endpoint = os.environ.get("SUBTRANS_DESKTOP_MODEL_ENDPOINT") or None
        if endpoint is None:
            return None
        try:
            parsed = urlsplit(endpoint)
            valid = (
                parsed.scheme == "https" and parsed.hostname and parsed.port != 0
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
                and not any(character.isspace() for character in endpoint)
            )
        except ValueError:
            valid = False
        if not valid:
            raise TranslationModelError("模型源必须是无凭据的 HTTPS 地址", code="invalid_endpoint")
        return endpoint.rstrip("/")

    @staticmethod
    def _ensure_free_space(required_bytes: int, path: Path) -> None:
        if required_bytes > shutil.disk_usage(path).free:
            raise TranslationModelError(
                f"磁盘空间不足，当前阶段至少需要 {required_bytes} 字节可用空间，请释放空间后重试",
                code="insufficient_disk_space",
            )

    def _record_download_progress(self, downloaded_bytes: int, total_bytes: Optional[int]) -> None:
        if (
            type(downloaded_bytes) is not int or downloaded_bytes < 0
            or (total_bytes is not None and (
                type(total_bytes) is not int or total_bytes < downloaded_bytes
            ))
        ):
            raise TranslationModelError("下载器返回了无效的字节进度", code="invalid_progress")
        with self._lock:
            if self._status != MODEL_STATUS_DOWNLOADING:
                return
            self._downloaded_bytes = downloaded_bytes
            self._total_bytes = total_bytes

    def _download_http_snapshot(self, local_dir: Path, endpoint: Optional[str]) -> Path:
        """Stream the pinned public files; count only bytes received this attempt.

        Hub 0.36 snapshot_download exposes a file-count bar, not transfer bytes.
        A private HTTP client avoids global progress patches shared with Whisper.
        Failed attempts are cleaned and retried from zero, not silently resumed.
        """
        import httpx
        from huggingface_hub import HfApi, hf_hub_url

        endpoint = endpoint or "https://huggingface.co"
        try:
            info = HfApi(endpoint=endpoint, token=False).model_info(
                MODEL_REPO, revision=self.revision, files_metadata=True, timeout=15,
            )
        except Exception as exc:
            raise TranslationModelError("获取模型文件信息失败，请检查网络和模型源后重试", code="download_failed") from exc
        if info.sha != self.revision:
            raise TranslationModelError("模型源返回的版本与固定版本不一致", code="revision_mismatch")
        files = {item.rfilename: item for item in (info.siblings or []) if item.rfilename in _ALLOWED_FILES}
        required = {"config.json", "pytorch_model.bin", *_TOKENIZER_FILES[:4]}
        if not required.issubset(files):
            raise TranslationModelError("模型仓库缺少所需权重或 tokenizer 文件", code="download_failed")
        sizes = [getattr(item, "size", None) for item in files.values()]
        total = sum(sizes) if all(type(size) is int and size >= 0 for size in sizes) else None
        self._record_download_progress(0, total)
        if total is not None:
            # Reserve source + a conservative source-sized conversion budget.
            self._ensure_free_space(total * 2, local_dir)
        downloaded = 0

        def require_https(request):
            if request.url.scheme != "https":
                raise TranslationModelError("模型下载重定向必须使用 HTTPS", code="invalid_endpoint")

        try:
            with httpx.Client(
                follow_redirects=True, timeout=30,
                headers={"Accept-Encoding": "identity"},
                event_hooks={"request": [require_https]},
            ) as client:
                for name, item in files.items():
                    size = getattr(item, "size", None)
                    size = size if type(size) is int and size >= 0 else None
                    lfs = getattr(item, "lfs", None)
                    expected_hash = getattr(lfs, "sha256", None)
                    if isinstance(lfs, dict):
                        expected_hash = lfs.get("sha256")
                    digest = hashlib.sha256() if lfs else hashlib.sha1()
                    if not lfs and size is not None:
                        digest.update(f"blob {size}\0".encode())
                        expected_hash = getattr(item, "blob_id", None)
                    received = 0
                    url = hf_hub_url(MODEL_REPO, name, revision=self.revision, endpoint=endpoint)
                    with client.stream("GET", url) as response:
                        response.raise_for_status()
                        if response.status_code != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
                            raise TranslationModelError("模型源返回了非完整原始文件", code="download_failed")
                        with (local_dir / name).open("wb") as output:
                            for chunk in response.iter_raw():
                                received += len(chunk)
                                downloaded += len(chunk)
                                if size is not None and received > size:
                                    raise TranslationModelError("模型文件大小与元数据不一致", code="download_failed")
                                self._record_download_progress(downloaded, total)
                                output.write(chunk)
                                digest.update(chunk)
                    if (size is not None and received != size) or (
                        expected_hash and digest.hexdigest() != expected_hash
                    ):
                        raise TranslationModelError("模型文件完整性校验失败，请重试", code="download_failed")
        except httpx.HTTPError as exc:
            # Avoid exposing signed CDN URLs in public status/error logs.
            raise TranslationModelError("模型下载失败，请检查网络和模型源后重试", code="download_failed") from exc
        return local_dir

    def _make_converter(self, source_dir: Path):
        if self._converter_factory is not None:
            return self._converter_factory(str(source_dir))
        from ctranslate2.converters import TransformersConverter

        return TransformersConverter(str(source_dir))

    def _get_runtime(self) -> tuple[object, object]:
        with self._runtime_lock:
            inter_threads = max(1, int(settings.local_ct2_inter_threads))
            intra_threads = max(1, int(settings.local_ct2_intra_threads))
            runtime_key = (str(self.model_dir), inter_threads, intra_threads)
            if self._runtime is None or self._runtime_key != runtime_key:
                self._runtime = self._load_runtime(
                    self.model_dir,
                    inter_threads=inter_threads,
                    intra_threads=intra_threads,
                )
                self._runtime_key = runtime_key
            return self._runtime

    def _load_runtime(
        self,
        model_dir: Path,
        *,
        inter_threads: int | None = None,
        intra_threads: int | None = None,
    ) -> tuple[object, object]:
        try:
            if self._runtime_factory is not None:
                return self._runtime_factory(str(model_dir))
            from ctranslate2 import Translator
            from transformers import MarianTokenizer

            tokenizer = MarianTokenizer.from_pretrained(str(model_dir), local_files_only=True)
            translator = Translator(
                str(model_dir),
                device="cpu",
                compute_type="int8",
                inter_threads=max(1, int(inter_threads or settings.local_ct2_inter_threads)),
                intra_threads=max(1, int(intra_threads or settings.local_ct2_intra_threads)),
            )
            return tokenizer, translator
        except ImportError as exc:
            raise TranslationModelError(
                "本地翻译依赖未安装，请运行 uv sync --extra local-translation",
                code="missing_dependency",
            ) from exc
        except TranslationModelError:
            raise
        except Exception as exc:
            raise TranslationModelError(f"本地模型加载失败：{exc}", code="model_load_failed") from exc

    @staticmethod
    def _encode(text: str, tokenizer) -> list[int]:
        supported = getattr(tokenizer, "supported_language_codes", None) or []
        if _TARGET_PREFIX not in supported:
            raise TranslationModelError("tokenizer 不支持简体中文目标代码 >>cmn_Hans<<", code="unsupported_language")
        encoded = tokenizer.encode(f"{_TARGET_PREFIX} {text}", add_special_tokens=True)
        ids = list(encoded)
        eos_id = getattr(tokenizer, "eos_token_id", None)
        if eos_id is not None and (not ids or ids[-1] != eos_id):
            ids.append(eos_id)
        max_length = getattr(tokenizer, "model_max_length", _MAX_INPUT_TOKENS)
        if not isinstance(max_length, int) or max_length > 100_000:
            max_length = _MAX_INPUT_TOKENS
        max_length = min(max_length, _MAX_INPUT_TOKENS)
        if len(ids) > max_length:
            raise TranslationModelError(
                f"字幕文本超过本地模型输入上限（{max_length} tokens）",
                code="input_too_long",
            )
        return ids

    def _translate_batch(self, texts: list[str], tokenizer, translator) -> list[str]:
        encoded = [self._encode(text, tokenizer) for text in texts]
        return self._translate_encoded(encoded, tokenizer, translator)

    def _translate_encoded(self, encoded_texts: list[list[int]], tokenizer, translator) -> list[str]:
        try:
            source_tokens = [tokenizer.convert_ids_to_tokens(ids) for ids in encoded_texts]
            with self._inference_lock:
                results = translator.translate_batch(
                    source_tokens,
                    batch_type=_LOCAL_CT2_BATCH_TYPE,
                    max_batch_size=_LOCAL_CT2_MAX_BATCH_SIZE,
                    beam_size=_LOCAL_CT2_BEAM_SIZE,
                    max_decoding_length=_LOCAL_CT2_MAX_DECODING_LENGTH,
                )
            translated: list[str] = []
            for result in results:
                hypotheses = getattr(result, "hypotheses", None)
                if not hypotheses or not hypotheses[0]:
                    raise TranslationModelError("本地翻译模型返回了空结果", code="invalid_response")
                output_ids = tokenizer.convert_tokens_to_ids(hypotheses[0])
                text = tokenizer.decode(output_ids, skip_special_tokens=True).strip()
                if not text:
                    raise TranslationModelError("本地翻译模型返回了空译文", code="invalid_response")
                translated.append(text)
            if len(translated) != len(encoded_texts):
                raise TranslationModelError("本地翻译结果数量不匹配", code="invalid_response")
            return translated
        except TranslationModelError:
            raise
        except Exception as exc:
            raise TranslationModelError(f"本地模型推理失败：{exc}", code="local_model_failed") from exc


_manager: Optional[TranslationModelManager] = None
_manager_lock = threading.Lock()


def get_translation_model_manager() -> TranslationModelManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = TranslationModelManager()
        return _manager
