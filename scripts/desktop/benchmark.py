#!/usr/bin/env python3
"""Measure a selected local video against an already-running desktop API.

No application imports, model downloads, services, or remote media retrieval.
Polling provides bounded observations, not fabricated server stage timestamps.
"""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import platform
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx


ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = {
    "sourceLang": "en", "targetLang": "zh-CN", "mode": "bilingual",
    "burn": "hard", "model": "local:base.en", "engine": "local-opus-en-zh",
    "needSubtitle": True, "ttsEnabled": False,
}
STATES = ("PENDING", "DOWNLOADING", "EXTRACTING", "TRANSCRIBING",
          "TRANSLATING", "BURNING", "SUCCESS", "FAILED", "CANCELLED")
STAGES = ("TRANSCRIBING", "TRANSLATING", "BURNING")
TERMINAL = {"SUCCESS", "FAILED", "CANCELLED"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


class BenchmarkError(Exception):
    """Only locally defined, non-sensitive error codes may leave the runner."""


def positive_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def validate_origin(origin: str) -> str:
    try:
        parts = urlsplit(origin)
        valid = (
            parts.scheme == "http" and parts.hostname == "127.0.0.1"
            and parts.port is not None and 0 < parts.port <= 65535
            and not parts.username and not parts.password
            and parts.path in {"", "/"} and not parts.query and not parts.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise BenchmarkError("invalid_loopback_origin")
    return origin.rstrip("/")


def command(args: list[str]) -> str:
    try:
        return subprocess.run(
            args, check=True, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise BenchmarkError("metadata_command_failed") from None


def host_metadata() -> dict:
    data = {"system": platform.system(), "architecture": platform.machine(),
            "macos": platform.mac_ver()[0], "chip": None, "memory_bytes": None}
    if data["system"] == "Darwin":
        data["chip"] = command(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"])
        data["memory_bytes"] = int(command(["/usr/sbin/sysctl", "-n", "hw.memsize"]))
    data["acceptance_hardware"] = (
        data["system"] == "Darwin" and data["architecture"] == "arm64"
        and data["chip"] == "Apple M1" and data["memory_bytes"] == 8 * 1024**3
    )
    data["classification"] = (
        "M1_8GB_ACCEPTANCE" if data["acceptance_hardware"] else
        "16GB_DEVELOPMENT" if data["memory_bytes"] == 16 * 1024**3 else
        "NON_ACCEPTANCE_HARDWARE"
    )
    return data


def probe_media(path: Path, ffprobe: str) -> dict:
    # A disguised playlist must not cause ffprobe to fetch remote references.
    raw = command([
        ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
        "-select_streams", "v:0", "-show_entries",
        "format=duration:stream=width,height", "-of", "json", str(path),
    ])
    try:
        data = json.loads(raw)
        duration = float(data["format"]["duration"])
        video = data["streams"][0]
        width, height = int(video["width"]), int(video["height"])
        if not math.isfinite(duration) or duration <= 0 or min(width, height) <= 0:
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError):
        raise BenchmarkError("invalid_video_metadata") from None
    return {
        "duration_seconds": duration, "width": width, "height": height,
        "bytes": path.stat().st_size,
        # A one-second mux/timebase tolerance is explicit, never silently widened.
        "acceptance_sample": abs(duration - 3600) <= 1 and (width, height) == (1920, 1080),
        "duration_tolerance_seconds": 1,
        "english_course_confirmed": False,
    }


def api_json(client: httpx.Client, method: str, path: str, **kwargs):
    try:
        response = client.request(method, path, **kwargs)
    except httpx.TimeoutException:
        raise BenchmarkError("api_request_timeout") from None
    except httpx.HTTPError:
        raise BenchmarkError("api_transport_error") from None
    if not 200 <= response.status_code < 300:
        raise BenchmarkError(f"api_http_{response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise BenchmarkError("api_invalid_json") from None


def desktop_metadata(status: dict) -> dict:
    if not isinstance(status, dict):
        raise BenchmarkError("invalid_desktop_status")
    prepared = status.get("models") or {}
    if not isinstance(prepared, dict) or not status.get("ready") or not prepared.get("ready"):
        raise BenchmarkError("models_not_ready")
    if status.get("active_tasks") != 0:
        raise BenchmarkError("other_tasks_active")
    if status.get("platform") != "macos-arm64" or status.get("defaults") != DEFAULTS:
        raise BenchmarkError("desktop_contract_mismatch")
    rows = prepared.get("models")
    if not isinstance(rows, list):
        raise BenchmarkError("invalid_model_inventory")
    models = []
    for model_id in (DEFAULTS["model"], DEFAULTS["engine"]):
        row = next((r for r in rows if isinstance(r, dict) and r.get("id") == model_id), {})
        if row.get("status") != "ready":
            raise BenchmarkError("models_not_ready")
        revision = row.get("revision")
        models.append({"id": model_id, "revision": revision if isinstance(revision, str)
                       and re.fullmatch(r"[0-9a-f]{40}", revision) else None})
    version = status.get("version")
    version = version if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", version) else None
    return {"version": version, "models": models, "defaults": dict(DEFAULTS)}


def task_snapshot(raw: dict, expected_id: str) -> dict:
    if not isinstance(raw, dict) or raw.get("id") != expected_id or raw.get("status") not in STATES:
        raise BenchmarkError("invalid_task_status")
    if raw.get("sourceType") != "upload" or any(raw.get(k) != v for k, v in DEFAULTS.items()):
        raise BenchmarkError("task_defaults_mismatch")
    # Never retain title, URL, error text, output paths, or arbitrary server fields.
    outputs = raw.get("outputs") or {}
    return {
        "status": raw["status"],
        "created_at_ms": raw.get("createdAt") if type(raw.get("createdAt")) is int else None,
        "updated_at_ms": raw.get("updatedAt") if type(raw.get("updatedAt")) is int else None,
        "outputs_present": isinstance(outputs, dict) and bool(outputs.get("video"))
        and bool(outputs.get("subtitle")) and raw.get("resourceStatus") == "AVAILABLE",
    }


def duration_bounds(start: dict, end: dict) -> dict:
    return {
        "lower_seconds": max(0.0, end["lower"] - start["upper"]),
        "upper_seconds": max(0.0, end["upper"] - start["lower"]),
    }


def summarize_timing(observations: list[dict]) -> dict:
    transitions = {}
    order = []
    previous = None
    valid = True
    for item in observations:
        state = item["status"]
        if previous is None or state != previous["status"]:
            if state in order:
                valid = False
            order.append(state)
            transitions[state] = {
                "lower": previous["request_started_seconds"] if previous else None,
                "upper": item["response_received_seconds"],
            }
        previous = item
    stage_order = [state for state in order if state in STAGES]
    complete = valid and stage_order == list(STAGES) and "SUCCESS" in transitions
    start = transitions.get("TRANSCRIBING", {})
    complete = complete and start.get("lower") is not None
    # DOWNLOADING is never an accepted phase of a local-only benchmark.
    complete = bool(complete and "DOWNLOADING" not in order)
    durations = {}
    for index, stage in enumerate(STAGES):
        end_stage = (*STAGES, "SUCCESS")[index + 1]
        left, right = transitions.get(stage), transitions.get(end_stage)
        durations[stage.lower()] = (
            duration_bounds(left, right)
            if complete and left and right and left["lower"] is not None else None
        )
    total = duration_bounds(transitions["TRANSCRIBING"], transitions["SUCCESS"]) if complete else None
    return {"complete_boundaries": complete, "stages": durations, "total": total,
            "method": "monotonic_poll_intervals", "observed_state_order": order,
            "limit_seconds": 7200,
            "upload_and_download_excluded": True,
            "includes_hard_encode": complete and "BURNING" in order}


def evaluate(report: dict) -> str:
    if report.get("error"):
        return "FAILED"
    if report.get("task_status") != "SUCCESS":
        return "FAILED"
    if not report.get("outputs_present"):
        return "FAILED"
    total = report["timing"]["total"]
    if report.get("clock_discontinuity") or total is None:
        return "INCONCLUSIVE"
    if total["lower_seconds"] > 7200:
        return "FAIL_OVER_7200S"
    if total["upper_seconds"] > 7200:
        return "INCONCLUSIVE_AT_7200S"
    if not report["host"]["acceptance_hardware"] or not report["media"]["acceptance_sample"]:
        return "DEVELOPMENT_ONLY"
    if not report["app"]["version"] or any(not row["revision"] for row in report["app"]["models"]):
        return "INCONCLUSIVE_VERSION_EVIDENCE"
    return "PASS_TIMING_ONLY"


def run_benchmark(
    client: httpx.Client, media_path: Path, report: dict, *,
    poll_interval: float = 0.5, timeout: float = 10800,
    monotonic=time.monotonic, sleep=time.sleep, wall_time=time.time,
) -> dict:
    """Mutate the report incrementally so failure retains observed evidence."""
    report.update(observations=[], manual_quality="NOT RUN", release_acceptance="NOT RUN")
    origin = monotonic()
    wall_origin = wall_time()
    try:
        report["app"] = desktop_metadata(api_json(client, "GET", "/api/desktop/status"))
        form = {k: str(v).lower() if isinstance(v, bool) else v for k, v in DEFAULTS.items()}
        upload_started = monotonic()
        with media_path.open("rb") as handle:
            # Do not copy the selected user's filename into task history.
            raw = api_json(client, "POST", "/api/tasks/upload", data=form,
                           files={"file": ("benchmark-input" + media_path.suffix.lower(), handle, "application/octet-stream")})
        upload_finished = monotonic()
        report["upload_request_seconds_excluded"] = upload_finished - upload_started
        task_id = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id):
            raise BenchmarkError("invalid_task_id")
        report["task_id"] = task_id
        task_snapshot(raw, task_id)
        # The upload route enqueues before returning. Only subsequent GETs give
        # a baseline guaranteed to exclude the entire upload HTTP transaction.
        deadline = upload_finished + timeout
        while True:
            started = monotonic()
            remaining = deadline - started
            if remaining <= 0:
                raise BenchmarkError("poll_timeout_task_may_still_be_running")
            raw = api_json(client, "GET", f"/api/tasks/{task_id}", timeout=min(30, remaining))
            ended = monotonic()
            snapshot = task_snapshot(raw, task_id)
            snapshot.update(request_started_seconds=started - upload_finished,
                            response_received_seconds=ended - upload_finished)
            report["observations"].append(snapshot)
            report["task_status"] = snapshot["status"]
            report["outputs_present"] = snapshot["outputs_present"]
            # Suspend / wall-clock adjustments invalidate a clean timing run.
            if abs((wall_time() - wall_origin) - (ended - origin)) > 5:
                report["clock_discontinuity"] = True
            report["post_upload_observation_seconds"] = ended - upload_finished
            if ended > deadline:
                raise BenchmarkError("poll_timeout_task_may_still_be_running")
            if snapshot["status"] in TERMINAL:
                if snapshot["status"] != "SUCCESS":
                    raise BenchmarkError("task_" + snapshot["status"].lower())
                break
            sleep(min(poll_interval, max(0, deadline - monotonic())))
    except BenchmarkError as exc:
        report["error"] = str(exc)
    except KeyboardInterrupt:
        report["error"] = "interrupted_task_may_still_be_running"
    except (OSError, ValueError, TypeError, KeyError):
        report["error"] = "local_io_or_invalid_response"
    report["timing"] = summarize_timing(report["observations"])
    report["result"] = evaluate(report)
    return report


def markdown_report(report: dict) -> str:
    total = report.get("timing", {}).get("total")
    duration = f"{total['lower_seconds']:.3f}–{total['upper_seconds']:.3f} s" if total else "unavailable / incomplete boundaries"
    lines = ["# Desktop local-video benchmark", "", f"Result: **{report['result']}**",
             f"Processing interval (transcription through hard encode): {duration}; limit 7200 s.",
             f"Hardware: {report.get('host', {}).get('classification', 'UNKNOWN')}",
             f"Error: {report.get('error', 'none')}", "",
             "| Stage | Lower seconds | Upper seconds |", "| --- | ---: | ---: |"]
    for name, value in report.get("timing", {}).get("stages", {}).items():
        lines.append(f"| {name} | {value['lower_seconds']:.3f} | {value['upper_seconds']:.3f} |" if value else f"| {name} | unknown | unknown |")
    lines += ["", "Upload, model preparation and media downloads are excluded. Polling bounds are not exact stage timestamps.",
              "Manual quality / playback / full-span review: **NOT RUN**. Release acceptance: **NOT RUN**.",
              "Non-M1/8GB hardware and non-60-minute/1080p inputs are development evidence only.",
              "No backend peak-memory measurement is provided; retain separate Activity Monitor evidence."]
    return "\n".join(lines) + "\n"


def redact(value, secrets: list[str]):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    return value


def write_reports(directory: Path, report: dict, secrets: list[str]) -> None:
    clean = redact(report, secrets)
    for name, content in (("benchmark.json", json.dumps(clean, indent=2, allow_nan=False) + "\n"),
                          ("benchmark.md", markdown_report(clean))):
        # Refuse overwrite and symlinks; reports stay local and owner-readable.
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--api", required=True, help="http://127.0.0.1:<running desktop port>")
    parser.add_argument("--output-dir", required=True, type=Path, help="new directory outside the repository")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--poll-interval", type=positive_number, default=0.5)
    parser.add_argument("--timeout", type=positive_number, default=10800, help="post-upload polling deadline in seconds")
    parser.add_argument("--upload-timeout", type=positive_number, default=600, help="HTTP per-operation upload timeout")
    parser.add_argument("--acceptance", action="store_true", help="refuse upload on non-M1/8GB or non-60min/1080p input")
    args = parser.parse_args(argv)
    token = os.environ.get("SUBTRANS_DESKTOP_SESSION_TOKEN", "")
    report = {"schema_version": 1, "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "manual_quality": "NOT RUN", "release_acceptance": "NOT RUN"}
    destination = args.output_dir.expanduser().resolve()
    media_path = args.input.expanduser().resolve()
    try:
        if destination.is_relative_to(ROOT) or media_path.is_relative_to(ROOT):
            raise BenchmarkError("samples_and_reports_must_be_outside_repository")
        # Reserve output before any task is submitted, never overwrite evidence.
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    except (OSError, BenchmarkError):
        print("FAILED: output directory must be new and samples/reports outside the repository")
        return 1
    try:
        origin = validate_origin(args.api)
        if not media_path.is_file() or media_path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise BenchmarkError("selected_local_video_required")
        report["host"] = host_metadata()
        report["media"] = probe_media(media_path, args.ffprobe)
        if args.acceptance and not (report["host"]["acceptance_hardware"] and report["media"]["acceptance_sample"]):
            raise BenchmarkError("acceptance_hardware_or_sample_mismatch")
        if not token:
            token = getpass.getpass("Desktop session token (not saved): ")
        if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise BenchmarkError("invalid_session_token")
        with httpx.Client(
            base_url=origin, headers={"X-API-Token": token},
            follow_redirects=False, trust_env=False, timeout=args.upload_timeout,
        ) as client:
            run_benchmark(client, media_path, report, poll_interval=args.poll_interval, timeout=args.timeout)
    except BenchmarkError as exc:
        report.update(error=str(exc), result="FAILED")
    except (OSError, ValueError, EOFError, KeyboardInterrupt):
        report.update(error="local_preflight_or_interruption", result="FAILED")
    try:
        write_reports(destination, report, [token, str(media_path), media_path.name])
    except OSError:
        print("FAILED: could not write benchmark reports; any submitted task may still be running")
        return 1
    print(f"{report['result']}; JSON and Markdown reports written; manual acceptance NOT RUN")
    return 0 if report["result"] in {"PASS_TIMING_ONLY", "DEVELOPMENT_ONLY"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
