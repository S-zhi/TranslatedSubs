"""Desktop HTTP adapters. Business services are imported only when called."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response

from src.desktop.runtime import desktop_version


logger = logging.getLogger(__name__)


async def _invoke(function, *args, **kwargs):
    result = await asyncio.to_thread(function, *args, **kwargs)
    return await result if inspect.isawaitable(result) else result


async def require_desktop_session(request: Request) -> None:
    """Only the outer authentication boundary may satisfy this dependency."""
    if not getattr(request.state, "desktop_authenticated", False):
        raise HTTPException(status_code=401, detail="Desktop session required")


def _prepare_models():
    from src.desktop.models import get_desktop_model_manager

    return get_desktop_model_manager().start_prepare()


async def start_desktop_preparation() -> None:
    try:
        await _invoke(_prepare_models)
    except Exception:
        logger.exception("Desktop model preparation could not start")


async def stop_desktop_tasks(app) -> None:
    """Serialize HTTP exit and ASGI shutdown so task cancellation runs once."""
    async with app.state.desktop_shutdown_lock:
        if app.state.desktop_tasks_stopped:
            return
        app.state.desktop_accepting_tasks = False
        # An upload admitted before shutdown may still be committing/enqueuing.
        # Cancel only after those handlers finish, so no late job escapes cleanup.
        await app.state.desktop_mutations_drained.wait()
        from src.desktop.lifecycle import shutdown_desktop_tasks

        await _invoke(shutdown_desktop_tasks)
        app.state.desktop_tasks_stopped = True


def create_desktop_router(web_dir: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/desktop/status")
    async def desktop_status():
        from src.desktop.lifecycle import active_task_count
        from src.desktop.models import desktop_task_defaults, get_desktop_model_manager

        models = await _invoke(get_desktop_model_manager().status)
        return {
            "version": desktop_version(),
            "platform": "macos-arm64",
            "models": models,
            "active_tasks": await _invoke(active_task_count),
            "defaults": desktop_task_defaults(),
            "ready": models["ready"],
        }

    @router.post("/api/desktop/models/prepare")
    @router.post("/api/desktop/models/retry")
    async def prepare_models():
        return await _invoke(_prepare_models)

    @router.get("/api/desktop/updates")
    async def desktop_updates(force: bool = False):
        from src.desktop.updates import get_update_service

        return await _invoke(get_update_service().check, force=force)

    @router.get("/api/desktop/tasks/active")
    async def desktop_active_tasks():
        from src.desktop.lifecycle import active_task_count

        return {"count": await _invoke(active_task_count)}

    @router.post("/api/desktop/shutdown")
    async def desktop_shutdown(request: Request):
        app = request.app
        request_shutdown = getattr(app.state, "desktop_request_shutdown", None)
        if request_shutdown is None:
            raise HTTPException(status_code=503, detail="Desktop process owner is unavailable")
        app.state.desktop_accepting_tasks = False

        async def finish_shutdown():
            try:
                await stop_desktop_tasks(app)
            finally:
                request_shutdown()

        return JSONResponse(
            {"ok": True, "shutting_down": True},
            background=BackgroundTask(finish_shutdown),
        )

    @router.get("/config.js", include_in_schema=False)
    async def desktop_config():
        config = (web_dir / "config.js").read_text(encoding="utf-8")
        desktop = json.dumps({
            "enabled": True,
            "version": desktop_version(),
        })
        override = (
            "\n// Native desktop requests always stay on the authenticated origin.\n"
            f"window.SUBTRANS_DESKTOP = {desktop};\n"
            "try { localStorage.removeItem('SUBTRANS_API_BASE_URL'); } catch (_) {}\n"
            "window.APP_CONFIG.API_BASE_URL = window.location.origin;\n"
            "window.APP_CONFIG.DRIVE_API_BASE_URL = window.location.origin;\n"
            "window.APP_CONFIG.USE_MOCK = false;\n"
        )
        return Response(
            config + override, media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    return router
