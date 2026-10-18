"""Cold-process ASGI coverage with real desktop SQLite, never conftest memory.

Only model readiness and task enqueue are substituted. No socket is bound and no
model, media or update download is performed.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_isolated(script, tmp_path, *, version=None):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("SUBTRANS_DESKTOP")}
    env.update(SUBTRANS_DESKTOP="1", SUBTRANS_DESKTOP_HOME=str(tmp_path),
               SUBTRANS_DESKTOP_SESSION_TOKEN="isolated-test-session")
    if version is not None:
        env["SUBTRANS_DESKTOP_VERSION"] = version
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("version", [None, "2.3.4"])
def test_cold_boot_sqlite_defaults_task_media_recovery_and_shutdown(tmp_path, version):
    run_isolated(r'''
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Fail closed on accidental network access; ASGI TestClient needs no connection.
import socket
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network forbidden"))
from src.config.config_repository import get_config_provider, ConfigUnavailable
from src.config.sqlite_config import SQLiteConfigProvider
provider = get_config_provider()
assert isinstance(provider, SQLiteConfigProvider)
assert not provider.db_path.exists()
try:
    provider.read()
except ConfigUnavailable:
    pass
else:
    raise AssertionError("reading uninitialized config must fail")

from src.desktop import models
from src.service import model_manager as whisper_module
from src.service import translation_model_manager as translation_module
preparations = []
coordinator = SimpleNamespace(
    start_prepare=lambda: preparations.append(True),
    status=lambda: {"state": "ready", "ready": True, "models": [], "error": None})
models.get_desktop_model_manager = lambda: coordinator
whisper_module.model_manager.is_ready = lambda name: name == "base.en"
whisper_module.model_manager.list_models = lambda: [{"name": "base.en", "status": "READY"}]
translation = SimpleNamespace(is_ready=lambda: True,
    dependency_status=lambda: {"ready": True, "missing": []},
    status=lambda: {"model_status": "READY", "installed_bytes": 1, "model_error": None})
translation_module.get_translation_model_manager = lambda: translation

# Import before config initialization must not read business settings.
from src.handler.app import create_app
from src.handler import deps, tasks
from src.config import settings, task_dir
from src.desktop.security import COOKIE_NAME
from src.desktop.updates import _installed_version
from src.service import runner, retention_scheduler
from starlette.testclient import TestClient
assert not provider.db_path.exists()
home = Path(os.environ["SUBTRANS_DESKTOP_HOME"])
assert settings.db_path == home / "db" / "app.db"
assert settings.data_dir == home / "data"
assert runner._store.db_path == settings.db_path
store = deps.get_store()
old = store.create(url="https://www.youtube.com/watch?v=interrupted", source_lang="en",
    target_lang="zh-CN", mode="bilingual", burn="hard", model="local:base.en",
    engine="local-opus-en-zh")
queued = []
tasks.enqueue_pipeline = queued.append
app = create_app()
owner_calls = []
app.state.desktop_request_shutdown = lambda: owner_calls.append(True)
origin = "http://127.0.0.1:5387"
with TestClient(app, base_url=origin) as client:
    assert provider.db_path.is_file()
    assert store.get(old.id).error_code == "desktop_interrupted"
    assert store.get(old.id).status == "FAILED"
    assert queued == []
    assert client.get("/api/health").status_code == 401
    client.cookies.set(COOKIE_NAME, os.environ["SUBTRANS_DESKTOP_SESSION_TOKEN"])
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/").status_code == 200
    config = client.get("/config.js")
    assert config.status_code == 200 and config.headers["cache-control"] == "no-store"
    assert 'window.APP_CONFIG.API_BASE_URL = window.location.origin' in config.text
    assert 'localStorage.removeItem' in config.text
    assert os.environ["SUBTRANS_DESKTOP_SESSION_TOKEN"] not in config.text
    status = client.get("/api/desktop/status").json()
    assert status["ready"] is True and status["active_tasks"] == 0
    expected_version = _installed_version()
    assert status["version"] == expected_version
    assert '"version": ' + json.dumps(expected_version) in config.text
    update = client.get("/api/desktop/updates").json()
    assert update["state"] == "disabled" and update["current_version"] == expected_version
    defaults = status["defaults"]
    assert defaults == {"sourceLang": "en", "targetLang": "zh-CN", "mode": "bilingual",
        "burn": "hard", "model": "local:base.en", "engine": "local-opus-en-zh",
        "needSubtitle": True, "ttsEnabled": False}
    assert not provider.read().values["models"]["llm"]["enabled"]
    payload = dict(defaults, url="https://www.youtube.com/watch?v=preview-test")
    created = client.post("/api/tasks", json=payload)
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    assert queued == [task_id]
    record = store.get(task_id)
    runtime = json.loads(record.runtime_config_json)
    assert runtime["engine"]["provider"] == "local"
    assert runtime["engine"]["id"] == "local-opus-en-zh"
    assert runtime["whisper"]["model"] == "local:base.en"
    assert runtime["tts"]["enabled"] is False
    assert os.environ["SUBTRANS_DESKTOP_SESSION_TOKEN"] not in record.runtime_config_json
    assert client.get("/api/desktop/tasks/active").json() == {"count": 1}
    # Tiny synthetic artifact verifies authorization and byte delivery, not decoding.
    directory = task_dir(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / "output.mp4"
    artifact.write_bytes(b"integration-media-bytes")
    store.update(task_id, status="SUCCESS", output_video=artifact.name)
    media = client.get(f"/api/tasks/{task_id}/download")
    assert media.status_code == 200, media.text
    assert media.content == artifact.read_bytes()
    client.cookies.clear()
    assert client.get(f"/api/tasks/{task_id}/download").status_code == 401
    client.cookies.set(COOKIE_NAME, os.environ["SUBTRANS_DESKTOP_SESSION_TOKEN"])
    assert client.post("/api/tasks", json=payload, headers={"Origin": "https://invalid.example"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "invalid.example"}).status_code == 400
    # Leave a real pending record for lifecycle cancellation, without a worker.
    pending = store.create(url="https://www.youtube.com/watch?v=pending-exit", source_lang="en",
        target_lang="zh-CN", mode="bilingual", burn="hard", model="local:base.en", engine="local-opus-en-zh")
    assert retention_scheduler._scheduler_thread.is_alive()
    assert client.post("/api/desktop/shutdown").status_code == 200
    assert client.post("/api/tasks", json=payload).status_code == 503
assert preparations == [True]
assert owner_calls == [True]
assert app.state.desktop_tasks_stopped is True
assert app.state.desktop_prepare_task.done()
assert not retention_scheduler._scheduler_thread.is_alive()
assert runner._executor is None
assert store.get(pending.id).status == "CANCELLED"
assert store.get(task_id).status == "SUCCESS"
assert artifact.read_bytes() == b"integration-media-bytes"
# Persisted desktop configuration is still readable after shutdown.
assert SQLiteConfigProvider(provider.db_path).read().values["models"]["whisper"]["model"] == "local:base.en"
''', tmp_path, version=version)


@pytest.mark.skipif(not (ROOT / ".ci/astra/preview_runtime.py").exists(),
                    reason="Local-only ignored preview harness is not distributed")
def test_local_preview_harness_uses_real_auth_and_task_api(tmp_path):
    run_isolated(r'''
import os
import runpy
import time
from pathlib import Path
from starlette.testclient import TestClient
namespace = runpy.run_path(".ci/astra/preview_runtime.py")
preview = namespace["create_preview_app"](5387, home=Path(os.environ["SUBTRANS_DESKTOP_HOME"]))
from src.config.config_repository import get_config_provider
from src.config.sqlite_config import SQLiteConfigProvider
from src.desktop.security import COOKIE_NAME
from src.handler.deps import get_store
assert isinstance(get_config_provider(), SQLiteConfigProvider)
origin = "http://127.0.0.1:5387"
with TestClient(preview, base_url=origin, client=("127.0.0.1", 12345)) as client:
    for path in ("/", "/config.js", "/__preview/submissions", "/api/desktop/status"):
        assert client.get(path).status_code == 401
    assert client.post("/__preview/models/ready").status_code == 401
    assert client.get("/__preview/session", headers={"Host": "attacker.invalid"}).status_code == 403
    assert client.get("/__preview/session", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/__preview/session").status_code == 403
    session = client.get("/__preview/session", follow_redirects=False)
    assert session.status_code == 303 and session.headers["location"] == "/"
    cookie = session.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    assert preview.token not in session.text and preview.token not in session.headers["location"]
    assert client.get("/").status_code == 200
    status = client.get("/api/desktop/status").json()
    assert status["models"]["state"] == "preparing"
    assert client.post("/__preview/models/failed").json()["state"] == "failed"
    assert client.get("/api/desktop/status").json()["models"]["state"] == "failed"
    assert client.post("/api/desktop/models/retry").json()["state"] == "preparing"
    assert client.post("/__preview/models/ready").json()["ready"] is True
    assert client.post("/__preview/models/invalid").status_code == 422
    assert client.post("/__preview/models/failed", headers={"Origin": "https://attacker.invalid"}).status_code == 403
    status = client.get("/api/desktop/status").json()
    assert status["ready"] is True
    inventory = client.get("/api/srt/local-models")
    assert inventory.status_code == 200, inventory.text
    assert any(row["name"] == "base.en" and row["status"] == "READY" for row in inventory.json())
    settings = client.get("/api/settings/models")
    assert settings.status_code == 200, settings.text
    assert settings.json()["whisper"]["status"] == "READY", settings.text
    created = client.post("/api/tasks", json=dict(status["defaults"], url="https://www.youtube.com/watch?v=preview"))
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    submissions = client.get("/__preview/submissions").json()["submissions"]
    assert len(submissions) == 1 and submissions[0]["id"] == task_id
    assert submissions[0]["model"] == "local:base.en"
    assert get_store().get(task_id).status == "PENDING"
    assert client.get("/api/desktop/updates").json()["state"] == "checking"
    # Polling a simulated 1-second transition exercises real API serialization.
    time.sleep(1.05)
    assert client.get("/api/desktop/updates").json()["state"] == "available"
    assert client.get("/api/desktop/updates?force=true").json()["state"] == "checking"
assert get_store().get(task_id).status == "CANCELLED"
# No bootstrap escape hatch was registered in the production app itself.
client = TestClient(preview.app, base_url=origin, client=("127.0.0.1", 12346))
assert client.get("/__preview/session").status_code == 401
client.cookies.set(COOKIE_NAME, preview.token)
assert client.get("/__preview/session").status_code == 404
remote = TestClient(preview, base_url=origin, client=("192.0.2.1", 12346))
assert remote.get("/__preview/session").status_code == 403
''', tmp_path)


def test_corrupt_desktop_configuration_aborts_before_preparation(tmp_path):
    run_isolated(r'''
from src.config.config_repository import get_config_provider, ConfigUnavailable
provider = get_config_provider()
provider.db_path.parent.mkdir(parents=True)
provider.db_path.write_bytes(b"not a SQLite database")
from types import SimpleNamespace
from src.desktop import models
calls = []
models.get_desktop_model_manager = lambda: SimpleNamespace(start_prepare=lambda: calls.append(True))
from src.handler.app import create_app
from src.service import retention_scheduler
from starlette.testclient import TestClient
app = create_app()
try:
    with TestClient(app, base_url="http://127.0.0.1:5387"):
        raise AssertionError("Corrupt config must never serve requests")
except ConfigUnavailable:
    pass
assert calls == []
assert retention_scheduler._scheduler_thread is None
assert not hasattr(app.state, "desktop_prepare_task")
assert provider.db_path.read_bytes() == b"not a SQLite database"
''', tmp_path)
