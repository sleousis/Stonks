"""Roadmap 23.9: the swap check's ``vs_live`` is a paired test on daily
returns, not a raw cumulative gap."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from stonks.lifecycle.check import _vs_live_check, paired_gaps
from stonks.lifecycle.settings import SwapPolicy
from stonks.production.pnl import daily_pnl

START = date(2026, 1, 5)


def _rows(returns: list[float], start: int = 0):
    value, points = 10_000.0, [(START + timedelta(days=start), 10_000.0)]
    for i, r in enumerate(returns, start=1):
        value *= 1 + r
        points.append((START + timedelta(days=start + i), value))
    return daily_pnl(points, max_gap_days=10)


def test_gaps_pair_only_shared_days() -> None:
    cand = _rows([0.01, 0.02, 0.03])
    live = _rows([0.0, 0.01], start=1)
    gaps = paired_gaps(cand, live)
    # live has returns on days 2 and 3; candidate on 1, 2, 3
    assert gaps == pytest.approx([0.02 - 0.0, 0.03 - 0.01])


def test_a_steadily_worse_candidate_fails() -> None:
    rng = np.random.default_rng(0)
    live = rng.normal(0.001, 0.01, 40)
    cand = live - 0.004 + rng.normal(0, 0.001, 40)
    check, t = _vs_live_check(list(cand - live), 1, SwapPolicy())
    assert not check.passed
    assert t is not None and t < check.limit
    assert "trails live" in check.detail


def test_a_noisy_small_lag_is_not_evidence() -> None:
    """A raw gap of a couple of points can be noise. The paired test lets it through."""
    rng = np.random.default_rng(1)
    gaps = list(rng.normal(-0.0005, 0.01, 30))
    check, _ = _vs_live_check(gaps, 1, SwapPolicy())
    assert check.passed, check.detail
    assert check.limit == pytest.approx(-1.6449, abs=1e-3)


def test_too_few_pairs_fail() -> None:
    check, t = _vs_live_check([0.01], 1, SwapPolicy())
    assert not check.passed and t is None and check.value is None
    few, _ = _vs_live_check([0.01, 0.0, 0.02], 1, SwapPolicy(min_paired_days=5))
    assert not few.passed and "need >= 5" in few.detail


def test_identical_books_pass() -> None:
    check, t = _vs_live_check([0.0] * 12, 1, SwapPolicy())
    assert check.passed and t == 0.0


def test_the_old_raw_gap_setting_still_loads() -> None:
    policy = SwapPolicy.model_validate({"max_underperformance": 0.02, "min_days": 5})
    assert policy.min_days == 5
    assert not hasattr(policy, "max_underperformance")
