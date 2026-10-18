"""Offline benchmark contract and timing tests; no models, service, or media I/O."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/desktop/benchmark.py"
SPEC = importlib.util.spec_from_file_location("desktop_benchmark", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class Clock:
    def __init__(self):
        self.now = 0.0

    def read(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def desktop_status():
    return {"version": "0.1.0", "platform": "macos-arm64", "ready": True,
            "active_tasks": 0, "defaults": dict(benchmark.DEFAULTS),
            "models": {"ready": True, "models": [
                {"id": key, "status": "ready", "revision": "a" * 40}
                for key in ("local:base.en", "local-opus-en-zh")
            ]}}


def task(state):
    return {"id": "task-123", "sourceType": "upload", **benchmark.DEFAULTS,
            "status": state, "createdAt": 1700000000000, "updatedAt": 1700000010000,
            "resourceStatus": "AVAILABLE",
            "outputs": {"video": "/private/secret/result.mp4", "subtitle": "/private/secret/sub.srt"},
            "title": "/private/secret/video.mp4", "error": "server-secret"}


def report_base():
    return {"host": {"acceptance_hardware": True, "classification": "M1_8GB_ACCEPTANCE"},
            "media": {"acceptance_sample": True}}


def execute(tmp_path, states, *, ready=None, timeout=1000, upload_seconds=500, tick=0.1):
    clock = Clock()
    requests = []
    pending = iter(states)
    media = tmp_path / "sensitive-title.mp4"
    media.write_bytes(b"fake video, never decoded")

    def respond(request):
        requests.append(request)
        assert request.headers["X-API-Token"] == "session-secret"
        if request.url.path == "/api/desktop/status":
            return httpx.Response(200, json=ready if ready is not None else desktop_status())
        if request.url.path == "/api/tasks/upload":
            body = request.read()
            assert b"sensitive-title" not in body
            assert b"benchmark-input.mp4" in body
            for value in (b"bilingual", b"hard", b"local:base.en", b"local-opus-en-zh", b"false"):
                assert value in body
            clock.advance(upload_seconds)
            return httpx.Response(201, json=task("PENDING"))
        assert request.method == "GET" and request.url.path == "/api/tasks/task-123"
        clock.advance(tick)
        value = next(pending)
        if isinstance(value, Exception):
            raise value
        return httpx.Response(200, json=task(value))

    with httpx.Client(base_url="http://127.0.0.1:1234", headers={"X-API-Token": "session-secret"},
                      transport=httpx.MockTransport(respond)) as client:
        report = benchmark.run_benchmark(
            client, media, report_base(), poll_interval=1, timeout=timeout,
            monotonic=clock.read, wall_time=clock.read, sleep=clock.advance,
        )
    return report, requests


def test_upload_excluded_and_encode_included_with_polling_bounds(tmp_path):
    report, requests = execute(tmp_path, ["EXTRACTING", "TRANSCRIBING", "TRANSCRIBING",
                                          "TRANSLATING", "BURNING", "BURNING", "SUCCESS"])
    assert report["result"] == "PASS_TIMING_ONLY"
    assert report["upload_request_seconds_excluded"] == 500
    assert report["timing"]["total"]["lower_seconds"] == pytest.approx(4.3)
    assert report["timing"]["total"]["upper_seconds"] == pytest.approx(6.7)
    assert report["timing"]["stages"]["burning"]["lower_seconds"] > 0
    assert report["manual_quality"] == "NOT RUN"
    assert len(requests) == 9
    serialized = json.dumps(report)
    assert "/private/secret" not in serialized
    assert "server-secret" not in serialized
    assert "session-secret" not in serialized


@pytest.mark.parametrize("states", [
    ["TRANSCRIBING", "TRANSLATING", "BURNING", "SUCCESS"],
    ["EXTRACTING", "TRANSCRIBING", "BURNING", "SUCCESS"],
    ["EXTRACTING", "TRANSCRIBING", "TRANSLATING", "SUCCESS"],
    ["EXTRACTING", "DOWNLOADING", "TRANSCRIBING", "TRANSLATING", "BURNING", "SUCCESS"],
    ["EXTRACTING", "TRANSCRIBING", "TRANSLATING", "TRANSCRIBING", "BURNING", "SUCCESS"],
])
def test_missed_boundaries_or_unexpected_download_never_pass(tmp_path, states):
    report, _ = execute(tmp_path, states)
    assert report["result"] == "INCONCLUSIVE"
    assert report["timing"]["total"] is None


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED"])
def test_task_terminal_failure_retains_observations(tmp_path, state):
    report, _ = execute(tmp_path, ["TRANSCRIBING", state])
    assert report["result"] == "FAILED"
    assert report["error"] == "task_" + state.lower()
    assert len(report["observations"]) == 2


def test_models_must_be_ready_and_no_prepare_or_upload(tmp_path):
    status = desktop_status()
    status["models"]["ready"] = False
    report, requests = execute(tmp_path, [], ready=status)
    assert report["error"] == "models_not_ready"
    assert len(requests) == 1


def test_active_tasks_prevent_contaminated_benchmark(tmp_path):
    status = desktop_status()
    status["active_tasks"] = 1
    report, requests = execute(tmp_path, [], ready=status)
    assert report["error"] == "other_tasks_active"
    assert len(requests) == 1


def test_timeout_preserves_task_id_and_does_not_cancel(tmp_path):
    report, requests = execute(tmp_path, ["TRANSCRIBING"] * 5, timeout=2)
    assert report["error"] == "poll_timeout_task_may_still_be_running"
    assert report["task_id"] == "task-123"
    assert all("cancel" not in r.url.path for r in requests)


def test_request_failure_does_not_echo_sensitive_exception(tmp_path):
    report, _ = execute(tmp_path, [httpx.ReadTimeout("session-secret /private/secret")])
    assert report["error"] == "api_request_timeout"
    assert "session-secret" not in json.dumps(report)


@pytest.mark.parametrize("lower,upper,expected", [
    (7199, 7200, "PASS_TIMING_ONLY"), (7199, 7201, "INCONCLUSIVE_AT_7200S"),
    (7201, 7202, "FAIL_OVER_7200S"),
])
def test_two_hour_limit_uses_conservative_bounds(lower, upper, expected):
    report = report_base()
    report.update(task_status="SUCCESS", outputs_present=True,
                  app=benchmark.desktop_metadata(desktop_status()),
                  timing={"total": {"lower_seconds": lower, "upper_seconds": upper}})
    assert benchmark.evaluate(report) == expected
    report["host"]["acceptance_hardware"] = False
    if upper <= 7200:
        assert benchmark.evaluate(report) == "DEVELOPMENT_ONLY"


def test_missing_versions_and_clock_jump_cannot_pass(tmp_path):
    status = desktop_status()
    status["models"]["models"][0].pop("revision")
    report, _ = execute(tmp_path, ["EXTRACTING", "TRANSCRIBING", "TRANSLATING", "BURNING", "SUCCESS"], ready=status)
    assert report["result"] == "INCONCLUSIVE_VERSION_EVIDENCE"
    report["clock_discontinuity"] = True
    assert benchmark.evaluate(report) == "INCONCLUSIVE"


@pytest.mark.parametrize("url", ["https://example.com", "http://127.0.0.1:1234/?token=secret",
                                 "http://secret@127.0.0.1:1234", "http://localhost:1234",
                                 "http://127.0.0.1:1234/api", "http://127.0.0.1:99999"])
def test_only_exact_local_origin_is_accepted(url):
    with pytest.raises(benchmark.BenchmarkError, match="invalid_loopback_origin"):
        benchmark.validate_origin(url)


def test_redirect_and_auth_failure_are_explicit_and_not_followed():
    for code in (302, 401, 403, 413, 500):
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(code))) as client:
            with pytest.raises(benchmark.BenchmarkError, match=f"api_http_{code}"):
                benchmark.api_json(client, "GET", "http://127.0.0.1:1234/api/desktop/status")


def test_probe_and_hardware_metadata_are_real_queries_with_strict_target(monkeypatch, tmp_path):
    calls = []

    def command(args):
        calls.append(args)
        return json.dumps({"format": {"duration": "3600.04"}, "streams": [{"width": 1920, "height": 1080}]})

    monkeypatch.setattr(benchmark, "command", command)
    media = tmp_path / "private.mp4"
    media.write_bytes(b"fake")
    assert benchmark.probe_media(media, "ffprobe")["acceptance_sample"]
    assert "-protocol_whitelist" in calls[0] and "file,pipe" in calls[0]
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(benchmark.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(benchmark, "command", lambda args: "Apple M1" if "machdep.cpu.brand_string" in args else str(16 * 1024**3))
    host = benchmark.host_metadata()
    assert host["classification"] == "16GB_DEVELOPMENT"
    assert not host["acceptance_hardware"]


def test_reports_redact_secrets_and_never_overwrite(tmp_path):
    report = {"result": "FAILED", "error": "secret /private/media.mp4", "manual_quality": "NOT RUN"}
    benchmark.write_reports(tmp_path, report, ["secret", "/private/media.mp4"])
    for name in ("benchmark.json", "benchmark.md"):
        output = tmp_path / name
        assert "secret" not in output.read_text()
        assert "/private/media.mp4" not in output.read_text()
        assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        benchmark.write_reports(tmp_path, report, [])


def test_acceptance_preflight_refuses_upload_on_development_host(monkeypatch, tmp_path):
    media = tmp_path / "private.mp4"
    media.write_bytes(b"fake")
    output = tmp_path / "new-report"
    monkeypatch.setattr(benchmark, "host_metadata", lambda: {"acceptance_hardware": False})
    monkeypatch.setattr(benchmark, "probe_media", lambda *args: {"acceptance_sample": True})
    monkeypatch.setattr(benchmark.getpass, "getpass", lambda *args: pytest.fail("must not request token"))
    assert benchmark.main(["--input", str(media), "--api", "http://127.0.0.1:1234",
                           "--output-dir", str(output), "--acceptance"]) == 1
    report = json.loads((output / "benchmark.json").read_text())
    assert report["error"] == "acceptance_hardware_or_sample_mismatch"


@pytest.mark.parametrize("seconds,width,height,accepted", [
    (3600, 1920, 1080, True), (3599, 1920, 1080, True),
    (3601, 1920, 1080, True), (3598.9, 1920, 1080, False),
    (3601.1, 1920, 1080, False), (3600, 1280, 720, False),
    (3600, 1080, 1920, False),
])
def test_exact_sample_requirements(monkeypatch, tmp_path, seconds, width, height, accepted):
    media = tmp_path / "sample.mp4"
    media.write_bytes(b"fake")
    monkeypatch.setattr(benchmark, "command", lambda args: json.dumps({
        "format": {"duration": seconds}, "streams": [{"width": width, "height": height}],
    }))
    assert benchmark.probe_media(media, "ffprobe")["acceptance_sample"] is accepted


@pytest.mark.parametrize("chip,memory,architecture,accepted", [
    ("Apple M1", 8, "arm64", True), ("Apple M1", 16, "arm64", False),
    ("Apple M1 Pro", 8, "arm64", False), ("Apple M2", 8, "arm64", False),
    ("Apple M1", 8, "x86_64", False),
])
def test_exact_hardware_requirements(monkeypatch, chip, memory, architecture, accepted):
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(benchmark.platform, "machine", lambda: architecture)
    monkeypatch.setattr(benchmark, "command", lambda args: chip if "machdep.cpu.brand_string" in args else str(memory * 1024**3))
    assert benchmark.host_metadata()["acceptance_hardware"] is accepted


def test_task_must_keep_local_hard_bilingual_defaults_and_output_pair(tmp_path):
    raw = task("SUCCESS")
    raw["burn"] = "soft"
    with pytest.raises(benchmark.BenchmarkError, match="task_defaults_mismatch"):
        benchmark.task_snapshot(raw, "task-123")
    raw = task("SUCCESS")
    raw["outputs"].pop("subtitle")
    assert not benchmark.task_snapshot(raw, "task-123")["outputs_present"]
    report, _ = execute(tmp_path, ["EXTRACTING", "TRANSCRIBING", "TRANSLATING", "BURNING", "SUCCESS"])
    report["outputs_present"] = False
    assert benchmark.evaluate(report) == "FAILED"


def test_invalid_json_does_not_escape_response_body():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="secret"))) as client:
        with pytest.raises(benchmark.BenchmarkError, match="^api_invalid_json$"):
            benchmark.api_json(client, "GET", "http://127.0.0.1:1234/api/desktop/status")


def test_input_required_before_any_processing():
    with pytest.raises(SystemExit) as exc:
        benchmark.main([])
    assert exc.value.code == 2
