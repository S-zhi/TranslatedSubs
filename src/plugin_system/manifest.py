"""Plugin identity and registration metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from src.plugin_system.contracts import OperationId, PluginFactory, StageId

PROTOCOL_VERSION = 1

REQUIRED_OPERATIONS: Mapping[StageId, frozenset[OperationId]] = {
    "download": frozenset({"download"}),
    "audio_extract": frozenset({"audio_extract"}),
    "vocal_separate": frozenset({"vocal_separate"}),
    "transcribe": frozenset({"transcribe"}),
    "translate": frozenset({"translate"}),
    "tts": frozenset({"tts"}),
    "render": frozenset({"subtitle", "dub_mux"}),
}


@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    stage: StageId
    protocol_version: int = PROTOCOL_VERSION
    implementation_version: str = "1"
    operations: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.plugin_id, str) or not self.plugin_id.strip():
            raise ValueError("plugin_id must be a non-empty string")
        if (
            not isinstance(self.implementation_version, str)
            or not self.implementation_version.strip()
        ):
            raise ValueError("implementation_version must be a non-empty string")
        if isinstance(self.protocol_version, bool) or not isinstance(self.protocol_version, int):
            raise ValueError("protocol_version must be an integer")
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError(f"unsupported plugin protocol version: {self.protocol_version}")
        if self.stage not in REQUIRED_OPERATIONS:
            raise ValueError(f"unsupported plugin stage: {self.stage}")
        if (
            not isinstance(self.operations, frozenset)
            or not isinstance(self.capabilities, frozenset)
        ):
            raise ValueError("operations and capabilities must be frozensets")
        required = REQUIRED_OPERATIONS[self.stage]
        if not required.issubset(self.operations):
            missing = ", ".join(sorted(required - self.operations))
            raise ValueError(f"manifest missing required operation(s): {missing}")
        unexpected = self.operations - required
        if unexpected:
            raise ValueError(
                f"unsupported operation(s) for stage {self.stage}: {sorted(unexpected)}"
            )
        if "probe" in self.capabilities and self.stage != "download":
            raise ValueError("probe capability is only supported by download plugins")
        if any(
            not isinstance(capability, str) or not capability
            for capability in self.capabilities
        ):
            raise ValueError("capabilities must contain non-empty strings")


def validate_registration(
    manifest: PluginManifest,
    factory: PluginFactory,
    registered_plugin_ids: set[str] | frozenset[str],
) -> None:
    """Reject inconsistent registrations without constructing a session."""
    if manifest.plugin_id in registered_plugin_ids:
        raise ValueError(f"plugin_id already registered: {manifest.plugin_id}")
    if factory.stage != manifest.stage:
        raise ValueError(
            f"factory stage {factory.stage!r} does not match manifest stage {manifest.stage!r}"
        )
    if manifest.protocol_version != PROTOCOL_VERSION:
        raise ValueError(f"unsupported plugin protocol version: {manifest.protocol_version}")
