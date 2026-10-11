from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migrate_env.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_migration_dry_run_skips_secrets_and_does_not_write(tmp_path):
    env_file = tmp_path / ".env"
    output = tmp_path / "config.local.toml"
    env_file.write_text(
        "SUBTRANS_WORKERS=3\n"
        "SUBTRANS_DL_FORMAT=bv*+ba/b\n"
        "SUBTRANS_DEEPSEEK_API_KEY=secret-value\n",
        encoding="utf-8",
    )

    result = _run("--env-file", str(env_file), "--output", str(output))

    assert result.returncode == 0
    assert "pipeline.workers" in result.stdout
    assert "secret-value" not in result.stdout
    assert not output.exists()


def test_migration_write_is_atomic_and_refuses_overwrite(tmp_path):
    env_file = tmp_path / ".env"
    output = tmp_path / "config.local.toml"
    env_file.write_text("SUBTRANS_WORKERS=3\n", encoding="utf-8")

    first = _run("--env-file", str(env_file), "--output", str(output), "--write")
    assert first.returncode == 0
    assert output.read_text(encoding="utf-8").find("workers = 3") >= 0
    assert oct(output.stat().st_mode & 0o777) == "0o600"

    second = _run("--env-file", str(env_file), "--output", str(output), "--write")
    assert second.returncode == 1
    assert "拒绝覆盖" in second.stderr


def test_migration_reports_unsupported_project_variable(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SUBTRANS_UNKNOWN_SETTING=yes\n", encoding="utf-8")

    result = _run("--env-file", str(env_file), "--output", str(tmp_path / "out.toml"))

    assert result.returncode == 1
    assert "SUBTRANS_UNKNOWN_SETTING" in result.stderr
