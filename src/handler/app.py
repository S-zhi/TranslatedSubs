"""FastAPI 应用装配：创建 app、配置 CORS、挂载各业务路由 + 前端静态文件。

启动（推荐，同时启动 Google Drive sidecar）：
    ./scripts/start.sh
仅启动 FastAPI：
    uv run uvicorn src.handler.app:app --reload --port 8000
    API 文档：http://localhost:8000/docs
    前端页面：http://localhost:8000/
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.config import settings


from src.handler import (
    audio_settings,
    health,
    replicate,
    replicate_settings,
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

# 项目根目录下的 web 前端目录（向上两级即 src/handler/app.py -> src/handler -> 项目根）
_WEB_DIR = Path(__file__).resolve().parents[2] / "web"



def create_app() -> FastAPI:
    app = FastAPI(title="TranslatedSubs API", version="0.1.0")

    # 本机工作台：只允许配置中的前端来源访问 API。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins),
        allow_methods=["GET", "POST", "DELETE", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Token"],
    )

    # 按业务挂载路由，后续新增业务在此 include 即可
    app.include_router(tasks.router)
    app.include_router(srt.router)
    app.include_router(storage.router)
    app.include_router(subtitle_editor.router)
    app.include_router(health.router)
    app.include_router(replicate.router)
    app.include_router(replicate_settings.router)
    app.include_router(translation_engines.router)
    app.include_router(audio_settings.router)

    # SPA 路由需要在静态目录挂载前显式回退到 index.html，否则直接打开
    # /tasks、/preview 等深链接时 StaticFiles 会按目录查找并返回 404。
    if _WEB_DIR.is_dir():
        spa_routes = (
            "/tasks", "/preview", "/editor", "/probe", "/storage",
            "/drive", "/replicate", "/settings",
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
        start_startup_probe(get_probe_store(), enabled=settings.startup_probe_enabled)
    @app.on_event("shutdown")
    def _shutdown_runner() -> None:
        """关闭应用时通知 runner 线程池退出。"""
        stop_startup_probe()
        get_translation_model_manager().cancel_warmup()
        shutdown_executor(wait=False)

    return app


app = create_app()
