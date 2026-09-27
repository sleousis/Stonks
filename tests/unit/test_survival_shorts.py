"""Survival tests on long/short books (roadmap 16.4): short round trips in
the trade Monte Carlo and the runs test, the borrow-cost stress and the
short-squeeze scenario."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.costs import CostModelSettings
from stonks.backtest.report import compute_report
from stonks.backtest.shorting import ShortingSettings
from stonks.backtest.trades import RoundTrip
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset
from stonks.lab.survival.cost_stress import CostStressTest
from stonks.lab.survival.mc_trades import trade_contributions
from stonks.lab.survival.runs_test import round_trip_trades
from stonks.lab.survival.stress import StressTest, squeeze_bars
from stonks.strategies.examples.tsmom import TimeSeriesMomentum
from tests.unit.trend_helpers import DATES, build_lake, trend

SERIES = {"UP.US": trend(0.003, seed=1), "DOWN.US": trend(-0.003, seed=2)}


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    db = build_lake(tmp_path_factory.mktemp("survival_shorts") / "lake.duckdb", SERIES)
    yield db
    db.close()


def _dataset(lake, shorting: ShortingSettings | None) -> LabDataset:
    return LabDataset(
        lake=lake,
        universe=["UP.US", "DOWN.US"],
        start=DATES[400].date(),
        end=DATES[-1].date(),
        train_ratio=0.5,
        costs=CostModelSettings.realistic(),
        benchmark="none",
        shorting=shorting,
    )


def _short_strategy() -> TimeSeriesMomentum:
    return TimeSeriesMomentum({"short_mode": "short", "rebalance": "daily"})


# ---- the trade ledger in the tests ----------------------------------------------------


def _trip(side: str, pnl: float, day: int) -> RoundTrip:
    ts = datetime(2024, 1, day, tzinfo=UTC)
    return RoundTrip(
        ticker="X",
        strategy_key="0",
        entry_ts=ts,
        exit_ts=datetime(2024, 1, day + 1, tzinfo=UTC),
        qty=10.0,
        entry_px=100.0,
        exit_px=100.0 - pnl / 10.0 if side == "short" else 100.0 + pnl / 10.0,
        pnl=pnl,
        return_pct=pnl / 1000.0,
        bars_held=1,
        fees=0.0,
        slippage_cost=0.0,
        mae_pct=None,
        mfe_pct=None,
        is_open=False,
        side=side,  # type: ignore[arg-type]
    )


def test_short_round_trips_count_in_the_trade_monte_carlo_and_runs_test():
    dates = [date(2024, 1, d) for d in range(1, 8)]
    report = compute_report("s", dates, [1000.0] * len(dates))
    trips = (_trip("short", 50.0, 2), _trip("long", -20.0, 3), _trip("short", -10.0, 4))
    from dataclasses import replace

    report = replace(report, trades=trips)
    # a winning short adds, a losing short subtracts: pnl / equity before entry
    assert trade_contributions(report) == pytest.approx([0.05, -0.02, -0.01])
    assert [t.side for t in round_trip_trades(report)] == ["short", "long", "short"]


def test_lab_backtest_of_a_short_dataset_reports_its_short_book(lake):
    dataset = _dataset(lake, ShortingSettings())
    report = run_backtest(_short_strategy(), dataset, dataset.val_window)
    assert report.short_book is not None
    assert report.short_book.borrow_fees > 0
    assert report.short_book.n_short_trades >= 1
    long_only = _dataset(lake, None)
    assert run_backtest(_short_strategy(), long_only, long_only.val_window).short_book is None


# ---- cost stress ----------------------------------------------------------------------


@pytest.mark.slow
def test_cost_stress_charges_borrow_fees_on_a_short_book(lake):
    test = CostStressTest(max_workers=1)
    report = test.run(_short_strategy(), _dataset(lake, ShortingSettings()))
    m = report.metrics
    assert m["borrow_fee_rate"] == pytest.approx(0.005)
    assert m["borrow_stress_multiplier"] == pytest.approx(3.0)
    assert m["financing_paid"] > 0
    assert np.isfinite(m["sharpe_borrow_stress"])
    # 3x borrow fees can only cost Sharpe
    assert m["sharpe_borrow_stress"] <= m["sharpe_1x"] + 1e-9


@pytest.mark.slow
def test_cost_stress_of_a_long_only_dataset_has_no_borrow_metrics(lake):
    report = CostStressTest(max_workers=1).run(_short_strategy(), _dataset(lake, None))
    assert "sharpe_borrow_stress" not in report.metrics
    assert "borrow_fee_rate" not in report.metrics


# ---- short squeeze --------------------------------------------------------------------


def test_squeeze_bars_hand_checked():
    bars = pd.DataFrame(
        {
            "timestamp": pd.bdate_range("2024-01-01", periods=5),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "adj_close": 100.0,
        }
    )
    # +44 % over 2 bars: x1.2 then x1.44, then held
    out = squeeze_bars(bars, 2, 0.44, 2)
    assert out["close"].tolist() == pytest.approx([100.0, 100.0, 120.0, 144.0, 144.0])
    assert out["high"].tolist() == pytest.approx([101.0, 101.0, 121.2, 145.44, 145.44])
    assert bars["close"].tolist() == [100.0] * 5  # the input is not changed
    assert squeeze_bars(bars, 9, 0.5, 2)["close"].tolist() == [100.0] * 5


@pytest.mark.slow
def test_stress_runs_the_squeeze_on_a_short_book(lake):
    options = StressTest.Options(n_paths=2, max_workers=1, max_squeeze_drawdown=-0.001)
    report = StressTest(options).run(_short_strategy(), _dataset(lake, ShortingSettings()))
    m = report.metrics
    assert m["squeeze_n_shorts"] >= 1
    assert m["squeeze_jump"] == pytest.approx(0.5)
    # the squeezed short costs money against the plain window
    assert m["squeeze_loss"] > 0
    assert not report.passed and "short squeeze" in report.notes


def test_stress_of_a_long_only_dataset_skips_the_squeeze(lake):
    options = StressTest.Options(n_paths=2, max_workers=1)
    report = StressTest(options).run(_short_strategy(), _dataset(lake, None))
    assert not any(k.startswith("squeeze_") for k in report.metrics)
