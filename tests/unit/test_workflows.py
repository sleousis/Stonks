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


#: ``uses: owner/repo@<sha> # v1.2.3`` on one line.
PINNED_LINE = re.compile(r"uses:\s*(?P<action>[^@\s]+)@(?P<ref>\S+)(?P<rest>.*)$")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_pin_names_its_tag(path):
    """A bare SHA says nothing to a reviewer. Each pin keeps the release tag
    it points at in a trailing comment, which Dependabot updates with it."""
    for line in path.read_text(encoding="utf-8").splitlines():
        m = PINNED_LINE.search(line)
        if not m or m["action"].startswith(FIRST_PARTY):
            continue
        assert re.search(r"#\s*v\d", m["rest"]), f"{path.name}: name the tag of {m['action']}"


def test_a_release_only_ships_commits_that_are_on_main():
    """The Release run feeds the automatic deploy, so a tag on an unmerged
    branch must fail before anything is built."""
    release = _load(Path(__file__).parents[2] / ".github" / "workflows" / "release.yml")
    steps = release["jobs"]["verify"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
    assert any("merge-base --is-ancestor" in step.get("run", "") for step in steps)


ROOT = Path(__file__).parents[2]


def _node_versions_in_ci() -> set[str]:
    versions: set[str] = set()
    for path in WORKFLOWS:
        for job in _load(path)["jobs"].values():
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("actions/setup-node@"):
                    versions.add(str(step["with"]["node-version"]))
    return versions


def test_ci_builds_the_console_on_the_node_major_the_image_ships():
    """The Docker console stage and every CI web job run one Node major, so
    a change in npm, esbuild or the Angular builder shows up in CI first."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"^FROM\s+(?:--platform=\S+\s+)?node:(\d+)", dockerfile, re.M)
    assert m, "Dockerfile: the console stage builds FROM node:<major>"
    assert _node_versions_in_ci() == {m.group(1)}


def test_pages_artifact_keeps_the_dotfiles_the_build_writes():
    """upload-pages-artifact v4 and later drop dotfiles unless asked, so a
    step that writes one into the site must also set include-hidden-files."""
    for path in WORKFLOWS:
        for job in _load(path)["jobs"].values():
            steps = job.get("steps", [])
            for step in steps:
                if not str(step.get("uses", "")).startswith("actions/upload-pages-artifact@"):
                    continue
                site = step["with"]["path"]
                writes_dotfile = any(
                    re.search(rf"{re.escape(site)}/\.\w", s.get("run", "")) for s in steps
                )
                hidden = step["with"].get("include-hidden-files") is True
                assert hidden or not writes_dotfile, (
                    f"{path.name}: a dotfile in {site} never reaches the Pages artifact"
                )
