"""Desktop boundary tests; no real server, model download, or cloud request."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.desktop import runtime
from src.desktop.api import create_desktop_router, require_desktop_session
from src.desktop.security import COOKIE_NAME, DesktopSecurityMiddleware


ORIGIN = "http://127.0.0.1:49173"
TOKEN = "test-native-session-secret"
AUTH = {"X-API-Token": TOKEN}
WEB_DIR = Path(__file__).resolve().parents[1] / "web"


@pytest.fixture
def desktop_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBTRANS_DESKTOP", "1")
    monkeypatch.setenv("SUBTRANS_DESKTOP_HOME", str(tmp_path / "app-data"))
    monkeypatch.setenv("SUBTRANS_DESKTOP_SESSION_TOKEN", TOKEN)
    monkeypatch.setenv("SUBTRANS_DESKTOP_VERSION", "1.2.3")


@pytest.fixture
def services(monkeypatch):
    calls = []
    snapshot = {"state": "preparing", "ready": False, "models": [], "error": None}
    defaults = {"sourceLang": "en", "targetLang": "zh-CN"}

    def prepare():
        calls.append("prepare")
        return dict(snapshot)

    manager = SimpleNamespace(status=lambda: dict(snapshot), start_prepare=prepare)
    models = ModuleType("src.desktop.models")
    models.get_desktop_model_manager = lambda: manager
    models.desktop_task_defaults = lambda: dict(defaults)
    lifecycle = ModuleType("src.desktop.lifecycle")
    lifecycle.active_task_count = lambda: 2
    lifecycle.recover_desktop_tasks = lambda: calls.append("recover")
    lifecycle.shutdown_desktop_tasks = lambda: calls.append("cancel")
    updates = ModuleType("src.desktop.updates")

    def check(force=False):
        calls.append(("updates", force))
        return {"state": "disabled", "current_version": "1.2.3", "error": None}

    updates.get_update_service = lambda: SimpleNamespace(check=check)
    for module in (models, lifecycle, updates):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return SimpleNamespace(calls=calls, snapshot=snapshot, manager=manager, lifecycle=lifecycle)


def boundary_app():
    app = FastAPI()
    app.state.desktop_port = 49173
    app.state.desktop_accepting_tasks = True
    app.state.desktop_inflight_mutations = 0
    app.state.desktop_mutations_drained = asyncio.Event()
    app.state.desktop_mutations_drained.set()
    app.state.desktop_tasks_stopped = False
    app.state.desktop_shutdown_lock = asyncio.Lock()
    app.add_middleware(DesktopSecurityMiddleware, token=TOKEN, state=app.state)

    @app.api_route(
        "/resource", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"],
        dependencies=[Depends(require_desktop_session)],
    )
    async def resource():
        return {"ok": True}

    app.include_router(create_desktop_router(WEB_DIR))
    return app


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"])
def test_every_method_requires_native_session(method):
    with TestClient(boundary_app(), base_url=ORIGIN) as client:
        assert client.request(method, "/resource").status_code == 401
        assert client.request(method, "/resource", headers=AUTH).status_code == 200
        client.cookies.set(COOKIE_NAME, TOKEN)
        assert client.request(method, "/resource").status_code == 200
        assert client.request(method, "/resource", headers={"X-API-Token": "wrong"}).status_code == 401


@pytest.mark.parametrize("suffix,headers", [
    ("?token=" + TOKEN, {}),
    ("?api_token=" + TOKEN, {}),
    ("", {"Authorization": "Bearer " + TOKEN}),
    ("", {"Origin": ORIGIN}),
    ("", {"Cookie": "api_token=" + TOKEN}),
])
def test_server_credentials_and_bare_origin_do_not_bypass_desktop(suffix, headers):
    with TestClient(boundary_app(), base_url=ORIGIN) as client:
        assert client.get("/resource" + suffix, headers=headers).status_code == 401


@pytest.mark.parametrize("host", [
    "evil.example:49173", "localhost:49173", "127.0.0.1:8000", "127.0.0.1",
    "127.0.0.1:49173.evil.example", "[::1]:49173", "127.0.0.1:49173@evil.example",
])
def test_host_must_match_bound_loopback_port(host):
    with TestClient(boundary_app(), base_url=ORIGIN) as client:
        assert client.get("/resource", headers={**AUTH, "Host": host}).status_code == 400


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost:49173", "http://127.0.0.1:8000"])
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_cross_origin_mutations_rejected_even_with_valid_token(origin, method):
    with TestClient(boundary_app(), base_url=ORIGIN) as client:
        assert client.request(method, "/resource", headers={**AUTH, "Origin": origin}).status_code == 403
        assert client.request(method, "/resource", headers={**AUTH, "Origin": ORIGIN}).status_code == 200


def test_fetch_metadata_and_duplicate_headers_cannot_bypass_boundary():
    with TestClient(boundary_app(), base_url=ORIGIN) as client:
        for site in ("same-site", "cross-site"):
            response = client.post("/resource", headers={**AUTH, "Sec-Fetch-Site": site})
            assert response.status_code == 403
        assert client.get("/resource", headers=[
            ("Host", "127.0.0.1:49173"), ("Host", "evil.example"), ("X-API-Token", TOKEN),
        ]).status_code == 400
        assert client.get("/resource", headers=[
            ("X-API-Token", TOKEN), ("X-API-Token", "wrong"),
        ]).status_code == 401
        assert client.post("/resource", headers=[
            ("X-API-Token", TOKEN), ("Origin", ORIGIN), ("Origin", "https://evil.example"),
        ]).status_code == 403


def test_unwrapped_desktop_dependency_is_fail_closed():
    app = FastAPI()
    app.add_api_route("/", lambda: {"ok": True}, dependencies=[Depends(require_desktop_session)])
    assert TestClient(app).get("/", headers=AUTH).status_code == 401


def test_desktop_endpoints_follow_service_contract(desktop_env, services):
    with TestClient(boundary_app(), base_url=ORIGIN, headers=AUTH) as client:
        status = client.get("/api/desktop/status").json()
        assert status == {
            "version": "1.2.3", "platform": "macos-arm64", "models": services.snapshot,
            "active_tasks": 2, "defaults": {"sourceLang": "en", "targetLang": "zh-CN"}, "ready": False,
        }
        for action in ("prepare", "retry"):
            assert client.post(f"/api/desktop/models/{action}").json() == services.snapshot
        assert client.get("/api/desktop/tasks/active").json() == {"count": 2}
        assert client.get("/api/desktop/updates").json()["state"] == "disabled"
        assert client.get("/api/desktop/updates?force=true").status_code == 200
        assert services.calls == ["prepare", "prepare", ("updates", False), ("updates", True)]
        services.snapshot.update(ready=True, state="ready")
        assert client.get("/api/desktop/status").json()["ready"] is True


def test_shutdown_closes_admission_and_runs_only_after_response(services):
    app = boundary_app()
    events = []
    app.state.desktop_request_shutdown = lambda: events.append("server-stop")
    services.lifecycle.shutdown_desktop_tasks = lambda: events.append("tasks-stop")

    async def exercise():
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "scheme": "http", "method": "POST", "path": "/api/desktop/shutdown",
            "raw_path": b"/api/desktop/shutdown", "query_string": b"",
            "root_path": "", "server": ("127.0.0.1", 49173), "client": ("127.0.0.1", 1),
            "headers": [(b"host", b"127.0.0.1:49173"), (b"x-api-token", TOKEN.encode())],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                assert not app.state.desktop_accepting_tasks
                assert not events
                events.append("response-complete")

        await app(scope, receive, send)

    asyncio.run(exercise())
    assert events == ["response-complete", "tasks-stop", "server-stop"]
    with TestClient(app, base_url=ORIGIN, headers=AUTH) as client:
        assert client.post("/resource").status_code == 503
        assert client.post("/api/desktop/models/prepare").status_code == 503
        assert client.get("/api/desktop/tasks/active").status_code == 200
        assert client.post("/api/desktop/shutdown").status_code == 200
    assert events.count("tasks-stop") == 1


def test_shutdown_always_notifies_process_owner_on_cancellation_failure(services):
    app = boundary_app()
    stopped = []
    app.state.desktop_request_shutdown = lambda: stopped.append(True)

    def fail():
        raise RuntimeError("cancellation failed")

    services.lifecycle.shutdown_desktop_tasks = fail
    with TestClient(app, base_url=ORIGIN, headers=AUTH, raise_server_exceptions=False) as client:
        assert client.post("/api/desktop/shutdown").json()["shutting_down"] is True
    assert stopped == [True]
    assert app.state.desktop_accepting_tasks is False


def test_shutdown_waits_for_already_admitted_mutations(services):
    from src.desktop.api import stop_desktop_tasks

    async def exercise():
        app = boundary_app()
        app.state.desktop_mutations_drained.clear()
        stopping = asyncio.create_task(stop_desktop_tasks(app))
        await asyncio.sleep(0)
        assert not app.state.desktop_accepting_tasks
        assert "cancel" not in services.calls
        app.state.desktop_mutations_drained.set()
        await stopping
        assert services.calls == ["cancel"]

    asyncio.run(exercise())


def test_shutdown_without_process_owner_returns_actionable_error(services):
    app = boundary_app()
    with TestClient(app, base_url=ORIGIN, headers=AUTH) as client:
        assert client.post("/api/desktop/shutdown").status_code == 503
    assert app.state.desktop_accepting_tasks is True
    assert "cancel" not in services.calls


def test_config_preserves_existing_settings_but_removes_external_origin(desktop_env):
    with TestClient(boundary_app(), base_url=ORIGIN, headers=AUTH) as client:
        response = client.get("/config.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert TOKEN not in response.text
    assert response.text.startswith((WEB_DIR / "config.js").read_text())
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the generated config.js regression check")
    script = """
