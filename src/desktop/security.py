"""Authentication boundary shared by every desktop API and static/media route."""

from __future__ import annotations

import hmac

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse


COOKIE_NAME = "subtrans_desktop_session"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class DesktopSecurityMiddleware:
    def __init__(self, app, *, token: str, state):
        if not token:
            raise ValueError("SUBTRANS_DESKTOP_SESSION_TOKEN is required")
        self.app = app
        self.token = token.encode("utf-8")
        self.state = state

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        server = scope.get("server") or (None, None)
        port = getattr(self.state, "desktop_port", None) or server[1]
        authority = f"127.0.0.1:{port}"
        status, detail = 0, ""
        if headers.getlist("host") != [authority]:
            status, detail = 400, "Invalid desktop Host"
        elif len(headers.getlist("x-api-token")) > 1:
            status, detail = 401, "Desktop session required"
        else:
            supplied = headers.get("x-api-token")
            if supplied is None:
                supplied = HTTPConnection(scope).cookies.get(COOKIE_NAME, "")
            if not hmac.compare_digest(supplied.encode("utf-8"), self.token):
                status, detail = 401, "Desktop session required"

        mutation = scope["type"] == "websocket" or scope.get("method") not in SAFE_METHODS
        if not status and mutation:
            origins = headers.getlist("origin")
            if (origins and origins != [f"http://{authority}"]) or headers.get(
                "sec-fetch-site"
            ) in {"cross-site", "same-site"}:
                status, detail = 403, "Cross-origin desktop mutation rejected"
            elif (
                not self.state.desktop_accepting_tasks
                and scope.get("path") != "/api/desktop/shutdown"
            ):
                status, detail = 503, "Desktop backend is shutting down"

        if status:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)
            return
        scope.setdefault("state", {})["desktop_authenticated"] = True
        admitted = mutation and scope.get("path") != "/api/desktop/shutdown"
        if admitted:
            self.state.desktop_inflight_mutations += 1
            self.state.desktop_mutations_drained.clear()
        try:
            await self.app(scope, receive, send)
        finally:
            if admitted:
                self.state.desktop_inflight_mutations -= 1
                if self.state.desktop_inflight_mutations == 0:
                    self.state.desktop_mutations_drained.set()
