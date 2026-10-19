"""Same-origin trial API. Run separately from src.handler.app."""

from __future__ import annotations

import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Callable

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .config import LauncherConfig
from .provider import InstaBoxClient
from .service import MESSAGES, TrialService
from .store import ProcessLock, ReservationDenied, TrialStore


ERRORS = {
    "capacity": (429, "体验名额暂时已满，请稍后再试。"),
    "budget": (429, "本时段体验名额已用完，请稍后再试。"),
    "unavailable": (503, "在线体验暂未配置，请使用自行部署入口。"),
    "session_required": (409, "请先刷新体验状态，再开始试用。"),
    "origin": (403, "请从产品网站发起试用。"),
    "invalid_request": (400, "试用请求格式不正确，请刷新后重试。"),
}


def _error(code: str) -> HTTPException:
    status, message = ERRORS[code]
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def create_app(config: LauncherConfig | None = None, *,
               transport: httpx.AsyncBaseTransport | None = None,
               clock: Callable[[], float] = time.time,
               run_workers: bool = True) -> FastAPI:
    config = config or LauncherConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = None
        if not config.configured:
            yield
            return
        lock = ProcessLock(config.db_path.with_suffix(".lock"))
        lock.acquire()
        provider = None
        service = None
        try:
            store = TrialStore(config.db_path, config.launcher_id)
            provider = InstaBoxClient(config.api_key, transport=transport)
            service = TrialService(config, store, provider, clock=clock)
            app.state.service = service
            if run_workers:
                await service.start()
            yield
        finally:
            if service:
                await service.close()
            if provider:
                await provider.close()
            lock.release()

    app = FastAPI(title="TranslatedSubs trial launcher", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = None

    @app.middleware("http")
    async def private_response(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse(
                {"detail": {"code": "unavailable", "message": "体验服务暂时不可用，请稍后重试。"}},
                status_code=503,
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Vary"] = "Cookie, Origin"
        return response

    def owner(request: Request) -> str | None:
        cookie = request.cookies.get(config.cookie_name, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie):
            return None
        return config.digest("session-owner", cookie)

    async def mutation(request: Request) -> str:
        if not config.configured:
            raise _error("unavailable")
        if request.headers.get("origin") != config.origin:
            raise _error("origin")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise _error("invalid_request")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024:
                raise _error("invalid_request")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise _error("invalid_request") from None
        if payload != {}:
            raise _error("invalid_request")
        key = owner(request)
        if not key:
            raise _error("session_required")
        return key

    def public(trial: dict | None) -> dict:
        service = app.state.service
        if service:
            return service.public(trial)
        return {"configured": False, "status": "unavailable", "trialId": None,
                "expiresAt": None, "url": None, "message": MESSAGES["unavailable"]}

    @app.get("/api/trials/current")
    async def current(request: Request, response: Response):
        if request.headers.get("origin") not in {None, config.origin}:
            raise _error("origin")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise _error("origin")
        key = owner(request)
        if key is None:
            session = secrets.token_urlsafe(32)
            response.set_cookie(
                config.cookie_name, session, httponly=True, secure=config.secure_cookie,
                samesite="strict", path="/", max_age=86400,
            )
            key = config.digest("session-owner", session)
        service = app.state.service
        return public(service.store.current(key) if service else None)

    @app.post("/api/trials", status_code=202)
    async def launch(request: Request):
        key = await mutation(request)
        service = app.state.service
        if service is None:
            raise _error("unavailable")
        try:
            trial = service.reserve(key)
        except ReservationDenied as exc:
            raise _error(exc.code) from None
        if run_workers:
            service.tick()
        return public(trial)

    @app.delete("/api/trials/current", status_code=202)
    async def end(request: Request):
        key = await mutation(request)
        service = app.state.service
        if service is None:
            raise _error("unavailable")
        trial = service.store.current(key)
        if trial and trial["holds_capacity"]:
            trial = service.end(trial)
            if run_workers:
                service.tick()
        return public(trial)

    return app
