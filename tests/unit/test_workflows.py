"""GitHub Actions hygiene for every workflow (Phase 12.8, TT-13)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).parents[2] / ".github" / "workflows").glob("*.yml"))
#: Actions published by GitHub itself; everything else is pinned to a commit.
FIRST_PARTY = ("actions/", "github/")
SHA = re.compile(r"^[0-9a-f]{40}$")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _uses(workflow: dict) -> list[str]:
    return [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_starts_from_a_read_only_token(path):
    """A top-level block, so a job without its own gets read access only,
    whatever the repository default is. Jobs widen it one by one."""
    permissions = _load(path).get("permissions")
    assert isinstance(permissions, dict), f"{path.name}: add a top-level permissions block"
    assert permissions.get("contents") == "read"
    assert set(permissions.values()) == {"read"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_third_party_actions_are_pinned_to_a_commit(path):
    """A moved tag on a third-party action would run new code with this
    repository's secrets and write tokens. Dependabot bumps the pins."""
    for uses in _uses(_load(path)):
        if uses.startswith(FIRST_PARTY):
            continue
        ref = uses.rsplit("@", 1)[1]
        assert SHA.match(ref), f"{path.name}: pin {uses} to a full commit SHA"
