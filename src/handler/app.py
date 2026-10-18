"""FastAPI 应用装配：创建 app、配置 CORS、挂载各业务路由 + 前端静态文件。

启动（推荐，同时启动 Google Drive sidecar）：
    ./scripts/start.sh
仅启动 FastAPI：
    uv run uvicorn src.handler.app:app --reload --port 8000
    API 文档：http://localhost:8000/docs
    前端页面：http://localhost:8000/
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import sys

if os.environ.get("SUBTRANS_DESKTOP") == "1":
    from src.desktop.runtime import configure_environment

    configure_environment()

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.config import settings
from src.config.config_repository import (
    ConfigUnavailable,
    initialize_config_document,
    start_config_subscription,
    stop_config_subscription,
)
from src.config.schema import BUSINESS_DEFAULTS
from src.config.redis_store import RevisionConflict


from src.handler import (
    audio_settings,
    business_settings,
    health,
    model_settings,
    srt,
    subtitle_editor,
    tasks,
    storage,
    translation_engines,
)

from src.handler.deps import get_probe_store, get_store

from src.service.retention_scheduler import start_retention_scheduler
from src.service.probe_batch import start_startup_probe, stop_startup_probe
from src.service.translation_model_manager import get_translation_model_manager

from src.service.runner import recover_interrupted_tasks, shutdown_executor
from src.store import (
    DOWNGRADE_REASON_DISK_FAILURE,
    DOWNGRADE_REASON_UNKNOWN,
    DOWNGRADE_REASON_USER_CLEANED,
    DOWNGRADE_REASON_VOLUME_MIGRATED,
)


logger = logging.getLogger(__name__)


class DynamicCORSMiddleware(CORSMiddleware):
    """Refresh allowed origins from the shared business snapshot per request."""
    def __init__(self, app, **kwargs):
        self._app = app
        self._cors_options = kwargs
        super().__init__(app, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            origins = list(settings.cors_allow_origins)
        except Exception:
            origins = []
        dynamic = CORSMiddleware(
            self._app,
            allow_origins=origins,
            **{key: value for key, value in self._cors_options.items() if key != "allow_origins"},
        )
        await dynamic(scope, receive, send)

def resolve_web_dir() -> Path:
    """PyInstaller places collected web assets directly below its bundle root."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root is not None:
        return Path(bundle_root) / "web"
    return Path(__file__).resolve().parents[2] / "web"


_WEB_DIR = resolve_web_dir()



