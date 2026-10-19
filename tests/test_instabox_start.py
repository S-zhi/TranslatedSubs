"""Private VM bootstrap regressions without cloud, network, or Docker workloads."""

from __future__ import annotations

import copy
import json
import logging
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "src/instabox_launcher/bootstrap.sh"
TRIAL_ID = "a" * 32
REVISION = "b" * 40
TOKEN = "c" * 64
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="Bash is required")

FAKE_COMMAND = r'''
import json
import os
from pathlib import Path
import re
import sys
import types

name = Path(sys.argv[0]).name
args = sys.argv[1:]
workspace = Path(os.environ["SUBTRANS_TRIAL_WORKSPACE"])
trial = workspace / ("translatedsubs-trial-" + os.environ["SUBTRANS_TRIAL_ID"])

def record(**details):
    event = {"name": name, "args": args, **details}
    event["token_in_environment"] = bool(os.environ.get("SUBTRANS_TRIAL_TOKEN"))
    status = trial / "status"
    event["status"] = status.read_text().strip() if status.exists() else None
    with Path(os.environ["BOOTSTRAP_COMMANDS"]).open("a") as output:
        output.write(json.dumps(event) + "\n")

if name == "git":
    assert args[0] == "-C"
    repo = Path(args[1])
    command = args[2:]
    record()
    if command[0] == "init":
        (repo / ".git").mkdir(exist_ok=True)
    elif command[0] == "fetch":
        assert command[-2:] == [
            "https://github.com/S-zhi/TranslatedSubs.git",
            os.environ["SUBTRANS_TRIAL_REVISION"],
        ]
        print("Fetching pinned source")
        sys.exit(int(os.environ.get("FETCH_EXIT", "0")))
    elif command[0] == "checkout":
        feature = os.environ.get("SOURCE_FEATURE", "current")
        sources = {
            "Dockerfile": "FROM python:3.12-slim\n",
            "compose.yaml": "services: {}\n",
            "source.py": "pass\n",
            "web/js/api-auth.js": (
                "export function consumeTrialTokenFragment() {}\n"
                if feature != "legacy_browser" else "export const oldAuth = {};\n"
            ),
            "web/js/main.js": (
                'import { consumeTrialTokenFragment } from "./api-auth.js";\n'
                "consumeTrialTokenFragment(Api.base);\n"
                if feature != "legacy_main" else "export const oldMain = {};\n"
            ),
            "web/config.js": (
                'const token = "trial_token";\n'
                if feature != "legacy_config_js" else "const api = true;\n"
            ),
            "src/config/config_repository.py": (
                "def initialize_config_document(): pass\n"
                if feature != "legacy_config" else "def get_config_provider(): pass\n"
            ),
            "src/config/schema.py": (
                "startup_probe_enabled = True\napi_token = None\n"
                if feature != "legacy_probe" else "api_token = None\n"
            ),
        }
        for relative, content in sources.items():
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    elif command[0] == "rev-parse":
        head = os.environ.get("HEAD_REVISION", os.environ["SUBTRANS_TRIAL_REVISION"])
        record(head_revision=head)
        print(head)
    elif command[0] == "clean":
        for leftover in [".env", "snapshot-credentials", "config.local.json"]:
            (repo / leftover).unlink(missing_ok=True)
    elif command[0] == "grep":
        pattern = command[2]
        target = repo / command[-1]
        sys.exit(0 if target.exists() and pattern in target.read_text() else 1)
    else:
        raise AssertionError(command)
elif name == "docker":
    record()
    if args[0] == "info" or args == ["compose", "version"]:
        sys.exit(0)
    assert args[0] == "compose"
    assert args[args.index("--project-name") + 1] == trial.name
    assert args[args.index("--env-file") + 1] == "/dev/null"
    command = args[args.index("--file") + 2:]
    if command == ["build", "app"]:
        print("Docker build output stays on the VM")
        sys.exit(int(os.environ.get("BUILD_EXIT", "0")))
    if command[:2] == ["exec", "-T"]:
        sys.exit(int(os.environ.get("REDIS_EXIT", "0")))
    if command[0] == "run":
        sys.exit(int(os.environ["INIT_EXIT"])) if os.environ.get("INIT_EXIT") else None
        token = os.environ["SUBTRANS_TRIAL_TOKEN"]
        assert re.fullmatch("[0-9a-f]{64}", token)
        assert command[command.index("-e") + 1] == "SUBTRANS_TRIAL_TOKEN"
        assert "--service-ports" not in command
        state_path = trial / "fake-runtime.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else None
        order = []
        class FreshProvider:
            def mutate(self, callback):
                global state
                order.append("mutate")
                if state is None:
                    raise RuntimeError("Fresh Redis configuration is missing")
                state = callback(state)
        provider = FreshProvider()
        def initialize_config_document():
            global state
            order.append("initialize")
            if state is None:
                state = {"business": {}}
        def get_config_provider():
            order.append("provider")
            return provider
        for module_name in ["src", "src.config", "src.config.config_repository"]:
            sys.modules[module_name] = types.ModuleType(module_name)
        module = sys.modules["src.config.config_repository"]
        module.initialize_config_document = initialize_config_document
        module.get_config_provider = get_config_provider
        exec(compile(command[-1], "bootstrap-init", "exec"), {})
        assert state["business"]["api_token"] == token
        assert state["business"]["startup_probe_enabled"] is False
        state_path.write_text(json.dumps(state))
        record(initialization_order=order)
elif name == "curl":
    header = None
    if "--header" in args:
        reference = args[args.index("--header") + 1]
        assert reference.startswith("@")
        header = Path(reference[1:])
        token = json.loads((trial / "fake-runtime.json").read_text())["business"]["api_token"]
        assert header.read_text() == "Authorization: Bearer " + token + "\n"
    record(header_mode=header.stat().st_mode & 0o777 if header else None)
    assert args[0] == "--disable"
    assert "--max-time" in args and "--connect-timeout" in args
    assert args[-1].startswith("http://127.0.0.1:8000/api/")
    assert "?" not in args[-1]
    if args[-1].endswith("/health"):
        code = os.environ.get("HEALTH_STATUS", "200")
    elif header:
        code = os.environ.get("AUTHENTICATED_STATUS", "200")
    else:
        code = os.environ.get("UNAUTHENTICATED_STATUS", "401")
    print(code, end="")
    sys.exit(int(os.environ.get("CURL_EXIT", "0")))
elif name == "sleep":
    pass
else:
    record()
    raise AssertionError("Unexpected external command: " + name)
'''


