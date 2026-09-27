"""Hypothesis settings for the property tests (BL-49).

Runs are derandomized, so a failure reproduces on every machine and in CI,
and have no deadline, so a busy xdist worker never fails a test on time.
``HYPOTHESIS_PROFILE=deep`` runs many more random examples locally.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

settings.register_profile(
    "default",
    derandomize=True,
    deadline=None,
    max_examples=150,
    suppress_health_check=[HealthCheck.too_slow],
    database=None,
)
settings.register_profile(
    "deep",
    deadline=None,
    max_examples=5_000,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))
