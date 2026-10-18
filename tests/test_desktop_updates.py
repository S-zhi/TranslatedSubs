from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from src.desktop import updates
from src.desktop.updates import MAX_MANIFEST_BYTES, UpdateService


MANIFEST_URL = "https://updates.example.com/stable.json"
DOWNLOAD_URL = "https://downloads.example.com/app.dmg"
PUBLIC_IP = "93.184.216.34"


def manifest(version="1.2.0"):
    return {
        "version": version,
        "release_notes": "Bug fixes and improvements.",
        "published_at": "2026-10-17T10:00:00Z",
        "platforms": {"macos-arm64": {
            "url": DOWNLOAD_URL,
            "sha256": "aB" * 32,
            "minimum_macos": "13.0",
        }},
    }


def make_service(document=None, *, handler=None, **kwargs):
    calls = []
    document = manifest() if document is None else document

    def respond(request):
        calls.append(request)
        return handler(request) if handler else httpx.Response(200, json=document)

    options = dict(
        manifest_url=MANIFEST_URL,
        current_version="1.1.0",
        macos_version="14.5.0",
        resolver=lambda host, port: [PUBLIC_IP],
        transport=httpx.MockTransport(respond),
    )
    options.update(kwargs)
    return UpdateService(**options), calls


def finished(service, *, force=False):
    service.check(force=force)
    worker = service._worker
    if worker is not None:
        worker.join(timeout=3)
        assert not worker.is_alive(), "Fake update worker did not complete"
    return service.check()


@pytest.mark.parametrize("latest,current,state", [
    ("1.2.0", "1.1.0", "available"),
    ("1.10.0", "1.9.0", "available"),
    ("2.0.0", "1.99.99", "available"),
    ("1.1.0", "1.1.0", "current"),
    ("1.0.9", "1.1.0", "current"),
    ("1.1.0+build.20", "1.1.0+build.1", "current"),
])
def test_stable_version_comparison(latest, current, state):
    service, calls = make_service(manifest(latest), current_version=current)
    result = finished(service)
    assert set(result) == {"state", "current_version", "latest_version", "release_notes", "download_url", "error"}
    assert result["state"] == state
    assert result["latest_version"] == latest
    assert result["current_version"] == current
    assert result["download_url"] == (DOWNLOAD_URL if state == "available" else None)
    assert result["error"] is None
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["1.2.0-rc.1", "2.0.0-beta", "1.2", "v1.2.0", "01.2.0", "1.2.0.1", "1.2.0+", "", None, True, 4])
def test_rejects_nonstable_and_malformed_versions(value):
    service, _ = make_service(manifest(value))
    assert finished(service)["state"] == "failed"


def test_invalid_current_version_fails_before_network():
    service, calls = make_service(current_version="unknown")
    assert finished(service)["state"] == "failed"
    assert not calls


def test_disabled_without_environment_channel(monkeypatch):
    monkeypatch.delenv("SUBTRANS_DESKTOP_UPDATE_URL", raising=False)
    service = UpdateService(current_version="1.0.0")
    assert service.check(force=True) == {
        "state": "disabled", "current_version": "1.0.0", "latest_version": None,
        "release_notes": None, "download_url": None, "error": None,
    }
    assert service._worker is None


def test_environment_and_singleton(monkeypatch):
    monkeypatch.setenv("SUBTRANS_DESKTOP_UPDATE_URL", MANIFEST_URL)
    monkeypatch.setenv("SUBTRANS_DESKTOP_VERSION", "1.0.0")
    monkeypatch.setattr(updates, "_service", None)
    with ThreadPoolExecutor(max_workers=8) as executor:
        services = list(executor.map(lambda _: updates.get_update_service(), range(24)))
    assert all(service is services[0] for service in services)
    assert services[0].manifest_url == MANIFEST_URL
    assert services[0].current_version == "1.0.0"


def test_installed_distribution_version(monkeypatch):
    monkeypatch.delenv("SUBTRANS_DESKTOP_VERSION", raising=False)
    names = []

    def distribution_version(name):
        names.append(name)
        return "1.0.2"

    monkeypatch.setattr(updates, "version", distribution_version)
    assert UpdateService(manifest_url="").check()["current_version"] == "1.0.2"
    assert names == ["translatedsubs-backend"]