@pytest.fixture
def bootstrap_machine(tmp_path):
    tools = tmp_path / "bin"
    tools.mkdir()
    for command in ("git", "docker", "curl", "flock", "sleep", "instabox-port"):
        executable = tools / command
        executable.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND)
        executable.chmod(0o755)
    # A closed PATH prevents a missing fake tool from invoking the real command.
    for command in ("mkdir", "chmod", "mktemp", "mv", "rm", "cat"):
        tools.joinpath(command).symlink_to(shutil.which(command))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return SimpleNamespace(
        tools=tools, workspace=workspace, log=tmp_path / "commands.jsonl",
    )


def run_bootstrap(machine, **overrides):
    environment = {
        **os.environ,
        "PATH": str(machine.tools),
        "SUBTRANS_TRIAL_WORKSPACE": str(machine.workspace),
        "SUBTRANS_TRIAL_ID": TRIAL_ID,
        "SUBTRANS_TRIAL_REVISION": REVISION,
        "SUBTRANS_TRIAL_TOKEN": TOKEN,
        "BOOTSTRAP_COMMANDS": str(machine.log),
        **overrides,
    }
    before = len(machine.log.read_text().splitlines()) if machine.log.exists() else 0
    result = subprocess.run(
        [BASH, str(SCRIPT)], env=environment, capture_output=True, text=True,
        timeout=30,
    )
    events = [
        json.loads(line) for line in machine.log.read_text().splitlines()[before:]
    ] if machine.log.exists() else []
    trial_dir = machine.workspace / f"translatedsubs-trial-{environment['SUBTRANS_TRIAL_ID']}"
    return result, events, trial_dir


def compose_command(event):
    args = event["args"]
    if event["name"] != "docker" or "--file" not in args:
        return []
    return args[args.index("--file") + 2:]


def assert_failed(result, trial_dir):
    assert result.returncode != 0
    assert result.stdout == result.stderr == ""
    assert (trial_dir / "status").read_text() == "failed\n"
    assert not (trial_dir / "auth-header").exists()


