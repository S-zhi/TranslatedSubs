"""Validate only the selected task plugins before persisting a task."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

from src.plugin_system import PluginError

_OPERATIONS = {
    "download": "download",
    "extract.asr": "audio_extract",
    "extract.dubbing": "audio_extract",
    "separate.asr": "vocal_separate",
    "separate.dubbing": "vocal_separate",
    "transcribe": "transcribe",
    "translate": "translate",
    "tts": "tts",
    "render.subtitle": "subtitle",
    "render.dub_mux": "dub_mux",
}


def preflight_plugin_plan(
    plan: Mapping[str, Any],
    *,
    source_lang: str = "",
    target_lang: str = "",
    plugin_manager: Any = None,
) -> None:
    """Check pinned identities and factory dependencies without a session/media."""
    slots = plan["slots"]
    if not slots:
        return
    if plugin_manager is None:
        # Resolve at call time so host integrations can inject their manager.
        from src.plugins import get_plugin_manager

        plugin_manager = get_plugin_manager()
    preflight = getattr(plugin_manager, "preflight", None)
    if not callable(preflight):
        raise HTTPException(
            status_code=422,
            detail={"code": "plugin_unavailable", "message": "插件预检接口不可用"},
        )
    for purpose, slot in slots.items():
        try:
            preflight(
                slot["plugin_id"], slot["implementation_version"],
                operation=_OPERATIONS[purpose], purpose=purpose,
                config=slot["config"], source_lang=source_lang,
                target_lang=target_lang,
            )
        except PluginError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": str(exc.code), "message": exc.message},
            ) from exc
