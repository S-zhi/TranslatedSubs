"""builtin 插件 session 的公共基类：会话样板只写一份。"""

from __future__ import annotations

from pathlib import Path

from src.plugin_system import PluginContext, PluginError
from src.plugin_system.contracts import PluginConfig


class BuiltinSessionBase:
    """builtin session 共享的 __init__/close/_error/_discard_new_output。

    子类用类属性 STAGE / PLUGIN_ID 区分自己；validate/run 各留各的。
    """

    STAGE: str = ""
    PLUGIN_ID: str = ""

    def __init__(self, config: PluginConfig, context: PluginContext) -> None:
        self._config = dict(config)
        self._context = context
        self._closed = False
        # whisper/translator 共用：运行时解析出的 secret，报错时用于脱敏。
        self._secret: str | None = None

    def close(self) -> None:
        self._closed = True

    def _require_open(self, message: str) -> None:
        """validate() 开场白：已关闭的 session 直接报 invalid_input。"""
        if self._closed:
            raise self._error("invalid_input", message)

    def _require_request_type(
        self, request: object, *types: type, message: str | None = None
    ) -> None:
        """validate() 第二段：request 必须是 types 之一（isinstance 检查）。

        message 不传时按类型名拼，如 "expected a TranscribeRequest"；
        文案无法从类型名推导的（如 render 的两种 request）由调用方传。
        """
        if not isinstance(request, types):
            if message is None:
                names = " or ".join(cls.__name__ for cls in types)
                article = "an" if names[0].lower() in "aeiou" else "a"
                message = f"expected {article} {names}"
            raise self._error("invalid_input", message)

    def _discard_new_output(self, output_path: Path, output_existed: bool) -> None:
        if not output_existed:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _error(
        self, code: str, message: str, cause: Exception | None = None
    ) -> PluginError:
        return PluginError(code, self.STAGE, self.PLUGIN_ID, message, cause)
