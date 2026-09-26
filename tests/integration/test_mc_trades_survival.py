"""MonteCarloTradesTest on real backtests (BL-17)."""

from __future__ import annotations

import dataclasses

import pytest

from stonks.lab.backtesting import run_backtest
from stonks.lab.survival.mc_trades import MonteCarloTradesTest, trade_contributions
from tests.fixtures.robustness_lab import FlipFlop, LongAll, dataset_for, trend_lake


@pytest.fixture
def lake():
    lake = trend_lake(":memory:", {"A.US": (0.001, 0.01)}, periods=520)
    yield lake
    lake.close()


def test_uses_validation_trades_and_reports_the_band(lake):
    ds = dataset_for(lake, ["A.US"], train_ratio=0.3)
    strategy = FlipFlop({"hold_bars": 2})
    report = MonteCarloTradesTest(n_paths=1000, min_trades=30).run(strategy, ds)

    val = run_backtest(FlipFlop({"hold_bars": 2}), ds, ds.val_window)
    assert report.metrics["n_trades"] == val.trade_stats.n_trades >= 30
    assert "p95_max_dd" in report.metrics
    assert "validation window" in report.notes


def test_contributions_match_trade_returns_when_fully_invested(lake):
    ds = dataset_for(lake, ["A.US"])
    val = run_backtest(FlipFlop({"hold_bars": 5}), ds, ds.val_window)
    contrib = trade_contributions(val)
    closed = [t for t in val.trades if not t.is_open]
    assert len(contrib) == len(closed) > 0
    # ~99% of equity goes into each trade, so pnl/equity ~ 0.99 * trade return
    for c, t in zip(contrib, closed, strict=True):
        assert c == pytest.approx(0.99 * t.return_pct, rel=0.05, abs=1e-4)


def test_buy_and_hold_has_no_closed_trades_and_fails_for_lack_of_data(lake):
    ds = dataset_for(lake, ["A.US"])
    report = MonteCarloTradesTest().run(LongAll({}), ds)
    assert not report.passed
    assert report.metrics["n_trades"] == 0
    assert "insufficient data" in report.notes


def test_prefers_a_stitched_walk_forward_report(lake):
    ds = dataset_for(lake, ["A.US"], train_ratio=0.5)
    stitched = run_backtest(FlipFlop({"hold_bars": 2}), ds, ds.full_window)

    @dataclasses.dataclass
    class WithStitched:
        base: object
        stitched_oos_report: object

        def __getattr__(self, name):
            return getattr(self.base, name)

    report = MonteCarloTradesTest(n_paths=1000).run(
        FlipFlop({"hold_bars": 2}), WithStitched(ds, stitched)
    )
    assert report.metrics["n_trades"] == stitched.trade_stats.n_trades
    assert "stitched" in report.notes


def test_run_seed_comes_from_the_tuner_when_unset():
    test = MonteCarloTradesTest()
    assert test.seed == 17

    class Setup:
        tuner = type("T", (), {"seed": 99})()

    test.bind_run(type("Ctx", (), {"setup": Setup()})())
    assert test.seed == 99
    assert MonteCarloTradesTest(seed=3).seed == 3