const vm = require('node:vm');
const source = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const stored = new Map([['SUBTRANS_API_BASE_URL', 'https://old-external.example']]);
const context = {
  window: { location: { origin: 'http://127.0.0.1:49173', hostname: '127.0.0.1', port: '49173', protocol: 'http:' } },
  localStorage: { getItem: key => stored.get(key), removeItem: key => stored.delete(key) },
};
vm.runInNewContext(source, context);
process.stdout.write(JSON.stringify({ config: context.window.APP_CONFIG, desktop: context.window.SUBTRANS_DESKTOP, stored: stored.size }));
"""
    result = subprocess.run([node, "-e", script], input=json.dumps(response.text), text=True, capture_output=True, check=True)
    parsed = json.loads(result.stdout)
    assert parsed["desktop"] == {"enabled": True, "version": "1.2.3"}
    assert parsed["config"]["API_BASE_URL"] == ORIGIN
    assert parsed["config"]["DRIVE_API_BASE_URL"] == ORIGIN
    assert parsed["config"]["USE_MOCK"] is False
    assert parsed["config"]["TARGET_LANGUAGES"]
    assert parsed["stored"] == 0


@pytest.mark.parametrize("path", ["/", "/tasks", "/config.js", "/js/api.js", "/api/health", "/api/health/ready", "/api/tasks", "/api/tasks/example/video", "/openapi.json"])
def test_real_app_protects_static_api_and_media(desktop_env, path):
    module = importlib.import_module("src.handler.app")
    app = module.create_app()
    # No lifespan here: these checks must never start real models or recover tasks.
    client = TestClient(app, base_url=ORIGIN)
    assert client.get(path).status_code == 401


def test_cookie_replaces_business_auth_without_config_lookup(desktop_env, monkeypatch):
    module = importlib.import_module("src.handler.app")
    from src.handler import deps

    class UnavailableSettings:
        @property
        def api_token(self):
            raise AssertionError("Desktop auth must not read server credentials")

    monkeypatch.setattr(deps, "settings", UnavailableSettings())
    app = module.create_app()

    @app.get("/authenticated-test", dependencies=[Depends(deps.require_api_token)])
    def authenticated_test():
        return {"ok": True}

    # Insert before the static catch-all, exactly as business routers are mounted.
    app.router.routes.insert(0, app.router.routes.pop())
    client = TestClient(app, base_url=ORIGIN)
    assert client.get("/authenticated-test").status_code == 401
    client.cookies.set(COOKIE_NAME, TOKEN)
    assert client.get("/authenticated-test").json() == {"ok": True}
    for path in ("/", "/config.js", "/js/api.js", "/api/health"):
        assert client.get(path).status_code == 200


def test_server_mode_has_original_auth_and_config(monkeypatch):
    monkeypatch.delenv("SUBTRANS_DESKTOP", raising=False)
    monkeypatch.delenv("SUBTRANS_DESKTOP_SESSION_TOKEN", raising=False)
    module = importlib.import_module("src.handler.app")
    from src.handler.deps import require_api_token

    app = module.create_app()
    assert require_api_token not in app.dependency_overrides
    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/desktop/status").status_code == 404
    assert client.get("/config.js").text == (WEB_DIR / "config.js").read_text()
    assert not any(item.cls is DesktopSecurityMiddleware for item in app.user_middleware)


def test_startup_uses_desktop_recovery_and_prepare_does_not_block_health(desktop_env, services, monkeypatch):
    module = importlib.import_module("src.handler.app")
    entered, release = threading.Event(), threading.Event()

    def prepare():
        entered.set()
        release.wait(timeout=5)
        services.calls.append("prepare")

    services.manager.start_prepare = prepare
    monkeypatch.setattr(module, "initialize_config_document", lambda: None)
    monkeypatch.setattr(module, "get_store", lambda: object())
    monkeypatch.setattr(module.tasks, "scan_missing_terminal", lambda *args, **kwargs: [])
    monkeypatch.setattr(module, "start_retention_scheduler", lambda: None)
    monkeypatch.setattr(module, "shutdown_executor", lambda **kwargs: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("Desktop must not invoke server recovery, warmup, or probing")

    for name in ("recover_interrupted_tasks", "get_translation_model_manager", "start_startup_probe", "start_config_subscription"):
        monkeypatch.setattr(module, name, forbidden)
    app = module.create_app()
    try:
        with TestClient(app, base_url=ORIGIN, headers=AUTH) as client:
            assert entered.wait(timeout=2)
            assert not release.is_set()
            assert client.get("/api/health").json() == {"ok": True}
            assert services.calls == ["recover"]
            release.set()
    finally:
        release.set()
    assert services.calls.count("recover") == 1
    assert services.calls.count("cancel") == 1


def test_frozen_web_resolution_uses_bundle_root(monkeypatch, tmp_path):
    module = importlib.import_module("src.handler.app")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert module.resolve_web_dir() == tmp_path / "web"


@pytest.mark.parametrize("token", [None, "", "with spaces", "bad;cookie", "bad\nvalue"])
def test_missing_or_invalid_token_fails_before_business_import(monkeypatch, token, tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("SUBTRANS_DESKTOP")}
    if token is not None:
        env["SUBTRANS_DESKTOP_SESSION_TOKEN"] = token
    result = subprocess.run(
        [sys.executable, "-m", "src.desktop", "--session-file", str(tmp_path / "session.json")],
        env=env, text=True, capture_output=True,
    )
    assert result.returncode == 2
    assert "SUBTRANS_DESKTOP_SESSION_TOKEN is required" in result.stderr
    assert "Traceback" not in result.stderr
    if token:
        assert token not in result.stderr
    assert not (tmp_path / "session.json").exists()


def test_desktop_app_refuses_missing_token(monkeypatch):
    module = importlib.import_module("src.handler.app")
    monkeypatch.setenv("SUBTRANS_DESKTOP", "1")
    monkeypatch.delenv("SUBTRANS_DESKTOP_SESSION_TOKEN", raising=False)
    with pytest.raises(ValueError, match="SESSION_TOKEN"):
        module.create_app()


def test_desktop_modules_are_import_safe():
    script = """
