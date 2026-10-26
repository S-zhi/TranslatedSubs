"""Exercise installer argument validation without root or system changes."""

import os
from pathlib import Path
import subprocess

import pytest


INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


def run_validation(tmp_path, *args, **overrides):
    source = INSTALLER.read_text(encoding="utf-8")
    # Stop before platform checks or any installation. Bypass only the root gate
    # so the same validation can run on developer machines and in CI.
    source = source.split('PROJECT_DIR="$(detect_project_dir)"', 1)[0]
    source = source.replace(
        '[[ "${EUID}" -eq 0 ]] || die "请使用 sudo 运行：sudo bash install.sh"',
        ":",
    )
    script = tmp_path / "validate-install.sh"
    script.write_text(source + '\nprintf "validation complete\\n"\n', encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("SUBTRANS_")}
    env.update(overrides)
    return subprocess.run(
        ["bash", str(script), *args], env=env, capture_output=True, text=True,
        check=False,
    )


@pytest.mark.parametrize("args,env", [
    ((), {}),
    (("--with-cloud-trial",), {}),
    (("--with-cloud-trial", "--no-start"), {}),
    ((), {"SUBTRANS_ENABLE_CLOUD_TRIAL": "1"}),
])
def test_installer_accepts_default_and_explicit_trial_options(tmp_path, args, env):
    result = run_validation(tmp_path, *args, **env)
    assert result.returncode == 0, result.stderr
    assert "validation complete" in result.stdout


@pytest.mark.parametrize("overrides,message", [
    ({"SUBTRANS_LAUNCHER_PORT": "8000"}, "不能与 SUBTRANS_API_PORT 相同"),
    ({"SUBTRANS_LAUNCHER_DATA_DIR": "/var/lib"}, "数据目录层级过浅"),
    ({"SUBTRANS_LAUNCHER_ENV_FILE": "/tmp/launcher env"}, "包含不支持的字符"),
])
def test_installer_rejects_invalid_trial_configuration(tmp_path, overrides, message):
    result = run_validation(tmp_path, "--with-cloud-trial", **overrides)
    assert result.returncode != 0
    assert message in result.stderr
