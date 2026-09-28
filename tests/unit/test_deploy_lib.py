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


FAKE_DOCKER = """#!/usr/bin/env bash
# Records every call. "compose up" always fails (a broken release and a
# broken rollback); a snapshot tar leaves an empty file behind.
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$*" in
*" up "*) exit 1 ;;
*"compose"*" ps "*) exit 0 ;;
inspect*) exit 1 ;;
*"tar czf /out/"*)
	name="${*##*tar czf /out/}"
	touch "$FAKE_SNAPDIR/${name%% *}"
	;;
esac
exit 0
"""


@needs_bash
def test_a_failed_rollback_still_reports_its_outcome(tmp_path):
    """The new tag fails to start and so does the previous one. The rollback
    must still reach its health check and say the stack is not healthy,
    instead of dying on the failed ``compose up`` without a word."""
    deploy_dir = tmp_path / "deploy"
    shutil.copytree(LIB.parent, deploy_dir / "scripts")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in {
        "docker": FAKE_DOCKER,
        "curl": "#!/bin/sh\nexit 7\n",
        "sleep": "#!/bin/sh\nexit 0\n",
        "crontab": "#!/bin/sh\nexit 0\n",
    }.items():
        (fake_bin / name).write_text(body, encoding="utf-8", newline="\n")
        (fake_bin / name).chmod(0o755)
    data = tmp_path / "srv" / "data"
    snapdir = tmp_path / "srv" / "snapshots"
    snapdir.mkdir(parents=True)
    (deploy_dir / ".env").write_text(
        f"STONKS_IMAGE=stonks\nSTONKS_IMAGE_TAG=v1.0.0\nSTONKS_DATA_PATH={data}\n"
        "COMPOSE_PROFILES=scheduler\nSTONKS_API_TOKEN=x\n",
        encoding="utf-8",
    )
    (deploy_dir / ".deployed-tag").write_text("v1.0.0\n", encoding="utf-8")
    log = tmp_path / "docker.log"
    out = subprocess.run(
        [BASH, str(deploy_dir / "scripts" / "deploy.sh"), "v1.1.0"],
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "FAKE_LOG": str(log),
            "FAKE_SNAPDIR": str(snapdir),
            "HOME": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 1
    assert "rollback to v1.0.0 is NOT healthy" in out.stderr
    assert "STONKS_IMAGE_TAG=v1.0.0" in (deploy_dir / ".env").read_text(encoding="utf-8")
    calls = log.read_text(encoding="utf-8")
    assert calls.count(" up -d") == 2  # the new tag, then the rollback
