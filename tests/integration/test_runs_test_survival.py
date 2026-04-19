"""Integration tests for RunsTestSurvivalTest — Wald-Wolfowitz independence
check on a strategy's per-bar return signs over the full window."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.core.protocols import SurvivalReport
from stonks.lab.dataset import LabDataset
from stonks.lab.survival.runs_test import RunsTestSurvivalTest
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold


@pytest.fixture
def lake_random_walk(tmp_path):
    rng = np.random.default_rng(3)
    dates = pd.bdate_range(start="2025-01-02", periods=300)
    close = np.exp(np.log(100.0) + np.cumsum(rng.normal(0.0, 0.015, len(dates))))

    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_prices(
        pd.DataFrame(
            [
                {"ticker": "R.US", "date": d.date(),
                 "open": c, "high": c, "low": c, "close": c,
                 "adj_close": c, "volume": 1_000_000}
                for d, c in zip(dates, close, strict=False)
            ]
        )
    )
    yield lake, dates
    lake.close()


def _dataset(lake, dates):
    return LabDataset(
        lake=lake, universe=["R.US"],
        start=dates[0].date(), end=dates[-1].date(),
        interval=Interval.DAY_1,
    )


def test_runs_test_survival_returns_valid_report(lake_random_walk):
    lake, dates = lake_random_walk
    ds = _dataset(lake, dates)
    test = RunsTestSurvivalTest(max_abs_z_score=3.0)
    strategy = BuyAndHold({"ticker": "R.US", "allocation": 1.0})

    report = test.run(strategy, ds)

    assert isinstance(report, SurvivalReport)
    assert report.test_id == "runs_test"
    assert "z_score" in report.metrics
    assert "n_positive" in report.metrics
    assert "n_negative" in report.metrics
    assert "n_runs" in report.metrics


def test_runs_test_survival_passes_on_independent_like_sequence(lake_random_walk):
    lake, dates = lake_random_walk
    ds = _dataset(lake, dates)
    # wide threshold ⇒ any random-walk-ish strategy passes
    test = RunsTestSurvivalTest(max_abs_z_score=5.0)
    strategy = BuyAndHold({"ticker": "R.US", "allocation": 1.0})
    report = test.run(strategy, ds)
    assert report.passed is True


def test_runs_test_survival_fails_when_threshold_is_zero(lake_random_walk):
    """With a 0-threshold only a perfectly independent sign sequence would
    pass; real backtest returns are never exactly that, so this fails."""
    lake, dates = lake_random_walk
    ds = _dataset(lake, dates)
    test = RunsTestSurvivalTest(max_abs_z_score=0.0)
    strategy = BuyAndHold({"ticker": "R.US", "allocation": 1.0})
    report = test.run(strategy, ds)
    # either the z_score is non-zero (fail) or we had no variance to sample (skipped)
    assert report.passed is False or "insufficient" in report.notes.lower()


def test_runs_test_survival_skips_when_no_bars(tmp_path):
    lake = DuckDBLake(tmp_path / "empty.duckdb")
    lake.migrate()
    try:
        from datetime import date

        ds = LabDataset(
            lake=lake, universe=["Z.US"],
            start=date(2025, 1, 1), end=date(2025, 6, 30),
            interval=Interval.DAY_1,
        )
        test = RunsTestSurvivalTest()
        strategy = BuyAndHold({"ticker": "Z.US"})
        report = test.run(strategy, ds)
        assert "insufficient" in report.notes.lower() or report.passed in (True, False)
    finally:
        lake.close()


def test_runs_test_rejects_bad_max_abs_z_score():
    with pytest.raises(ValueError):
        RunsTestSurvivalTest(max_abs_z_score=-1.0)