import sys
import src.desktop
import src.desktop.__main__
import src.desktop.runtime
import src.desktop.api
import src.desktop.security
assert 'src.config' not in sys.modules
assert 'src.handler' not in sys.modules
assert 'src.desktop.models' not in sys.modules
assert 'src.desktop.lifecycle' not in sys.modules
assert 'src.desktop.updates' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)


def test_environment_setup_and_relative_path_rejection(desktop_env, monkeypatch, tmp_path):
    monkeypatch.delenv("SUBTRANS_DESKTOP", raising=False)
    assert runtime.configure_environment() == tmp_path / "app-data"
    assert os.environ["SUBTRANS_DESKTOP"] == "1"
    monkeypatch.setenv("SUBTRANS_DESKTOP_HOME", "relative")
    with pytest.raises(ValueError, match="absolute"):
        runtime.configure_environment()
    monkeypatch.setenv("SUBTRANS_DESKTOP_HOME", str(tmp_path))
    monkeypatch.setenv("SUBTRANS_DESKTOP_FFMPEG", "relative/ffmpeg")
    with pytest.raises(ValueError, match="FFMPEG"):
        runtime.configure_environment()


def test_descriptor_is_atomic_private_secret_free_and_owned(tmp_path, monkeypatch):
    path = tmp_path / "session.json"
    observed = []
    replace = os.replace

    def inspect_replace(source, target):
        observed.append(json.loads(Path(source).read_text()))
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        replace(source, target)

    monkeypatch.setattr(os, "replace", inspect_replace)
    runtime.write_session_descriptor(path, port=49173, instance_id="ours")
    assert observed == [{"port": 49173, "pid": os.getpid(), "instance_id": "ours"}]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert TOKEN not in path.read_text()
    runtime.remove_session_descriptor(path, "another-instance")
    assert path.exists()
    runtime.remove_session_descriptor(path, "ours")
    assert not path.exists()