def test_private_bootstrap_pins_source_initializes_auth_and_checks_both_access_paths(bootstrap_machine):
    result, events, trial_dir = run_bootstrap(bootstrap_machine)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert (trial_dir / "status").read_text() == "ready\n"

    fetch, = [event for event in events if event["name"] == "git" and "fetch" in event["args"]]
    assert fetch["args"][-3:] == [
        "1", "https://github.com/S-zhi/TranslatedSubs.git", REVISION,
    ]
    assert any(event["args"][-4:] == ["checkout", "--detach", "--force", "FETCH_HEAD"]
               for event in events)
    assert any(event["args"][-2:] == ["clean", "-ffdx"] for event in events)
    assert all("clone" not in event["args"] and "main" not in event["args"] for event in events)
    assert (trial_dir / "source-revision").read_text().strip() == REVISION

    initialize = next(index for index, event in enumerate(events)
                      if compose_command(event)[:1] == ["run"])
    start = next(index for index, event in enumerate(events)
                 if compose_command(event) == ["up", "-d", "app"])
    checks = [index for index, event in enumerate(events) if event["name"] == "curl"]
    assert initialize < start < checks[0]
    assert [events[index]["args"][-1] for index in checks] == [
        "http://127.0.0.1:8000/api/health",
        "http://127.0.0.1:8000/api/settings/runtime",
        "http://127.0.0.1:8000/api/settings/runtime",
    ]
    assert events[checks[1]]["header_mode"] is None
    assert events[checks[2]]["header_mode"] == 0o600
    assert all(events[index]["status"] == "checking" for index in checks)
    initialization, = [event for event in events if "initialization_order" in event]
    assert initialization["initialization_order"] == ["initialize", "provider", "mutate"]

    for path in (trial_dir / "status", trial_dir / "bootstrap.log", trial_dir / "source-revision"):
        assert path.stat().st_mode & 0o777 == 0o600
    assert trial_dir.stat().st_mode & 0o777 == 0o700
    assert (trial_dir / "repo/source.py").stat().st_mode & 0o777 == 0o644
    config_path = trial_dir / "repo/config.local.json"
    assert config_path.stat().st_mode & 0o777 == 0o644
    config = json.loads(config_path.read_text())
    assert set(config) == {"redis", "paths", "http"}
    assert config["redis"] == {"url": "redis://redis:6379/0", "namespace": trial_dir.name}
    assert config["http"] == {"host": "0.0.0.0", "port": 8000}
    assert TOKEN not in json.dumps(events)
    assert TOKEN not in (trial_dir / "bootstrap.log").read_text()
    assert TOKEN not in config_path.read_text()
    assert not (trial_dir / "auth-header").exists()
    assert not any(event["name"] == "instabox-port" for event in events)
    assert all(not event["token_in_environment"] for event in events
               if event["name"] != "docker" or compose_command(event)[:1] != ["run"])


@pytest.mark.parametrize("status_options", [
    {"UNAUTHENTICATED_STATUS": "200"},
    {"UNAUTHENTICATED_STATUS": "503"},
    {"AUTHENTICATED_STATUS": "401"},
    {"AUTHENTICATED_STATUS": "503"},
    {"CURL_EXIT": "28"},
])
def test_private_bootstrap_requires_health_and_auth_gates(bootstrap_machine, status_options):
    result, events, trial_dir = run_bootstrap(bootstrap_machine, **status_options)
    assert_failed(result, trial_dir)
    assert compose_command(events[-1]) == ["stop", "--timeout", "10", "app"]
    assert all(event["name"] != "instabox-port" for event in events)


def test_private_bootstrap_does_not_treat_other_success_codes_as_healthy(bootstrap_machine):
    result, events, trial_dir = run_bootstrap(bootstrap_machine, HEALTH_STATUS="202")
    assert_failed(result, trial_dir)
    checks = [event for event in events if event["name"] == "curl"]
    assert len(checks) > 1
    assert all(event["args"][-1].endswith("/health") for event in checks)


@pytest.mark.parametrize("failure", ["BUILD_EXIT", "REDIS_EXIT", "INIT_EXIT"])
def test_private_bootstrap_stops_before_http_when_preparation_fails(bootstrap_machine, failure):
    result, events, trial_dir = run_bootstrap(bootstrap_machine, **{failure: "1"})
    assert_failed(result, trial_dir)
    assert not any(compose_command(event) == ["up", "-d", "app"] for event in events)
    assert not any(event["name"] == "curl" for event in events)
    if failure == "BUILD_EXIT":
        assert not any(compose_command(event)[:1] == ["run"] for event in events)
    assert "Docker build output stays on the VM" in (trial_dir / "bootstrap.log").read_text()


