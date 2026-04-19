"""Integration tests for MonteCarloPermutationTest.

Exercises the full pipeline: real-lake bars + N permuted in-memory lakes,
one backtest each, p-value against a configurable threshold.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.core.protocols import SurvivalReport
from stonks.lab.dataset import LabDataset
from stonks.lab.survival.permutation import MonteCarloPermutationTest
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold


@pytest.fixture
def lake_gbm(tmp_path):
    """Lake with a random-walk (GBM-ish) price series — no persistent
    time structure a breakout-style strategy could exploit. Good null-
    hypothesis data for MCPT."""
    rng = np.random.default_rng(3)
    n = 300
    dates = pd.bdate_range(start="2025-01-02", periods=n)
    log_close = np.log(100.0) + np.cumsum(rng.normal(0.0, 0.015, n))
    close = np.exp(log_close)

    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_prices(
        pd.DataFrame(
            [
                {"ticker": "RND.US", "date": d.date(),
                 "open": c, "high": c, "low": c, "close": c,
                 "adj_close": c, "volume": 1_000_000}
                for d, c in zip(dates, close, strict=False)
            ]
        )
    )
    yield lake, dates
    lake.close()


def test_mcpt_returns_valid_report(lake_gbm):
    lake, dates = lake_gbm
    dataset = LabDataset(
        lake=lake,
        universe=["RND.US"],
        start=dates[0].date(),
        end=dates[-1].date(),
        interval=Interval.DAY_1,
    )
    test = MonteCarloPermutationTest(n_permutations=10, max_p_value=0.05, seed=1)
    strategy = BuyAndHold({"ticker": "RND.US", "allocation": 1.0})

    report = test.run(strategy, dataset)

    assert isinstance(report, SurvivalReport)
    assert report.test_id == "mcpt"
    assert 0.0 <= report.metrics["p_value"] <= 1.0
    assert report.metrics["n_permutations"] == 10.0
    assert "real_score" in report.metrics
    assert "perm_score_mean" in report.metrics


def test_mcpt_respects_max_p_value_threshold(lake_gbm):
    """With a lenient threshold and a strategy that shouldn't beat random,
    the test should still produce a coherent passed/fail flag tied to
    p_value vs max_p_value."""
    lake, dates = lake_gbm
    dataset = LabDataset(
        lake=lake,
        universe=["RND.US"],
        start=dates[0].date(),
        end=dates[-1].date(),
        interval=Interval.DAY_1,
    )

    lenient = MonteCarloPermutationTest(n_permutations=5, max_p_value=1.0, seed=1)
    strict = MonteCarloPermutationTest(n_permutations=5, max_p_value=0.0001, seed=1)

    strategy = BuyAndHold({"ticker": "RND.US", "allocation": 1.0})
    r_lenient = lenient.run(strategy, dataset)
    r_strict = strict.run(strategy, dataset)
    # same p_value (same seed), different thresholds flip pass/fail
    assert r_lenient.metrics["p_value"] == r_strict.metrics["p_value"]
    assert r_lenient.passed is True
    assert r_strict.passed is False


def test_mcpt_skips_when_universe_has_no_bars(tmp_path):
    lake = DuckDBLake(tmp_path / "empty.duckdb")
    lake.migrate()
    try:
        dataset = LabDataset(
            lake=lake,
            universe=["NOBARS.US"],
            start=date(2026, 1, 1),
            end=date(2026, 4, 1),
            interval=Interval.DAY_1,
        )
        test = MonteCarloPermutationTest(n_permutations=3)
        strategy = BuyAndHold({"ticker": "NOBARS.US"})
        report = test.run(strategy, dataset)
        assert report.passed is False   # no data = no evidence
        assert report.metrics["p_value"] == 1.0
    finally:
        lake.close()


def test_mcpt_rejects_bad_constructor_args():
    with pytest.raises(ValueError):
        MonteCarloPermutationTest(n_permutations=0)
    with pytest.raises(ValueError):
        MonteCarloPermutationTest(max_p_value=0.0)
    with pytest.raises(ValueError):
        MonteCarloPermutationTest(max_p_value=1.5)
    with pytest.raises(ValueError):
        MonteCarloPermutationTest(metric="unknown")
    with pytest.raises(ValueError):
        MonteCarloPermutationTest(start_index_ratio=1.5)