def test_descriptor_failure_keeps_previous_file_and_cleans_temporary(tmp_path, monkeypatch):
    path = tmp_path / "session.json"
    path.write_text("previous")

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        runtime.write_session_descriptor(path, port=49173, instance_id="ours")
    assert path.read_text() == "previous"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("started,crash", [(True, False), (False, False), (True, True)])
def test_runtime_passes_prebound_socket_publishes_after_startup_and_cleans_up(desktop_env, monkeypatch, tmp_path, started, crash):
    path = tmp_path / "session.json"
    app = FastAPI()
    app_module = ModuleType("src.handler.app")
    app_module.app = app
    observations = []

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            observations.append("socket-closed")

        def bind(self, address):
            assert address == ("127.0.0.1", 0)
            observations.append("bound")

        def setblocking(self, blocking):
            assert blocking is False

        def getsockname(self):
            return ("127.0.0.1", 49173)

    listener = FakeSocket()
    uvicorn = ModuleType("uvicorn")

    class FakeConfig:
        def __init__(self, configured_app, **kwargs):
            assert configured_app is app
            assert kwargs["proxy_headers"] is False
            assert kwargs["access_log"] is False

    class FakeServer:
        def __init__(self, config):
            self.started = False
            self.should_exit = False

        async def startup(self, sockets=None):
            assert sockets == [listener]
            assert not path.exists()
            self.started = started

        async def serve(self, sockets=None):
            assert os.environ["SUBTRANS_DESKTOP"] == "1"
            assert app.state.desktop_port == 49173
            await self.startup(sockets=sockets)
            assert path.exists() is started
            if started:
                assert json.loads(path.read_text())["port"] == 49173
            app.state.desktop_request_shutdown()
            assert self.should_exit
            if crash:
                raise RuntimeError("server crashed")

    uvicorn.Config, uvicorn.Server = FakeConfig, FakeServer
    # Create the event loop before patching socket.socket (asyncio uses a socketpair).
    async def exercise():
        with monkeypatch.context() as patch:
            patch.setitem(sys.modules, "src.handler.app", app_module)
            patch.setitem(sys.modules, "uvicorn", uvicorn)
            patch.setattr(runtime.socket, "socket", lambda *args: listener)
            await runtime.serve_desktop(path)

    if crash:
        with pytest.raises(RuntimeError, match="server crashed"):
            asyncio.run(exercise())
    else:
        asyncio.run(exercise())
    assert observations == ["bound", "socket-closed"]
    assert not path.exists()


@pytest.mark.parametrize("installed", ["3.4.5", None])
def test_version_fallback_matches_update_service(monkeypatch, installed):
    from importlib import metadata
    from src.desktop.updates import _installed_version

    monkeypatch.delenv("SUBTRANS_DESKTOP_VERSION", raising=False)

    def package_version(name):
        assert name == "translatedsubs-backend"
        if installed is None:
            raise metadata.PackageNotFoundError(name)
        return installed

    monkeypatch.setattr(metadata, "version", package_version)
    monkeypatch.setattr("src.desktop.updates.version", package_version)
    assert runtime.desktop_version() == _installed_version() == (installed or "unknown")