@pytest.mark.parametrize("url", [
    "http://updates.example.com/x", "file:///tmp/update.json", "https://u:p@updates.example.com/x",
    "https://@updates.example.com/x", "https://127.0.0.1/x", "https://10.0.0.1/x",
    "https://169.254.169.254/x", "https://[::1]/x", "https://[fc00::1]/x",
    "https://[::ffff:127.0.0.1]/x", "https://localhost/x", "https://a.local/x",
    "https://a.internal/x", "https://a.localhost/x", "https://224.0.0.1/x",
    "https://updates.example.com/x#fragment", "https://updates.example.com:bad/x",
    "https://updates.example.com/\nsecret", "https://updates.example.com\\@evil.test/x",
])
@pytest.mark.parametrize("target", ["manifest", "download"])
def test_rejects_unsafe_urls(url, target):
    document = manifest()
    kwargs = {}
    if target == "manifest":
        kwargs["manifest_url"] = url
    else:
        document["platforms"]["macos-arm64"]["url"] = url
    service, calls = make_service(document, **kwargs)
    result = finished(service)
    assert result["state"] == "failed"
    assert result["download_url"] is None
    assert len(calls) == (0 if target == "manifest" else 1)


@pytest.mark.parametrize("addresses", [["10.2.3.4"], [PUBLIC_IP, "::1"], []])
def test_dns_rejects_private_or_mixed_answers(addresses):
    service, calls = make_service(resolver=lambda host, port: addresses)
    assert finished(service)["state"] == "failed"
    assert not calls


def test_download_dns_is_validated():
    service, calls = make_service(resolver=lambda host, port: [PUBLIC_IP] if host.startswith("updates.") else ["192.168.1.1"])
    assert finished(service)["state"] == "failed"
    assert len(calls) == 1


def test_connection_pins_validated_ip_and_retains_tls_hostname():
    service, calls = make_service()
    assert finished(service)["state"] == "available"
    request = calls[0]
    assert request.url.host == PUBLIC_IP
    assert request.headers["host"] == "updates.example.com"
    assert request.extensions["sni_hostname"] == "updates.example.com"
    assert all(0 < value <= 10 for value in request.extensions["timeout"].values())
    assert request.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("location", ["http://bad.example/x", "https://127.0.0.1/x", "https://10.0.0.1/x", "https://cdn.example.com/x", "/again"])
def test_all_redirects_are_rejected_without_following(location):
    service, calls = make_service(handler=lambda _: httpx.Response(302, headers={"location": location}))
    result = finished(service)
    assert result["state"] == "failed"
    assert "redirect" in result["error"]
    assert len(calls) == 1


@pytest.mark.parametrize("digest", [None, "", "a" * 63, "b" * 65, "z" * 64, 123])
def test_invalid_sha256(digest):
    document = manifest()
    document["platforms"]["macos-arm64"]["sha256"] = digest
    service, _ = make_service(document)
    assert "SHA256" in finished(service)["error"]


@pytest.mark.parametrize("field,value", [
    ("platforms", {}), ("platforms", {"macos-x64": {}}), ("platforms", []),
    ("release_notes", None), ("release_notes", 5),
    ("published_at", "yesterday"), ("published_at", "2026-10-17"),
    ("published_at", "2026-10-17T10:00:00"),
])
def test_required_manifest_fields(field, value):
    document = manifest()
    document[field] = value
    service, _ = make_service(document)
    assert finished(service)["state"] == "failed"


@pytest.mark.parametrize("document", [[], "text", {}, {"version": "1.2.0"}])
def test_malformed_manifest_shape(document):
    service, _ = make_service(document)
    assert finished(service)["state"] == "failed"


@pytest.mark.parametrize("macos,minimum,state", [
    ("13.0", "13.0.0", "available"), ("13.0", "13.1", "failed"),
    ("12.7.6", "13.0", "failed"), ("", "13.0", "failed"),
    ("14.5", "bad", "failed"), ("14.5", None, "failed"),
])
def test_minimum_macos(macos, minimum, state):
    document = manifest()
    document["platforms"]["macos-arm64"]["minimum_macos"] = minimum
    service, _ = make_service(document, macos_version=macos)
    result = finished(service)
    assert result["state"] == state
    if state == "failed":
        assert "macOS" in result["error"]
        assert result["download_url"] is None


