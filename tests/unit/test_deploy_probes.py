"""The container healthchecks use the probe routes (roadmap 18.7): the
image's HEALTHCHECK asks liveness, and Compose waits for readiness before
Caddy and the scheduler start."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_the_image_healthcheck_asks_liveness():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    check = dockerfile.split("HEALTHCHECK", 1)[1].split("\n\n", 1)[0]
    assert "/api/health/live" in check


def test_compose_waits_for_the_api_to_be_ready():
    compose = yaml.safe_load((ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8"))
    test = " ".join(compose["services"]["api"]["healthcheck"]["test"])
    assert "/api/health/ready" in test
