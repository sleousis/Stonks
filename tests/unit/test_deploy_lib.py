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

pytestmark = pytest.mark.skipif(
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


def test_the_lab_worker_counts_as_a_writer_of_the_data_volume(tmp_path):
    """It writes job state to state.sqlite, so a snapshot, a migration or a
    restore must not run while it is up."""
    writers = _run(tmp_path, "scheduler,lab-worker", "expected_services | grep -v caddy")
    assert writers == ["api", "scheduler", "lab-worker"]
