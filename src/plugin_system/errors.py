"""Stable plugin failures and cancellation control flow."""

from __future__ import annotations

import re
from typing import Literal, TypeAlias

from src.plugin_system.contracts import StageId


PluginErrorCode: TypeAlias = Literal[
    "invalid_input",
    "plugin_unavailable",
    "version_mismatch",
    "credential_missing",
    "dependency_missing",
    "execution_error",
    "invalid_result",
    "cleanup_error",
]

_SENSITIVE_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[^\s,;]+"), "Bearer [REDACTED]"),
    (
        re.compile(
            r"(?i)(password|passwd|api[_-]?key|token|authorization|cookie)"
            r"(\s*[=:]\s*)[^\s,;&]+"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@"), r"\1[REDACTED]@"),
    (re.compile(r"(?i)([?&](?:token|api[_-]?key|key|password|auth)=)[^&#\s]+"), r"\1[REDACTED]"),
)


def sanitize_message(message: object) -> str:
    text = str(message)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class PluginCancelled(Exception):
    """Cooperative cancellation signal; deliberately not a PluginError."""


class PluginError(Exception):
    """Sanitized, stable failure metadata for a plugin invocation."""

    def __init__(
        self,
        code: PluginErrorCode | str,
        stage: StageId,
        plugin_id: str,
        message: str,
        cause: Exception | None = None,
    ) -> None:
        self.code = code
        self.stage = stage
        self.plugin_id = plugin_id
        self.message = sanitize_message(message)
        self.cause = cause
        super().__init__(self.message)
        if cause is not None:
            self.__cause__ = cause

    def to_dict(self) -> dict[str, str]:
        """Return only safe public metadata; never serialize the cause."""
        return {
            "code": str(self.code),
            "stage": self.stage,
            "plugin_id": self.plugin_id,
            "message": self.message,
        }