@pytest.mark.parametrize("feature", [
    "legacy_browser", "legacy_main", "legacy_config_js", "legacy_config", "legacy_probe",
])
def test_private_bootstrap_rejects_source_without_trial_handoff_and_config_support(bootstrap_machine, feature):
    result, events, trial_dir = run_bootstrap(bootstrap_machine, SOURCE_FEATURE=feature)
    assert_failed(result, trial_dir)
    assert not any(compose_command(event) == ["build", "app"] for event in events)
    assert not any(compose_command(event) == ["up", "-d", "app"] for event in events)


@pytest.mark.parametrize("options", [
    {"HEAD_REVISION": "d" * 40},
    {"FETCH_EXIT": "1"},
])
def test_private_bootstrap_rejects_failed_or_mismatched_checkout(bootstrap_machine, options):
    result, events, trial_dir = run_bootstrap(bootstrap_machine, **options)
    assert_failed(result, trial_dir)
    assert not any(compose_command(event) == ["build", "app"] for event in events)


@pytest.mark.parametrize("options", [
    {"SUBTRANS_TRIAL_REVISION": "main"},
    {"SUBTRANS_TRIAL_REVISION": "b" * 39},
    {"SUBTRANS_TRIAL_TOKEN": "c" * 63},
    {"SUBTRANS_TRIAL_TOKEN": "token\nunsafe"},
])
def test_private_bootstrap_rejects_invalid_revision_or_token(bootstrap_machine, options):
    result, events, trial_dir = run_bootstrap(bootstrap_machine, **options)
    assert_failed(result, trial_dir)
    assert not events


@pytest.mark.parametrize("trial_id", ["../escape", "a" * 31, "a" * 33])
def test_private_bootstrap_rejects_invalid_trial_path_before_making_state(bootstrap_machine, trial_id):
    result, events, _ = run_bootstrap(bootstrap_machine, SUBTRANS_TRIAL_ID=trial_id)
    assert result.returncode != 0
    assert result.stdout == result.stderr == ""
    assert not events
    assert not list(bootstrap_machine.workspace.iterdir())


def test_private_bootstrap_checks_flock_before_any_workload(bootstrap_machine):
    (bootstrap_machine.tools / "flock").unlink()
    result, events, trial_dir = run_bootstrap(bootstrap_machine)
    assert_failed(result, trial_dir)
    assert not events


def test_private_bootstrap_rerun_repairs_same_project_before_restarting_http(bootstrap_machine):
    first, first_events, trial_dir = run_bootstrap(bootstrap_machine)
    assert first.returncode == 0
    repo = trial_dir / "repo"
    (repo / ".env").write_text("API_TOKEN=snapshot-token")
    (repo / "snapshot-credentials").write_text("old-provider-key")
    (repo / "config.local.json").write_text('{"api_token":"snapshot-token"}')

    second, events, resumed_dir = run_bootstrap(
        bootstrap_machine, SUBTRANS_TRIAL_TOKEN="d" * 64,
    )
    assert second.returncode == 0
    assert resumed_dir == trial_dir
    assert list(bootstrap_machine.workspace.iterdir()) == [trial_dir]
    stop = next(index for index, event in enumerate(events)
                if compose_command(event) == ["stop", "--timeout", "10", "app"])
    checkout = next(index for index, event in enumerate(events)
                    if event["name"] == "git" and "checkout" in event["args"])
    initialize = next(index for index, event in enumerate(events)
                      if compose_command(event)[:1] == ["run"])
    start = next(index for index, event in enumerate(events)
                 if compose_command(event) == ["up", "-d", "app"])
    assert stop < checkout < initialize < start
    assert not (repo / ".env").exists()
    assert not (repo / "snapshot-credentials").exists()
    assert "snapshot-token" not in (repo / "config.local.json").read_text()
    runtime = json.loads((trial_dir / "fake-runtime.json").read_text())
    assert runtime["business"]["api_token"] == "d" * 64
    projects = {
        event["args"][event["args"].index("--project-name") + 1]
        for event in first_events + events if compose_command(event)
    }
    assert projects == {trial_dir.name}


