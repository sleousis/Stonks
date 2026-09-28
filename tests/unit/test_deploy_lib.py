"""deploy/scripts/lib.sh: which services deploy, rollback, backup and
restore stop and start, and which the host check expects to run."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parents[2] / "deploy" / "scripts" / "lib.sh"
BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(
    BASH is None or sys.platform == "win32", reason="needs a POSIX bash"
)


def _run(tmp_path: Path, profiles: str, function: str) -> list[str]:
    (tmp_path / ".env").write_text(f"COMPOSE_PROFILES={profiles}\n", encoding="utf-8")
    out = subprocess.run(
        [BASH, "-c", f'. "$1"; {function}', "lib", str(LIB)],
        env={"DEPLOY_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


@needs_bash
@pytest.mark.parametrize(
    ("profiles", "expected"),
    [
        ("", ["api", "caddy"]),
        ("scheduler", ["api", "caddy", "scheduler"]),
        ("scheduler,lab-worker", ["api", "caddy", "scheduler", "lab-worker"]),
        ("lab-worker,ai", ["api", "caddy", "lab-worker"]),
    ],
)
def test_expected_services_follow_the_profiles(tmp_path, profiles, expected):
    assert _run(tmp_path, profiles, "expected_services") == expected


@needs_bash
def test_the_lab_worker_counts_as_a_writer_of_the_data_volume(tmp_path):
    """It writes job state to state.sqlite, so a snapshot, a migration or a
    restore must not run while it is up."""
    writers = _run(tmp_path, "scheduler,lab-worker", "expected_services | grep -v caddy")
    assert writers == ["api", "scheduler", "lab-worker"]


@needs_bash
def test_deploy_refuses_the_placeholder_api_token(tmp_path):
    """``change-me`` was the example value, and the legacy token signs in as
    the admin. deploy.sh stops before it touches anything."""
    shutil.copytree(LIB.parent, tmp_path / "scripts")
    (tmp_path / ".env").write_text("STONKS_API_TOKEN=change-me\n", encoding="utf-8")
    out = subprocess.run(
        [BASH, str(tmp_path / "scripts" / "deploy.sh"), "v1.0.0"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 2
    assert "STONKS_API_TOKEN" in out.stderr
    assert not (tmp_path / ".previous-tag").exists()


def test_the_example_env_has_no_working_placeholder_credentials():
    example = (LIB.parents[1] / ".env.example").read_text(encoding="utf-8")
    values = dict(
        line.split("=", 1)
        for line in example.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    for key in ("STONKS_API_TOKEN", "RESTIC_PASSWORD", "STONKS_SECRET_KEYS"):
        assert values[key] == "", f"{key} must be empty in the example, not a guessable value"
