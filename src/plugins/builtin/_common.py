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

    def close(self) -> None:
        self._closed = True

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