def test_private_bootstrap_does_not_repurpose_existing_trial_for_another_revision(bootstrap_machine):
    first, _, trial_dir = run_bootstrap(bootstrap_machine)
    assert first.returncode == 0
    result, events, _ = run_bootstrap(bootstrap_machine, SUBTRANS_TRIAL_REVISION="d" * 40)
    assert_failed(result, trial_dir)
    assert not any(event["name"] == "git" for event in events)
    assert not any(compose_command(event) == ["build", "app"] for event in events)
    assert (trial_dir / "source-revision").read_text().strip() == REVISION


def test_private_bootstrap_uses_isolated_named_volumes_and_no_cloud_credentials(bootstrap_machine):
    result, _, trial_dir = run_bootstrap(bootstrap_machine)
    assert result.returncode == 0
    compose = (trial_dir / "compose.trial.yaml").read_text()
    assert "build: ./repo" in compose
    assert "translatedsubs-data:/data" in compose
    assert "translatedsubs-redis:/data" in compose
    assert "container_name:" not in compose
    assert "name:" not in compose
    assert "external:" not in compose
    assert "env_file:" not in compose
    assert "http-entrypoint.py:/app/http-entrypoint.py:ro" in compose
    assert all(name not in SCRIPT.read_text() for name in (
        "INSTABOX_API_KEY", "instabox-port", "--service-ports", "--no-access-log",
    ))


def test_embedded_initializer_initializes_actual_fresh_config_provider_before_mutation(bootstrap_machine, monkeypatch):
    from src.config import config_repository
    from src.config.redis_store import ConfigSnapshot, ConfigUnavailable

    result, events, _ = run_bootstrap(bootstrap_machine)
    assert result.returncode == 0
    initialization = next(event for event in events if compose_command(event)[:1] == ["run"])
    code = initialization["args"][-1]

    class FreshConfigStore:
        def __init__(self):
            self.snapshot = None
            self.markers = {}
            self.client = SimpleNamespace(
                get=self.markers.get,
                set=lambda key, value, nx=False: self.markers.setdefault(key, value),
            )

        def _key(self, name):
            return f"fresh:{name}"

        def get(self, _name):
            return self.snapshot

        def set(self, _name, values, *, expected_revision, updated_by):
            revision = self.snapshot.revision if self.snapshot else 0
            assert expected_revision == revision
            self.snapshot = ConfigSnapshot(
                schema=1, revision=revision + 1, values=copy.deepcopy(values),
                updated_at=0, updated_by=updated_by,
            )
            return self.snapshot

    provider = config_repository.RedisConfigProvider(FreshConfigStore())
    with pytest.raises(ConfigUnavailable, match="missing"):
        provider.mutate(lambda document: document)
    monkeypatch.setattr(config_repository, "_provider", provider)
    monkeypatch.setenv("SUBTRANS_TRIAL_TOKEN", TOKEN)
    exec(compile(code, "bootstrap-init", "exec"), {})
    assert provider.read().values["business"]["api_token"] == TOKEN
    assert provider.read().values["business"]["startup_probe_enabled"] is False
    assert provider.read().revision == 2
    assert provider.store.markers == {"fresh:initialized": "1"}


@pytest.mark.parametrize("query", [
    "token=secret&normal=ok",
    "api_token=secret&normal=ok",
    "API_TOKEN=secret&token=other-secret&normal=ok",
    "%74oken=secret&normal=ok",
    "trial_token=secret&normal=ok",
])
def test_generated_http_entrypoint_redacts_query_tokens_and_preserves_access_audit(bootstrap_machine, monkeypatch, query):
    import uvicorn

    result, _, trial_dir = run_bootstrap(bootstrap_machine)
    assert result.returncode == 0
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    runpy.run_path(str(trial_dir / "http-entrypoint.py"), run_name="__main__")
    (target, options), = calls
    assert target == ("src.handler.app:app",)
    assert options["access_log"] is True
    assert options["host"] == "0.0.0.0" and options["port"] == 8000
    handler = options["log_config"]["handlers"]["access"]
    assert "redact_trial_tokens" in handler["filters"]
    filter_type = options["log_config"]["filters"]["redact_trial_tokens"]["()"]
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "test", 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", f"/api/tasks?{query}", "1.1", 200), None,
    )
    assert filter_type().filter(record) is True
    message = record.getMessage()
    assert "secret" not in message
    assert "[REDACTED]" in message and "normal=ok" in message
    assert record.args[:2] == ("127.0.0.1:1234", "GET")
    assert record.args[3:] == ("1.1", 200)
    assert '/api/tasks?' in message and message.endswith('HTTP/1.1" 200')