def create_app(debug: bool | None = None) -> FastAPI:
    app = FastAPI(title="TranslatedSubs API", version="0.1.0")
    desktop_mode = os.environ.get("SUBTRANS_DESKTOP") == "1"
    if desktop_mode:
        from src.desktop.api import create_desktop_router, require_desktop_session
        from src.desktop.runtime import configure_environment
        from src.desktop.security import DesktopSecurityMiddleware
        from src.handler.deps import require_api_token

        configure_environment()
        app.state.desktop_accepting_tasks = True
        app.state.desktop_inflight_mutations = 0
        app.state.desktop_mutations_drained = asyncio.Event()
        app.state.desktop_mutations_drained.set()
        app.state.desktop_tasks_stopped = False
        app.state.desktop_shutdown_lock = asyncio.Lock()
        app.add_middleware(
            DesktopSecurityMiddleware,
            token=os.environ["SUBTRANS_DESKTOP_SESSION_TOKEN"], state=app.state,
        )
        app.dependency_overrides[require_api_token] = require_desktop_session
        app.include_router(create_desktop_router(_WEB_DIR))

    @app.exception_handler(ConfigUnavailable)
    async def config_unavailable_handler(request: Request, exc: ConfigUnavailable):
        return JSONResponse(status_code=503, content={"detail": "Redis 配置存储不可用"})

    @app.exception_handler(RevisionConflict)
    async def revision_conflict_handler(request: Request, exc: RevisionConflict):
        return JSONResponse(status_code=409, content={"detail": "配置版本冲突"})

    # 本机工作台：只允许配置中的前端来源访问 API。
    if not desktop_mode:
        app.add_middleware(
            DynamicCORSMiddleware,
            allow_origins=[],
            allow_methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-API-Token", "If-Match"],
        )

    # 按业务挂载路由，后续新增业务在此 include 即可
    app.include_router(tasks.router)
    app.include_router(srt.router)
    app.include_router(storage.router)
    app.include_router(subtitle_editor.router)
    app.include_router(health.router)
    app.include_router(translation_engines.router)
    app.include_router(model_settings.router)
    app.include_router(audio_settings.router)
    app.include_router(business_settings.router)

    # SPA 路由需要在静态目录挂载前显式回退到 index.html，否则直接打开
    # /tasks、/preview 等深链接时 StaticFiles 会按目录查找并返回 404。
    if _WEB_DIR.is_dir():
        spa_routes = (
            "/tasks", "/preview", "/editor", "/probe", "/storage",
            "/drive", "/settings", "/model-settings",
        )
        for route_path in spa_routes:
            app.add_api_route(
                route_path,
                lambda: FileResponse(_WEB_DIR / "index.html"),
                methods=["GET"],
                include_in_schema=False,
            )

    # 最后挂载前端静态文件（必须放在 API router 之后，否则会拦截 /api/*）。
    # html=True 让根路径直接返回 web/index.html，避免再开一个 http.server。
    if _WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
    else:
        logger.warning("前端目录不存在: %s，仅暴露 API", _WEB_DIR)

    @app.on_event("startup")
    def _scan_missing_terminal() -> None:
        """启动时校验终态资源，并恢复所有未完成任务。

        SUCCESS 任务的磁盘产物已不在时降级为 MISSING；PENDING 和处理中任务
        重新入队，由流水线根据已有中间产物断点续跑。两项操作都可重复执行。
        """
        try:
            initialize_config_document()
        except ConfigUnavailable:
            if desktop_mode:
                raise
            logger.exception("Redis shared configuration unavailable; skipping recovery startup")
            return

        store = get_store()
        downgraded = tasks.scan_missing_terminal(store, data_dir=settings.data_dir)
        if downgraded:
            counts = {
                reason: 0
                for reason in (
                    DOWNGRADE_REASON_USER_CLEANED,
                    DOWNGRADE_REASON_DISK_FAILURE,
                    DOWNGRADE_REASON_VOLUME_MIGRATED,
                    DOWNGRADE_REASON_UNKNOWN,
                )
            }
            for task_id in downgraded:
                rec = store.get(task_id)
                reason = (rec.downgrade_reason if rec else None) or DOWNGRADE_REASON_UNKNOWN
                counts[reason] = counts.get(reason, 0) + 1

            logger.info(
                "启动扫描：降级 %d 个任务为 MISSING，原因统计: %s",
                len(downgraded), counts,
            )
            abnormal = counts[DOWNGRADE_REASON_DISK_FAILURE] + counts[DOWNGRADE_REASON_VOLUME_MIGRATED]
            if abnormal:
                logger.warning("启动扫描检测到 %d 个磁盘故障或存储迁移任务: %s", abnormal, downgraded)

        if desktop_mode:
            from src.desktop.api import start_desktop_preparation
            from src.desktop.lifecycle import recover_desktop_tasks

            recover_desktop_tasks()
            app.state.desktop_prepare_task = asyncio.create_task(start_desktop_preparation())
            start_retention_scheduler()
            return

        # Local translation is optional at startup.  If a complete model and
        # its optional dependencies are already present, warm the cached
        # runtime in the background without blocking API startup or downloading.
        local_manager = get_translation_model_manager()
        if local_manager.start_warmup():
            logger.info("本地翻译模型已提交后台预热")

        recovered = recover_interrupted_tasks()
        if recovered:
            logger.warning("启动恢复：以下未完成任务已重新入队: %s", recovered)

        start_retention_scheduler()
        start_config_subscription()
        effective_debug = settings.debug if debug is None else bool(debug)
        start_startup_probe(
            get_probe_store(),
            enabled=settings.startup_probe_enabled,
            debug=effective_debug,
        )
        if effective_debug:
            logger.info("debug 模式：已跳过网站连通性检查")
    @app.on_event("shutdown")
    async def _shutdown_runner() -> None:
        """关闭应用时通知 runner 线程池退出。"""
        if desktop_mode:
            from src.desktop.api import stop_desktop_tasks
            from src.desktop.runtime import stop_desktop_retention

            try:
                await stop_desktop_tasks(app)
            finally:
                preparation = getattr(app.state, "desktop_prepare_task", None)
                if preparation is not None and not preparation.done():
                    preparation.cancel()
                    await asyncio.gather(preparation, return_exceptions=True)
                shutdown_executor(wait=False)
                await stop_desktop_retention()
            return
        stop_startup_probe()
        stop_config_subscription()
        get_translation_model_manager().cancel_warmup()
        shutdown_executor(wait=False)

    return app


app = create_app()


def create_debug_app() -> FastAPI:
    """Uvicorn factory used by the explicit ``--debug`` launcher."""
    return create_app(debug=True)
