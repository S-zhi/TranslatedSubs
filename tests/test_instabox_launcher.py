"""Launcher API, cost reservations and recovery with no cloud resources."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from src.instabox_launcher.app import create_app
from src.instabox_launcher.config import LauncherConfig
from src.instabox_launcher.provider import InstaBoxClient, ProviderError
from src.instabox_launcher.service import TrialService
from src.instabox_launcher.store import ProcessLock, ReservationDenied, TrialStore


@pytest.fixture
def config(tmp_path):
    return LauncherConfig(api_key="ibk_private_key", secret="stable-secret-" * 4,
                          origin="https://product.example", source_revision="a" * 40,
                          db_path=tmp_path / "private" / "trials.sqlite3")


class FakeCloud:
    def __init__(self):
        self.requests = []
        self.machines = {}
        self.creates = {}
        self.lose_create = False
        self.exec_failure = False
        self.delete_failures = 0
        self.failed_delete_operation = False
        self.bad_url = None

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        payload = json.loads(request.content) if request.content else {}
        if path == "/v1/machines" and request.method == "POST":
            key = request.headers["idempotency-key"]
            if key not in self.creates:
                machine_id = f"m_{len(self.creates) + 1}"
                self.creates[key] = machine_id
                self.machines[machine_id] = {"id": machine_id, "state": "running", "metadata": payload["metadata"]}
            machine_id = self.creates[key]
            if self.lose_create:
                self.lose_create = False
                raise httpx.ReadTimeout("secret upstream failure", request=request)
            return httpx.Response(202, json={"id": f"op_create_{machine_id}", "machineId": machine_id, "state": "succeeded"})
        if path == "/v1/machines" and request.method == "GET":
            return httpx.Response(200, json={"machines": list(self.machines.values()), "nextCursor": None})
        if path.startswith("/v1/operations/"):
            operation = path.rsplit("/", 1)[-1]
            machine_id = "m_" + operation.rsplit("_", 1)[-1]
            state = "failed" if self.failed_delete_operation and "delete_failed" in operation else "succeeded"
            return httpx.Response(200, json={"id": operation, "machineId": machine_id, "state": state})
        machine_id = path.split("/")[3]
        if path.endswith("/exec"):
            return httpx.Response(200, json={"exitCode": 1 if self.exec_failure else 0,
                                             "timedOut": False, "truncated": False,
                                             "stdout": "ready\n" if payload.get("stdin") else "verified\n",
                                             "stderr": "private token in upstream output"})
        if path.endswith("/ports"):
            return httpx.Response(201, json={"id": "8000-trial", "machineId": machine_id, "port": 8000,
                                             "url": self.bad_url or "https://8000-trial.instabox.tech/"})
        if request.method == "DELETE":
            if self.delete_failures:
                self.delete_failures -= 1
                return httpx.Response(503, json={"message": "private upstream detail"})
            if self.failed_delete_operation and request.headers["idempotency-key"].startswith("trial-delete-") and len(request.headers["idempotency-key"]) < 60:
                return httpx.Response(202, json={"id": f"op_delete_failed_{machine_id}", "machineId": machine_id, "state": "failed"})
            self.machines.pop(machine_id, None)
            return httpx.Response(202, json={"id": f"op_delete_{machine_id}", "machineId": machine_id, "state": "succeeded"})
        if machine_id not in self.machines:
            return httpx.Response(404)
        return httpx.Response(200, json=self.machines[machine_id])


def setup_service(config, cloud, now):
    store = TrialStore(config.db_path, config.launcher_id)
    provider = InstaBoxClient(config.api_key, transport=httpx.MockTransport(cloud))
    return TrialService(config, store, provider, clock=lambda: now[0])


async def advance_until(service, trial_id, phase):
    for _ in range(12):
        await service.advance(trial_id)
        if service.store.get(trial_id)["phase"] == phase:
            return
    assert service.store.get(trial_id)["phase"] == phase


def test_full_lifecycle_keeps_credentials_private_and_publishes_only_after_checks(config):
    async def scenario():
        cloud, now = FakeCloud(), [1000.0]
        service = setup_service(config, cloud, now)
        trial = service.reserve("owner")
        assert service.reserve("owner")["id"] == trial["id"]
        await advance_until(service, trial["id"], "ready")
        data = service.public(service.store.get(trial["id"]))
        token = config.digest("instance-token", trial["id"])
        assert data["url"] == f"https://8000-trial.instabox.tech/#trial_token={token}"
        assert config.api_key not in json.dumps(data)
        creates = [r for r in cloud.requests if r.method == "POST" and r.url.path == "/v1/machines"]
        assert len(creates) == 1
        payload = json.loads(creates[0].content)
        assert payload["vault"] is False and payload["onTimeout"] == "kill"
        assert payload["timeoutSeconds"] == 1800 and payload["templateId"] == "universal"
        execs = [r for r in cloud.requests if r.url.path.endswith("/exec")]
        assert len(execs) == 2
        assert json.loads(execs[0].content)["envs"]["SUBTRANS_TRIAL_REVISION"] == "a" * 40
        assert all(config.api_key not in r.content.decode() for r in cloud.requests)
        assert all(r.url.host == "api.instabox.sh" and r.headers["x-api-key"] == config.api_key for r in cloud.requests)
        assert cloud.requests.index(execs[-1]) < next(i for i, r in enumerate(cloud.requests) if r.url.path.endswith("/ports"))
        assert token not in service.store.path.read_bytes().decode(errors="ignore")
        assert service.store.path.stat().st_mode & 0o777 == 0o600
        service.end(service.store.get(trial["id"]))
        assert service.public(service.store.get(trial["id"]))["url"] is None
        await advance_until(service, trial["id"], "expired")
        assert not service.store.get(trial["id"])["holds_capacity"]
        assert not cloud.machines
        await service.provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [400, 401, 402, 403, 429])
def test_rejected_create_releases_slot_but_keeps_creation_budget(config, status):
    async def scenario():
        def rejected(request):
            return httpx.Response(status, json={"error": {"code": "rejected", "message": "secret diagnostic"}})
        now = [1000.0]
        provider = InstaBoxClient(config.api_key, transport=httpx.MockTransport(rejected))
        store = TrialStore(config.db_path, config.launcher_id)
        service = TrialService(config, store, provider, clock=lambda: now[0])
        trial = service.reserve("owner")
        await service.advance(trial["id"])
        row = store.get(trial["id"])
        assert row["phase"] == "failed" and not row["holds_capacity"]
        assert "secret diagnostic" not in json.dumps(service.public(row))
        assert store.current("owner")["id"] == trial["id"]
        await provider.close()
    asyncio.run(scenario())


def test_exec_http_200_failure_never_publishes_and_is_reclaimed(config, caplog):
    async def scenario():
        cloud, now = FakeCloud(), [1000.0]
        cloud.exec_failure = True
        service = setup_service(config, cloud, now)
        trial = service.reserve("owner")
        await advance_until(service, trial["id"], "failed")
        assert not any(r.url.path.endswith("/ports") for r in cloud.requests)
        assert "private token" not in caplog.text
        assert not cloud.machines
        await service.provider.close()
    asyncio.run(scenario())


def test_ambiguous_create_replays_frozen_key_without_duplicate_vm_and_survives_restart(config):
    async def scenario():
        cloud, now = FakeCloud(), [1000.0]
        cloud.lose_create = True
        first = setup_service(config, cloud, now)
        trial = first.reserve("owner")
        await first.advance(trial["id"])
        assert first.store.get(trial["id"])["holds_capacity"]
        assert first.store.get(trial["id"])["create_uncertain"]
        await first.provider.close()
        recovered = setup_service(config, cloud, now)
        await advance_until(recovered, trial["id"], "ready")
        creates = [r for r in cloud.requests if r.url.path == "/v1/machines" and r.method == "POST"]
        assert len(creates) == 2
        assert creates[0].content == creates[1].content
        assert creates[0].headers["idempotency-key"] == creates[1].headers["idempotency-key"]
        assert len(cloud.creates) == 1
        await recovered.provider.close()
    asyncio.run(scenario())


def test_unknown_create_after_expiry_is_looked_up_and_deleted_without_new_create(config):
    async def scenario():
        cloud, now = FakeCloud(), [1000.0]
        cloud.lose_create = True
        service = setup_service(config, cloud, now)
        trial = service.reserve("owner")
        await service.advance(trial["id"])
        now[0] += 1801
        await advance_until(service, trial["id"], "expired")
        assert len(cloud.creates) == 1
        assert not cloud.machines
        assert service.public(service.store.get(trial["id"]))["url"] is None
        await service.provider.close()
    asyncio.run(scenario())


def test_empty_reconciliation_does_not_release_ambiguous_reservation(config):
    async def scenario():
        cloud, now = FakeCloud(), [1000.0]
        service = setup_service(config, cloud, now)
        trial = service.reserve("owner")
        service.store.update(trial["id"], now=now[0], create_attempted=1, create_uncertain=1)
        now[0] += 1801
        await service.advance(trial["id"])
        row = service.store.get(trial["id"])
        assert row["holds_capacity"] and row["phase"] == "deleting"
        assert not any(r.method == "POST" for r in cloud.requests)
        await service.provider.close()
    asyncio.run(scenario())


def test_deletion_transport_failure_retains_capacity_then_retries(config):
    async def scenario():
        cloud, now = FakeCloud(), [1000.0]
        service = setup_service(config, cloud, now)
        trial = service.reserve("owner")
        await advance_until(service, trial["id"], "ready")
        cloud.delete_failures = 1
        now[0] += 1801
        await service.advance(trial["id"])
        assert service.store.get(trial["id"])["holds_capacity"]
        assert service.public(service.store.get(trial["id"]))["url"] is None
        await advance_until(service, trial["id"], "expired")
        deletes = [r for r in cloud.requests if r.method == "DELETE"]
        assert len(deletes) == 2
        assert deletes[0].headers["idempotency-key"] == deletes[1].headers["idempotency-key"]
        await service.provider.close()
    asyncio.run(scenario())


def test_confirmed_failed_delete_operation_uses_new_persisted_key(config):
    async def scenario():
        cloud, now = FakeCloud(), [1000.0]
        service = setup_service(config, cloud, now)
        trial = service.reserve("owner")
        await advance_until(service, trial["id"], "ready")
        cloud.failed_delete_operation = True
        service.end(service.store.get(trial["id"]))
        await advance_until(service, trial["id"], "expired")
        keys = [r.headers["idempotency-key"] for r in cloud.requests if r.method == "DELETE"]
        assert len(keys) == 2 and keys[0] != keys[1]
        assert not cloud.machines
        await service.provider.close()
    asyncio.run(scenario())


def test_cancel_during_lost_create_response_does_not_resurrect_trial(config):
    async def scenario():
        now, cloud = [1000.0], FakeCloud()
        service = None
        def cancel_after_create(request):
            response = cloud(request)
            if request.url.path == "/v1/machines" and request.method == "POST":
                trial = service.store.current("owner")
                service.end(trial)
            return response
        store = TrialStore(config.db_path, config.launcher_id)
        provider = InstaBoxClient(config.api_key, transport=httpx.MockTransport(cancel_after_create))
        service = TrialService(config, store, provider, clock=lambda: now[0])
        trial = service.reserve("owner")
        await service.advance(trial["id"])
        row = store.get(trial["id"])
        assert row["phase"] == "deleting" and row["machine_id"]
        assert service.public(row)["url"] is None
        await advance_until(service, trial["id"], "expired")
        assert not cloud.machines
        assert not any(r.url.path.endswith("/exec") or r.url.path.endswith("/ports") for r in cloud.requests)
        await provider.close()
    asyncio.run(scenario())


def test_cancel_during_definitively_rejected_create_does_not_hold_slot_forever(config):
    async def scenario():
        service = None
        def rejected(request):
            service.end(service.store.current("owner"))
            return httpx.Response(429, json={"error": {"code": "quota_exceeded"}})
        provider = InstaBoxClient(config.api_key, transport=httpx.MockTransport(rejected))
        store = TrialStore(config.db_path, config.launcher_id)
        service = TrialService(config, store, provider, clock=lambda: 1000.0)
        trial = service.reserve("owner")
        await service.advance(trial["id"])
        row = store.get(trial["id"])
        assert row["phase"] == "expired" and not row["holds_capacity"]
        await provider.close()
    asyncio.run(scenario())


def test_scheduler_recovers_after_temporary_sqlite_read_failure(config, caplog):
    async def scenario():
        cloud, now = FakeCloud(), [1000.0]
        service = setup_service(replace(config, sweep_seconds=0.001), cloud, now)
        calls = []
        def tick():
            calls.append(True)
            if len(calls) == 1:
                raise OSError("transient database issue")
        service.tick = tick
        await service.start()
        for _ in range(100):
            if len(calls) > 1:
                break
            await asyncio.sleep(0.001)
        assert len(calls) > 1 and not service.sweeper.done()
        assert "temporarily unavailable" in caplog.text
        await service.close()
        await service.provider.close()
    asyncio.run(scenario())


def test_local_expiry_hides_ready_url_without_browser_polling(config):
    async def scenario():
        cloud, now = FakeCloud(), [1000.0]
        service = setup_service(config, cloud, now)
        trial = service.reserve("owner")
        await advance_until(service, trial["id"], "ready")
        now[0] = trial["expires_at"]
        assert service.public(service.store.get(trial["id"]))["status"] == "expired"
        assert service.public(service.store.get(trial["id"]))["url"] is None
        await advance_until(service, trial["id"], "expired")
        await service.provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["https://evil.example/", "http://8000-trial.instabox.tech/",
                                  "https://evil.instabox.tech.evil.example/", "https://user@8000-trial.instabox.tech/",
                                  "https://8000-trial.instabox.tech/?token=x"])
def test_invalid_publication_url_is_never_delivered(config, url):
    async def scenario():
        cloud, now = FakeCloud(), [1000.0]
        cloud.bad_url = url
        service = setup_service(config, cloud, now)
        trial = service.reserve("owner")
        await advance_until(service, trial["id"], "failed")
        assert service.public(service.store.get(trial["id"]))["url"] is None
        await service.provider.close()
    asyncio.run(scenario())


def test_atomic_concurrent_reservations_deduplicate_and_enforce_global_capacity(config):
    store = TrialStore(config.db_path, config.launcher_id)
    def reserve(owner):
        try:
            return store.reserve(owner, now=1000, ttl=1800, max_active=2, hourly=6, daily=12,
                                 revision=config.source_revision, payload_factory=lambda trial_id: {"name": trial_id})["id"]
        except ReservationDenied as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        same = list(pool.map(reserve, ["owner"] * 8))
    assert len(set(same)) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        different = list(pool.map(reserve, [f"owner-{i}" for i in range(8)]))
    assert different.count("capacity") == 7
    assert len(store.due(1000)) == 2


def test_creation_budget_survives_resource_release(config):
    store = TrialStore(config.db_path, config.launcher_id)
    first = store.reserve("owner", now=1000, ttl=1800, max_active=2, hourly=1, daily=12,
                          revision=config.source_revision, payload_factory=lambda _: {})
    store.finish(first["id"], "failed", 1001)
    with pytest.raises(ReservationDenied, match="budget"):
        store.reserve("owner-2", now=1002, ttl=1800, max_active=2, hourly=1, daily=12,
                      revision=config.source_revision, payload_factory=lambda _: {})


def test_process_lock_and_stable_identity_protect_restart(config):
    lock = ProcessLock(config.db_path.with_suffix(".lock"))
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="one worker"):
            ProcessLock(config.db_path.with_suffix(".lock")).acquire()
    finally:
        lock.release()
    TrialStore(config.db_path, config.launcher_id)
    with pytest.raises(RuntimeError, match="origin/secret"):
        TrialStore(config.db_path, "different-launcher")


def test_api_cookie_ownership_origin_and_refresh_without_allocation(config):
    cloud = FakeCloud()
    app = create_app(config, transport=httpx.MockTransport(cloud), run_workers=False)
    with TestClient(app, base_url=config.origin) as client:
        response = client.get("/api/trials/current")
        assert response.json()["status"] == "idle" and not cloud.requests
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
        headers = {"Origin": config.origin}
        launched = client.post("/api/trials", json={}, headers=headers)
        assert launched.status_code == 202
        trial_id = launched.json()["trialId"]
        assert client.post("/api/trials", json={}, headers=headers).json()["trialId"] == trial_id
        assert client.get("/api/trials/current").json()["trialId"] == trial_id
        app.state.service.store.update(trial_id, now=1000, phase="ready",
                                       port_url="https://8000-trial.instabox.tech/")
        assert client.get("/api/trials/current").json()["url"]
        assert client.post("/api/trials", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.delete("/api/trials/current", headers=headers).status_code == 400
        assert client.post("/api/trials", json={"command": "anything"}, headers=headers).status_code == 400
        assert client.post("/api/trials", content="x" * 1025,
                           headers={**headers, "Content-Type": "application/json"}).status_code == 400
        assert client.get("/api/trials/current", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        client.cookies.clear()
        assert client.post("/api/trials", json={}, headers=headers).status_code == 409
        assert client.get("/api/trials/current").json()["trialId"] is None
        # Only an owning cookie receives a ready URL; the opaque ID is not a route.
        assert client.get(f"/api/trials/{trial_id}").status_code == 404
        assert client.get("/api/trials/current").headers["cache-control"] == "no-store"
        assert client.get("/api/trials/current").headers["referrer-policy"] == "no-referrer"
        assert not cloud.requests


def test_unconfigured_service_is_honest_and_never_contacts_provider(tmp_path):
    cloud = FakeCloud()
    app = create_app(LauncherConfig(db_path=tmp_path / "unused.sqlite3"), transport=httpx.MockTransport(cloud))
    with TestClient(app) as client:
        assert client.get("/api/trials/current").json()["status"] == "unavailable"
        assert client.post("/api/trials", json={}).status_code == 503
        assert not cloud.requests
        assert not (tmp_path / "unused.sqlite3").exists()


@pytest.mark.parametrize("kwargs", [{"origin": "http://product.example"}, {"source_revision": "main"},
                                    {"secret": "short"}, {"ttl_seconds": 86400}, {"max_active": 100}])
def test_invalid_configuration_fails_without_network(kwargs):
    with pytest.raises(ValueError):
        LauncherConfig(**kwargs)
