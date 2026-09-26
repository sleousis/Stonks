"""RS-01: tickers a strategy reads but does not trade (a reference market, an
index filter, a regime condition's ticker, the benchmark) reach every worker
snapshot and every permuted or perturbed lake. Without them tuning results
change with the worker count and the MCPT passes on runs that never trade."""

from __future__ import annotations

import math

import pytest

from stonks.core.interval import Interval
from stonks.lab.backtesting import run_backtest
from stonks.lab.dataset import LabDataset, data_tickers
from stonks.lab.objectives import FinalReturnObjective
from stonks.lab.parallel import ParallelSettings, dataset_snapshot
from stonks.lab.survival.permutation import PermutationScorer
from stonks.lab.survival.perturbation import PerturbationTest
from stonks.lab.tuning.base import fix_params
from stonks.lab.tuning.grid import GridTuner
from stonks.strategies.base import strategy_data_tickers
from stonks.strategies.examples.intramarket_difference import IntramarketDifferenceStrategy
from stonks.strategies.examples.stocks_on_the_move import StocksOnTheMove
from stonks.strategies.regime import RegimeFilter
from tests.nt888_bars import as_of, random_walk, seed_lake

T, REF = "ETH.CC", "BTC.CC"
N = 480
PARAMS = {"ticker": T, "reference_ticker": REF, "lookback": 6, "atr_lookback": 24}


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    frame = random_walk(N, seed=21)
    ref = random_walk(N, seed=22, start_price=40.0)
    lk = seed_lake(tmp_path_factory.mktemp("refs") / "lake.duckdb", {T: frame, REF: ref})
    yield lk, frame
    lk.close()


def _dataset(lake) -> LabDataset:
    lk, frame = lake
    return LabDataset(
        lake=lk,
        universe=[T],
        start=as_of(frame, 0).date(),
        end=as_of(frame, -1).date(),
        interval=Interval.HOUR_1,
        train_ratio=0.4,
        benchmark="none",
    )


def _n_trades(strategy, dataset) -> float:
    return float(run_backtest(strategy, dataset, dataset.full_window).trade_stats.n_trades)


# ---- declarations ---------------------------------------------------------------


def test_strategies_declare_the_tickers_they_read():
    assert strategy_data_tickers(IntramarketDifferenceStrategy(PARAMS)) == (REF,)
    assert strategy_data_tickers(StocksOnTheMove({})) == ("SPY.US",)
    assert strategy_data_tickers(StocksOnTheMove({"index_ticker": ""})) == ()
    regime = RegimeFilter(
        {
            "inner_class_path": (
                "stonks.strategies.examples.intramarket_difference:IntramarketDifferenceStrategy"
            ),
            "inner_params": PARAMS,
            "conditions": [
                {"kind": "price_trend", "ticker": "QQQ.US"},
                {"kind": "yield_curve"},
            ],
        }
    )
    assert set(strategy_data_tickers(regime)) == {REF, "QQQ.US", "US10Y.GBOND", "US3M.GBOND"}


def test_dataset_data_tickers_hold_universe_references_and_benchmark(lake):
    ds = _dataset(lake)
    assert data_tickers(ds) == [T]
    ds = ds.for_strategy(IntramarketDifferenceStrategy(PARAMS))
    assert ds.universe == [T]  # never traded
    assert data_tickers(ds) == [T, REF]
    import dataclasses

    assert "SPY.US" in data_tickers(dataclasses.replace(ds, benchmark="auto"))
    assert "QQQ.US" in data_tickers(dataclasses.replace(ds, benchmark="QQQ.US"))
    assert data_tickers(dataclasses.replace(ds, benchmark="EW")) == [T, REF]


# ---- snapshots and tuning ---------------------------------------------------------


def test_snapshot_carries_the_strategy_reference_bars(lake):
    ds = _dataset(lake).for_strategy(IntramarketDifferenceStrategy(PARAMS))
    with dataset_snapshot(ds) as spec:
        opened = spec.open()
        try:
            n = opened.lake.sql("SELECT COUNT(*) AS n FROM bars WHERE ticker = ?", [REF])
            assert int(n["n"].iloc[0]) == N
        finally:
            opened.lake.close()


def test_tuning_is_identical_for_one_and_two_workers(lake):
    space = fix_params(
        IntramarketDifferenceStrategy.parameter_spec(),
        {"ticker": T, "reference_ticker": REF, "atr_lookback": 24},
    )
    results = [
        GridTuner(grid_size=2, parallel=ParallelSettings(max_workers=w)).tune(
            strategy_cls=IntramarketDifferenceStrategy,
            param_space=space,
            objective=FinalReturnObjective(),
            dataset=_dataset(lake),
            budget=4,
        )
        for w in (1, 2)
    ]
    serial, pooled = results
    assert any(s != 0.0 for _, s in serial.history), "the serial run must trade"
    assert pooled.history == serial.history
    assert pooled.best_params == serial.best_params


# ---- modified lakes -----------------------------------------------------------------


def test_permuted_lake_runs_still_trade(lake):
    strategy = IntramarketDifferenceStrategy(PARAMS)
    ds = _dataset(lake).for_strategy(strategy)
    scorer = PermutationScorer.build(strategy, ds, ds.full_window, _n_trades)
    assert scorer is not None
    assert scorer.score_real() > 0
    assert scorer.score_permutation(seed=3) > 0


def test_perturbed_lake_runs_still_trade(lake):
    strategy = IntramarketDifferenceStrategy(PARAMS)
    ds = _dataset(lake).for_strategy(strategy)
    report = PerturbationTest(
        noise_sigmas=(1e-9,), min_correlation=0.99, window="full", max_workers=1
    ).run(strategy, ds)
    assert math.isfinite(report.metrics["correlation_min"])
    assert report.passed, report.metrics


# ---- preflight and runner --------------------------------------------------------------


def test_preflight_warns_when_a_reference_ticker_has_no_bars(lake):
    from stonks.lab.preflight import run_preflight

    strategy = IntramarketDifferenceStrategy({**PARAMS, "reference_ticker": "NOPE.CC"})
    report = run_preflight(_dataset(lake).for_strategy(strategy), strategy)
    issue = next(i for i in report.issues if i.code == "missing_reference_data")
    assert issue.severity == "warning"
    assert issue.details["tickers"] == ["NOPE.CC"]
    clean = run_preflight(_dataset(lake).for_strategy(IntramarketDifferenceStrategy(PARAMS)))
    assert not any(i.code == "missing_reference_data" for i in clean.issues)


def test_runner_adds_the_strategy_references_before_tuning(lake):
    from stonks.lab.runner import with_strategy_references

    ds = with_strategy_references(_dataset(lake), IntramarketDifferenceStrategy, PARAMS)
    assert ds.reference_tickers == (REF,)
    assert ds.universe == [T]


def test_backtest_is_the_same_with_the_reference_inside_or_outside_the_universe(lake):
    import dataclasses

    strategy = IntramarketDifferenceStrategy(PARAMS)
    outside = _dataset(lake).for_strategy(strategy)
    inside = dataclasses.replace(outside, universe=[T, REF], reference_tickers=())
    a = run_backtest(IntramarketDifferenceStrategy(PARAMS), outside, outside.full_window)
    b = run_backtest(IntramarketDifferenceStrategy(PARAMS), inside, inside.full_window)
    assert a.trade_stats.n_trades > 0
    assert a.equity_curve == b.equity_curve
