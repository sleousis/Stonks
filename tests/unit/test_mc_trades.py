"""Monte Carlo over the trade sequence (BL-17)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pytest

from stonks.backtest.report import compute_report
from stonks.backtest.trades import RoundTrip
from stonks.lab.survival.mc_trades import (
    MonteCarloTradesOptions,
    MonteCarloTradesTest,
    simulate_trade_paths,
    trade_contributions,
)
from stonks.lab.survival.registry import build_survival_test, survival_test_names

_KEYS = (
    "risk_of_ruin",
    "median_max_dd",
    "p95_max_dd",
    "median_return",
    "p05_return",
    "return_to_dd",
    "prob_profit",
)


def _coin_flip(n: int = 200, size: float = 0.08) -> np.ndarray:
    return np.array([size if i % 2 == 0 else -size for i in range(n)])


def test_registered_under_its_id():
    assert "mc_trades" in survival_test_names()
    test = build_survival_test("mc_trades", {"n_paths": 1000, "min_trades": 10})
    assert isinstance(test, MonteCarloTradesTest)
    assert test.options.n_paths == 1000


def test_options_bounds_follow_the_spec():
    with pytest.raises(ValueError):
        MonteCarloTradesOptions(n_paths=100)
    with pytest.raises(ValueError):
        MonteCarloTradesOptions(min_return_to_dd=10.0)
    defaults = MonteCarloTradesOptions()
    assert defaults.n_paths == 5000
    assert defaults.ruin_drawdown == 0.40
    assert defaults.min_trades == 30
    assert defaults.max_risk_of_ruin == 0.10
    assert defaults.min_return_to_dd == 2.0
    assert defaults.min_prob_profit == 0.8


def test_all_winning_trades_never_ruin():
    result = simulate_trade_paths(np.full(40, 0.01), n_per_year=40, n_paths=1000, seed=1)
    assert result.risk_of_ruin == 0.0
    assert result.p95_max_dd == 0.0
    assert result.prob_profit == 1.0
    assert result.median_return == pytest.approx(1.01**40 - 1)


def test_ruined_paths_stop_at_ruin():
    # every trade loses 25%: the second trade takes the path past a 40% drawdown
    result = simulate_trade_paths(
        np.full(40, -0.25), n_per_year=10, n_paths=1000, ruin_drawdown=0.4, seed=1
    )
    assert result.risk_of_ruin == 1.0
    assert result.median_return == pytest.approx(0.75**2 - 1)
    assert result.median_max_dd == pytest.approx(1 - 0.75**2)


def test_seeded_runs_are_reproducible_and_seed_sensitive():
    trades = np.random.default_rng(0).normal(0.004, 0.03, 120)
    a = simulate_trade_paths(trades, n_per_year=60, n_paths=2000, seed=3)
    b = simulate_trade_paths(trades, n_per_year=60, n_paths=2000, seed=3)
    c = simulate_trade_paths(trades, n_per_year=60, n_paths=2000, seed=4)
    assert a == b
    assert a != c


def test_coin_flip_fails():
    report = MonteCarloTradesTest(n_paths=2000, seed=2).evaluate_contributions(
        _coin_flip(), years=1.0
    )
    assert not report.passed
    for key in _KEYS:
        assert key in report.metrics


def test_steady_edge_passes_and_stores_the_drawdown_bands():
    rng = np.random.default_rng(5)
    trades = np.where(rng.random(100) < 0.7, 0.02, -0.01)
    report = MonteCarloTradesTest(n_paths=2000, seed=2).evaluate_contributions(trades, years=1.0)
    assert report.passed, report.notes
    for key in _KEYS:
        assert key in report.metrics
    assert 0.0 < report.metrics["median_max_dd"] <= report.metrics["p95_max_dd"] < 0.4
    assert report.metrics["n_trades"] == 100
    assert report.metrics["n_per_year"] == 100


def test_too_few_trades_fails_with_a_note_and_no_band():
    trades = np.full(29, 0.02)  # would pass every threshold with enough trades
    report = MonteCarloTradesTest(n_paths=1000).evaluate_contributions(trades, years=1.0)
    assert not report.passed
    assert "insufficient data" in report.notes
    assert report.metrics["n_trades"] == 29
    # a go-live check must never read a band built on too few trades
    assert "p95_max_dd" not in report.metrics


def test_zero_trades_fails_without_division_errors():
    report = MonteCarloTradesTest().evaluate_contributions(np.empty(0), years=1.0)
    assert not report.passed
    assert report.metrics["n_trades"] == 0


def test_trade_rate_sets_the_path_length():
    trades = np.full(60, 0.01)
    report = MonteCarloTradesTest(n_paths=1000).evaluate_contributions(trades, years=2.0)
    assert report.metrics["n_per_year"] == 30
    assert report.metrics["median_return"] == pytest.approx(1.01**30 - 1)


def _trip(entry: datetime, pnl: float, *, is_open: bool = False) -> RoundTrip:
    return RoundTrip(
        ticker="X",
        strategy_key="0",
        entry_ts=entry,
        exit_ts=entry + timedelta(days=2),
        qty=1.0,
        entry_px=100.0,
        exit_px=100.0 + pnl,
        pnl=pnl,
        return_pct=pnl / 100.0,
        bars_held=2,
        fees=0.0,
        slippage_cost=0.0,
        mae_pct=None,
        mfe_pct=None,
        is_open=is_open,
    )


def test_contributions_are_pnl_over_equity_before_entry():
    dates = [date(2025, 1, d) for d in (1, 2, 3, 6, 7)]
    curve = [1000.0, 1000.0, 1100.0, 1200.0, 1200.0]
    report = compute_report("s", dates, curve)
    import dataclasses

    report = dataclasses.replace(
        report,
        trades=(
            _trip(datetime(2025, 1, 2, 14, 30), 100.0),  # equity before: 1000 (Jan 1)
            _trip(datetime(2025, 1, 6, 14, 30), -60.0),  # equity before: 1100 (Jan 3)
            _trip(datetime(2025, 1, 7, 14, 30), 5.0, is_open=True),  # open: skipped
        ),
    )
    np.testing.assert_allclose(trade_contributions(report), [0.1, -60.0 / 1100.0])


def test_overlapping_trades_each_count_against_the_equity_before_their_entry():
    import dataclasses

    dates = [date(2025, 1, d) for d in (1, 2, 3, 6, 7)]
    curve = [1000.0, 1000.0, 1100.0, 1200.0, 1200.0]
    report = dataclasses.replace(
        compute_report("s", dates, curve),
        trades=(
            # two trades open over the same days (two tickers held at once)
            _trip(datetime(2025, 1, 2, 14, 30), 40.0),
            _trip(datetime(2025, 1, 2, 14, 30), 60.0),
            # a third entered while both are still open
            _trip(datetime(2025, 1, 3, 14, 30), -20.0),
        ),
    )
    np.testing.assert_allclose(trade_contributions(report), [0.04, 0.06, -20.0 / 1000.0])