def test_unsupported_platform_does_not_fetch():
    service, calls = make_service(platform_name="macos-x64")
    assert finished(service)["state"] == "failed"
    assert not calls


def test_cache_expiry_force_and_snapshot_isolation():
    now = [100.0]
    service, calls = make_service(clock=lambda: now[0], cache_ttl=30)
    result = finished(service)
    result["state"] = "corrupted"
    assert service.check()["state"] == "available"
    now[0] += 29
    assert finished(service)["state"] == "available"
    assert len(calls) == 1
    now[0] += 1
    assert finished(service)["state"] == "available"
    assert len(calls) == 2
    assert finished(service, force=True)["state"] == "available"
    assert len(calls) == 3


@pytest.mark.parametrize("error", [httpx.ReadTimeout("secret-url"), httpx.ConnectError("secret-token"), OSError("secret-path")])
def test_network_failure_is_cached_nonfatal_and_redacted(error):
    def fail(request):
        raise error

    service, calls = make_service(handler=fail)
    result = finished(service)
    assert result["state"] == "failed"
    assert "secret" not in result["error"]
    assert result["download_url"] is None
    assert service.check() == result
    assert len(calls) == 1


def test_failure_can_be_retried():
    attempts = []

    def retry(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json=manifest())

    service, _ = make_service(handler=retry)
    assert finished(service)["state"] == "failed"
    assert finished(service, force=True)["state"] == "available"


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"not json"),
    httpx.Response(500, text="private server details"),
    httpx.Response(204),
    httpx.Response(200, headers={"content-length": str(MAX_MANIFEST_BYTES + 1)}),
    httpx.Response(200, headers={"content-length": "-1"}),
    httpx.Response(200, headers={"content-encoding": "gzip"}),
])
def test_bad_response_fails_safely(response):
    service, _ = make_service(handler=lambda _: response)
    result = finished(service)
    assert result["state"] == "failed"
    assert "private server details" not in result["error"]


class ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


def test_chunked_body_is_bounded_without_content_length():
    stream = ChunkStream([b"x" * 8192] * (MAX_MANIFEST_BYTES // 8192 + 1))
    service, _ = make_service(handler=lambda _: httpx.Response(200, stream=stream))
    result = finished(service)
    assert result["state"] == "failed"
    assert "size limit" in result["error"]
    assert stream.closed


def test_concurrent_forced_checks_coalesce_and_do_not_block_event_loop():
    started = threading.Event()
    release = threading.Event()
    caller_thread = threading.get_ident()
    network_threads = []

    def blocking(request):
        network_threads.append(threading.get_ident())
        started.set()
        assert release.wait(3)
        return httpx.Response(200, json=manifest())

    service, calls = make_service(handler=blocking)
    try:
        async def poll():
            assert service.check()["state"] == "checking"
            await asyncio.sleep(0)
            assert service.check()["state"] == "checking"

        asyncio.run(poll())
        assert started.wait(1)
        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(lambda _: service.check(force=True), range(40)))
        assert all(result["state"] == "checking" for result in results)
        assert len(calls) == 1
        assert network_threads == [service._worker.ident]
        assert network_threads[0] != caller_thread
    finally:
        release.set()
    assert finished(service)["state"] == "available"


def test_hung_dns_times_out_without_spawning_more_workers_or_publishing_late_success():
    started = threading.Event()
    release = threading.Event()
    now = [100.0]
    resolutions = []

    def blocked_resolver(host, port):
        resolutions.append(host)
        started.set()
        assert release.wait(3)
        return [PUBLIC_IP]

    service, calls = make_service(resolver=blocked_resolver, clock=lambda: now[0], timeout=5)
    try:
        assert service.check()["state"] == "checking"
        assert started.wait(1)
        now[0] += 6
        for _ in range(20):
            result = service.check(force=True)
            assert result["state"] == "failed"
            assert "timed out" in result["error"]
        assert len(resolutions) == 1
    finally:
        release.set()
    assert finished(service)["state"] == "failed"
    assert not calls


def test_total_deadline_catches_slow_trickle():
    now = [100.0]

    def chunks():
        for _ in range(10):
            now[0] += 1
            yield b" " * 8192

    stream = ChunkStream(chunks())
    service, _ = make_service(handler=lambda _: httpx.Response(200, stream=stream), clock=lambda: now[0], timeout=5)
    assert "timed out" in finished(service)["error"]
    assert stream.closed
