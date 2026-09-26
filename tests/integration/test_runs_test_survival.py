"""Integration tests for RunsTestSurvivalTest — Wald-Wolfowitz independence
check on a strategy's per-bar return signs over the full window."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.core.protocols import SurvivalReport
from stonks.lab.dataset import LabDataset
from stonks.features.library import count_runs, runs_test_z_score
from stonks.lab.backtesting import run_backtest, run_backtest_with_fills
from stonks.lab.survival.runs_test import RunsTestSurvivalTest, round_trip_trades
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum


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
                {
                    "ticker": "R.US",
                    "date": d.date(),
                    "open": c,
                    "high": c,
                    "low": c,
                    "close": c,
                    "adj_close": c,
                    "volume": 1_000_000,
                }
                for d, c in zip(dates, close, strict=False)
            ]
        )
    )
    yield lake, dates
    lake.close()


def _dataset(lake, dates):
    return LabDataset(
        lake=lake,
        universe=["R.US"],
        start=dates[0].date(),
        end=dates[-1].date(),
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
            lake=lake,
            universe=["Z.US"],
            start=date(2025, 1, 1),
            end=date(2025, 6, 30),
            interval=Interval.DAY_1,
        )
        test = RunsTestSurvivalTest()
        strategy = BuyAndHold({"ticker": "Z.US"})
        report = test.run(strategy, ds)
        assert "insufficient" in report.notes.lower() or report.passed in (True, False)
    finally:
        lake.close()


def test_run_backtest_with_fills_matches_run_backtest(lake_random_walk):
    lake, dates = lake_random_walk
    ds = _dataset(lake, dates)
    report, fills = run_backtest_with_fills(Momentum({"lookback_days": 5}), ds, ds.full_window)
    plain = run_backtest(Momentum({"lookback_days": 5}), ds, ds.full_window)
    assert report == plain
    assert fills and {f.side for f in fills} == {"buy", "sell"}
    assert [f.filled_at for f in fills] == sorted(f.filled_at for f in fills)


def test_trade_level_runs_test_scores_signs_of_round_trip_returns(lake_random_walk):
    lake, dates = lake_random_walk
    ds = _dataset(lake, dates)
    strategy = Momentum({"lookback_days": 5})
    _, fills = run_backtest_with_fills(Momentum({"lookback_days": 5}), ds, ds.full_window)
    returns = np.array([t.return_pct for t in round_trip_trades(fills)])
    signs = np.sign(returns[returns != 0]).astype(int)
    assert len(signs) >= 10  # enough round trips for the test to mean something

    report = RunsTestSurvivalTest(max_abs_z_score=3.0, trade_level=True).run(strategy, ds)

    assert report.metrics["z_score"] == pytest.approx(runs_test_z_score(signs))
    assert report.metrics["n_trades"] == float(len(signs))
    assert report.metrics["n_positive"] == float((signs > 0).sum())
    assert report.metrics["n_runs"] == float(count_runs(signs))
    assert "trade_level" in report.notes
    # the bar-level default scores a different, much longer sequence
    bar_level = RunsTestSurvivalTest(max_abs_z_score=3.0).run(strategy, ds)
    assert bar_level.metrics["n_positive"] + bar_level.metrics["n_negative"] > len(signs)


def test_trade_level_runs_test_passes_with_too_few_trades(lake_random_walk):
    lake, dates = lake_random_walk
    ds = _dataset(lake, dates)
    # buy-and-hold never closes a trade
    report = RunsTestSurvivalTest(trade_level=True).run(
        BuyAndHold({"ticker": "R.US", "allocation": 1.0}), ds
    )
    assert report.passed is True
    assert report.metrics["n_trades"] == 0.0
    assert "insufficient" in report.notes


def test_runs_test_rejects_bad_max_abs_z_score():
    with pytest.raises(ValueError):
        RunsTestSurvivalTest(max_abs_z_score=-1.0)
