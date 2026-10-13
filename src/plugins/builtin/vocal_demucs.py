"""Built-in CPU Demucs separation plugin."""

from __future__ import annotations

from pathlib import Path

from src.core import vocal_separator
from src.plugin_system import (
    PluginCancelled,
    PluginContext,
    PluginError,
    PluginManifest,
    VocalSeparateRequest,
    VocalSeparateResult,
)
from src.plugin_system.contracts import PluginConfig


PLUGIN_ID = "builtin.vocal_demucs"
_ALLOWED_CONFIG_KEYS = frozenset({"backend", "model", "threads", "command", "timeout_seconds"})


class DemucsFactory:
    stage = "vocal_separate"
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        stage="vocal_separate",
        implementation_version="1",
        operations=frozenset({"vocal_separate"}),
    )

    def create_session(self, config: PluginConfig, context: PluginContext):
        return DemucsSession(config, context)


class DemucsSession:
    def __init__(self, config: PluginConfig, context: PluginContext) -> None:
        self._config = dict(config)
        self._context = context
        self._closed = False

    def validate(self, request: VocalSeparateRequest) -> None:
        if self._closed:
            raise self._error("invalid_input", "vocal separation session is closed")
        if type(request) is not VocalSeparateRequest:
            raise self._error("invalid_input", "expected a VocalSeparateRequest")
        if (
            request.stage != "vocal_separate"
            or request.operation != "vocal_separate"
            or request.purpose not in {"separate.asr", "separate.dubbing"}
            or self._context.purpose != request.purpose
        ):
            raise self._error("invalid_input", "vocal separation invocation identity is invalid")
        if not request.audio_path.is_file():
            raise self._error("invalid_input", "vocal separation input is missing")
        if set(self._config) != _ALLOWED_CONFIG_KEYS:
            raise self._error("invalid_input", "vocal separation config is incomplete or unsupported")
        if self._config.get("backend") != "demucs":
            raise self._error("invalid_input", "unsupported vocal separation backend")
        if not isinstance(self._config.get("model"), str) or not self._config["model"].strip():
            raise self._error("invalid_input", "vocal separation model is invalid")
        for field in ("threads", "timeout_seconds"):
            value = self._config.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise self._error("invalid_input", f"vocal separation {field} is invalid")
        if not isinstance(self._config.get("command"), str) or not self._config["command"].strip():
            raise self._error("invalid_input", "vocal separation command is invalid")

        task_dir = self._context.task_dir.resolve()
        if request.purpose == "separate.asr":
            if request.vocal_path is None or request.background_path is not None:
                raise self._error("invalid_input", "ASR separation requires only the vocal output")
            if request.vocal_path.resolve() != (task_dir / "vocal.wav").resolve():
                raise self._error("invalid_input", "ASR vocal output path is invalid")
        else:
            if request.background_path is None:
                raise self._error("invalid_input", "dubbing separation requires the background output")
            if request.background_path.resolve() != (task_dir / "background.wav").resolve():
                raise self._error("invalid_input", "dubbing background output path is invalid")
            if request.vocal_path is not None and request.vocal_path.resolve() != (
                task_dir / "vocal_stem.wav"
            ).resolve():
                raise self._error("invalid_input", "dubbing vocal stem path is invalid")

    def run(self, request: VocalSeparateRequest) -> VocalSeparateResult:
        self.validate(request)
        self._context.check_cancelled()
        try:
            result = vocal_separator.separate_stems(
                request.audio_path,
                self._context.task_id,
                purpose=request.purpose,
                vocal_path=request.vocal_path,
                background_path=request.background_path,
                backend=self._config["backend"],
                model=self._config["model"],
                threads=self._config["threads"],
                command=self._config["command"],
                timeout_seconds=self._config["timeout_seconds"],
                invocation_id=self._context.invocation_id,
                task_dir=self._context.task_dir,
                cancel_check=self._context.check_cancelled,
                register_process=self._context.register_process,
                unregister_process=self._context.unregister_process,
            )
        except PluginCancelled:
            raise
        except vocal_separator.VocalSeparationCancelledError as exc:
            raise PluginCancelled("vocal separation cancelled") from exc
        except Exception as exc:
            self._context.check_cancelled()
            if self._caused_by_missing_dependency(exc):
                raise self._error(
                    "dependency_missing",
                    "Demucs dependency is missing; install demucs and verify the configured Python command",
                    exc,
                ) from exc
            raise self._error("execution_error", "vocal separation failed", exc) from exc

        self._context.check_cancelled()
        vocal_matches = (
            result.vocal_path is None and request.vocal_path is None
        ) or (
            result.vocal_path is not None
            and request.vocal_path is not None
            and result.vocal_path.resolve() == request.vocal_path.resolve()
        )
        background_matches = (
            result.background_path is None and request.background_path is None
        ) or (
            result.background_path is not None
            and request.background_path is not None
            and result.background_path.resolve() == request.background_path.resolve()
        )
        if not vocal_matches or not background_matches:
            raise self._error("invalid_result", "vocal separation returned unexpected output paths")
        for path in (result.vocal_path, result.background_path):
            if path is not None and (not path.is_file() or path.stat().st_size <= 0):
                raise self._error("invalid_result", "vocal separation output is missing or empty")
        return VocalSeparateResult(
            vocal_path=result.vocal_path,
            background_path=result.background_path,
        )

    def close(self) -> None:
        self._closed = True

    def _error(self, code: str, message: str, cause: Exception | None = None) -> PluginError:
        return PluginError(code, "vocal_separate", PLUGIN_ID, message, cause)

    @staticmethod
    def _caused_by_missing_dependency(error: BaseException) -> bool:
        seen: set[int] = set()
        current: BaseException | None = error
        while current is not None and id(current) not in seen:
            if isinstance(current, (FileNotFoundError, ImportError)):
                return True
            seen.add(id(current))
            current = current.__cause__ or current.__context__
        return False


factory = DemucsFactory()
