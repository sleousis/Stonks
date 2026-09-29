"""The GitHub deploy workflow (roadmap 14.4): what it copies to the server
and what it must leave alone there."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"


def _steps() -> list[dict]:
    workflow = yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["deploy"]["steps"]


def _rsync_command() -> str:
    runs = [step.get("run", "") for step in _steps()]
    found = [run for run in runs if "rsync" in run]
    assert len(found) == 1, "expected one rsync step"
    return " ".join(found[0].replace("\\\n", " ").split())


def test_the_copy_keeps_the_server_only_files():
    command = _rsync_command()
    assert "--delete" in command
    for kept in (".env", ".deployed-tag", ".previous-tag", "snapshots/"):
        assert f"--exclude {kept}" in command


def test_the_copy_never_deletes_the_ibkr_secret_files():
    """The IBKR credentials live only on the server (git ignores them), so
    ``rsync --delete`` would remove them on every deploy and the gateway
    profiles could no longer start."""
    command = _rsync_command()
    assert "--filter 'protect /ibkr/secrets/*'" in command
