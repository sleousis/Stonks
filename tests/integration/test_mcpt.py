"""Integration tests for MonteCarloPermutationTest.

Exercises the full pipeline: real-lake bars + N permuted in-memory lakes,
one backtest each, p-value against a configurable threshold.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.core.protocols import SurvivalReport, TunerResult
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import SharpeObjective
from stonks.lab.survival.base import TuningSetup
from stonks.lab.survival.permutation import MonteCarloPermutationTest
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum


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
                {
                    "ticker": "RND.US",
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
        assert report.passed is False  # no data = no evidence
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


# ---- out-of-sample default, look-back history, other tables, re-tuning -------

FAR_PAST = datetime(1990, 1, 1)
FAR_FUTURE = datetime(2100, 1, 1)


class _Spy(BaseStrategy):
    """Records, per lake, every as_of it is asked about and the lake's
    full daily bar history (read once)."""

    id = "mcpt_spy"
    as_ofs: dict[object, list] = {}
    history: dict[object, pd.DataFrame] = {}
    statements: dict[object, int] = {}

    def estimate_return(self, ticker, as_of, lake):
        key = lake  # the object, not id(): closed lakes get their ids reused
        _Spy.as_ofs.setdefault(key, []).append(as_of)
        if key not in _Spy.history:
            _Spy.history[key] = lake.get_bars(ticker, Interval.DAY_1, FAR_PAST, FAR_FUTURE)
            _Spy.statements[key] = len(lake.get_income_statement(ticker))
        return

    def decide(self, my_picks, portfolio, prices, as_of):
        return []


def _reset_spy():
    _Spy.as_ofs, _Spy.history, _Spy.statements = {}, {}, {}


def _gbm_dataset(lake, dates):
    return LabDataset(
        lake=lake,
        universe=["RND.US"],
        start=dates[20].date(),  # 20 bars of pre-dataset history
        end=dates[-30].date(),  # 29 bars after the dataset end
        train_ratio=0.6,
        interval=Interval.DAY_1,
    )


def _as_date(ts):
    return pd.Timestamp(ts).date()


def test_default_scores_only_the_validation_window(lake_gbm):
    lake, dates = lake_gbm
    ds = _gbm_dataset(lake, dates)
    _reset_spy()
    MonteCarloPermutationTest(n_permutations=3, max_p_value=1.0, seed=1).run(_Spy({}), ds)

    val_start, val_end = ds.val_window
    assert len(_Spy.as_ofs) == 4  # the real copy + 3 permutations
    for as_ofs in _Spy.as_ofs.values():
        assert min(_as_date(a) for a in as_ofs) >= val_start
        assert max(_as_date(a) for a in as_ofs) <= val_end


def test_permutes_only_the_window_and_keeps_real_history_for_lookbacks(lake_gbm):
    lake, dates = lake_gbm
    ds = _gbm_dataset(lake, dates)
    real = lake.get_bars("RND.US", Interval.DAY_1, FAR_PAST, FAR_FUTURE)
    val_start, val_end = ds.val_window
    _reset_spy()
    MonteCarloPermutationTest(n_permutations=3, max_p_value=1.0, seed=1).run(_Spy({}), ds)

    real_ts = real["timestamp"].dt.date
    real_before = real[real_ts < val_start].reset_index(drop=True)
    real_inside = real[(real_ts >= val_start) & (real_ts <= val_end)].reset_index(drop=True)
    inside_variants = set()
    for hist in _Spy.history.values():
        ts = hist["timestamp"].dt.date
        # history before the window (incl. pre-dataset bars) is untouched
        before = hist[ts < val_start].reset_index(drop=True)
        pd.testing.assert_series_equal(before["close"], real_before["close"])
        assert (ts <= val_end).all()  # nothing after the window leaks in
        inside = hist[ts >= val_start].reset_index(drop=True)
        assert inside["timestamp"].tolist() == real_inside["timestamp"].tolist()
        inside_variants.add(tuple(np.round(inside["close"].to_numpy(), 10)))
    # the real copy plus 3 distinct permutations of the window
    assert tuple(np.round(real_inside["close"].to_numpy(), 10)) in inside_variants
    assert len(inside_variants) == 4


def test_multi_ticker_permutations_keep_cross_asset_co_movement(lake_gbm):
    """One shared permutation for the whole universe (Masters): a ticker
    that moves in lockstep with another still does in every permuted lake."""
    lake, dates = lake_gbm
    real = lake.get_bars("RND.US", Interval.DAY_1, FAR_PAST, FAR_FUTURE)
    twin = real.assign(ticker="TWIN.US")
    for col in ("open", "high", "low", "close", "adj_close"):
        twin[col] = twin[col] * 2.0
    lake.upsert_bars(twin, interval=Interval.DAY_1)

    seen: list[tuple[pd.DataFrame, pd.DataFrame]] = []

    class _PairSpy(BaseStrategy):
        id = "pair_spy"

        def __init__(self, params):
            super().__init__(params)
            self._lakes: list = []

        def estimate_return(self, ticker, as_of, lake):
            if not any(lake is x for x in self._lakes):
                self._lakes.append(lake)
                seen.append(
                    tuple(
                        lake.get_bars(t, Interval.DAY_1, FAR_PAST, FAR_FUTURE)
                        for t in ("RND.US", "TWIN.US")
                    )
                )
            return

        def decide(self, my_picks, portfolio, prices, as_of):
            return []

    ds = dataclasses.replace(_gbm_dataset(lake, dates), universe=["RND.US", "TWIN.US"])
    MonteCarloPermutationTest(n_permutations=3, max_p_value=1.0, seed=5).run(_PairSpy({}), ds)

    assert len(seen) == 4
    variants = set()
    for rnd, tw in seen:
        np.testing.assert_allclose(tw["close"].to_numpy(), 2.0 * rnd["close"].to_numpy())
        variants.add(tuple(np.round(rnd["close"].to_numpy(), 8)))
    assert len(variants) == 4  # the permutations did shuffle


def test_permuted_lakes_carry_non_bar_tables(lake_gbm):
    lake, dates = lake_gbm
    lake.con.execute(
        "INSERT INTO income_statement (ticker, period_end, frequency, revenue)"
        " VALUES ('RND.US', DATE '2024-12-31', 'A', 5.0)"
    )
    _reset_spy()
    MonteCarloPermutationTest(n_permutations=2, max_p_value=1.0, seed=1).run(
        _Spy({}), _gbm_dataset(lake, dates)
    )
    assert len(_Spy.statements) == 3
    assert set(_Spy.statements.values()) == {1}


def test_seed_is_threaded_through_permutations(lake_gbm):
    lake, dates = lake_gbm
    ds = _gbm_dataset(lake, dates)
    strategy = Momentum({"lookback_days": 5, "threshold": 0.0})

    def run(seed):
        return MonteCarloPermutationTest(
            n_permutations=4, max_p_value=1.0, metric="sharpe", seed=seed
        ).run(strategy, ds)

    assert run(3).metrics == run(3).metrics
    assert run(3).metrics["perm_score_mean"] != run(4).metrics["perm_score_mean"]


def test_coarser_intervals_are_derived_from_the_permuted_bars(tmp_path):
    """Intraday dataset: daily bars in the modified lakes must come from the
    (permuted) hourly bars, never from the real daily bars."""
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    rng = np.random.default_rng(0)
    days = pd.bdate_range("2025-03-03", periods=40)
    hourly = [d + pd.Timedelta(hours=h) for d in days for h in range(14, 21)]
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(hourly))))
    lake.upsert_bars(
        pd.DataFrame(
            {
                "ticker": "H.US",
                "timestamp": hourly,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "adj_close": close,
                "volume": 1.0,
            }
        ),
        interval=Interval.HOUR_1,
    )
    # vendor daily bars with a sentinel price that must never be seen
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "H.US",
                "date": [d.date() for d in days],
                "open": 999.0,
                "high": 999.0,
                "low": 999.0,
                "close": 999.0,
                "adj_close": 999.0,
                "volume": 1.0,
            }
        )
    )
    ds = LabDataset(
        lake=lake,
        universe=["H.US"],
        start=days[0].date(),
        end=days[-1].date(),
        train_ratio=0.5,
        interval=Interval.HOUR_1,
    )
    _reset_spy()
    try:
        MonteCarloPermutationTest(n_permutations=2, max_p_value=1.0, seed=1).run(_Spy({}), ds)
    finally:
        lake.close()
    assert len(_Spy.history) == 3
    for hist in _Spy.history.values():
        assert not hist.empty
        assert (hist["close"] != 999.0).all()


class _WindowTuner:
    """Fake tuner: best score = mean close its dataset sees in the train
    window; records every dataset it was handed."""

    def __init__(self) -> None:
        self.datasets: list[tuple[LabDataset, pd.DataFrame]] = []

    def tune(self, strategy_cls, param_space, objective, dataset, budget):
        bars = dataset.lake.get_bars("RND.US", Interval.DAY_1, FAR_PAST, FAR_FUTURE)
        self.datasets.append((dataset, bars))
        start, end = dataset.train_window
        ts = bars["timestamp"].dt.date
        score = float(bars[(ts >= start) & (ts <= end)]["close"].mean())
        params = {"ticker": "RND.US", "allocation": 1.0}
        return TunerResult(best_params=params, best_score=score, history=[(params, score)])


def test_retune_requires_a_tuning_setup(lake_gbm):
    lake, dates = lake_gbm
    with pytest.raises(ValueError, match="tuning"):
        MonteCarloPermutationTest(n_permutations=2, retune=True).run(
            BuyAndHold({"ticker": "RND.US"}), _gbm_dataset(lake, dates)
        )


def test_retune_re_tunes_on_each_permuted_train_window(lake_gbm):
    lake, dates = lake_gbm
    ds = _gbm_dataset(lake, dates)
    tuner = _WindowTuner()
    test = MonteCarloPermutationTest(n_permutations=3, max_p_value=1.0, retune=True, seed=2)
    test.bind_tuning(TuningSetup(tuner=tuner, objective=SharpeObjective(), budget=4))
    report = test.run(BuyAndHold({"ticker": "RND.US"}), ds)

    train_start, train_end = ds.train_window
    real = lake.get_bars("RND.US", Interval.DAY_1, FAR_PAST, FAR_FUTURE)
    real_ts = real["timestamp"].dt.date
    real_train = real[(real_ts >= train_start) & (real_ts <= train_end)]["close"]

    assert len(tuner.datasets) == 4  # real + 3 permutations
    variants = set()
    for dataset, bars in tuner.datasets:
        assert dataset.train_window == ds.train_window
        assert dataset.lake is not lake
        ts = bars["timestamp"].dt.date
        assert (ts <= train_end).all()  # validation bars never reach the tuner
        before = bars[ts < train_start]["close"].reset_index(drop=True)
        pd.testing.assert_series_equal(
            before, real[real_ts < train_start]["close"].reset_index(drop=True)
        )
        variants.add(tuple(np.round(bars[ts >= train_start]["close"].to_numpy(), 10)))
    assert len(variants) == 4
    assert report.metrics["real_score"] == pytest.approx(float(real_train.mean()))
    assert "retune" in report.notes


def test_retune_keeps_the_wrapped_inner_strategy(lake_gbm):
    from stonks.lab.tuning.grid import GridTuner
    from stonks.strategies.macro_regime import MacroRegimeFilter

    class _InnerRecorder:
        name = "inner_recorder"
        direction = "maximize"

        def __init__(self) -> None:
            self.seen: list = []

        def score(self, strategy, dataset):
            self.seen.append((type(strategy.inner), dict(strategy.params["inner_params"])))
            return 0.0

    lake, dates = lake_gbm
    objective = _InnerRecorder()
    strategy = MacroRegimeFilter(
        {
            "inner_class_path": "stonks.strategies.examples.buy_and_hold:BuyAndHold",
            "inner_params": {"ticker": "RND.US"},
        }
    )
    test = MonteCarloPermutationTest(
        n_permutations=2,
        max_p_value=1.0,
        retune=True,
        tuning=TuningSetup(tuner=GridTuner(grid_size=2), objective=objective, budget=2),
    )
    test.run(strategy, _gbm_dataset(lake, dates))
    assert objective.seen
    for inner_cls, inner_params in objective.seen:
        assert inner_cls is BuyAndHold
        assert inner_params == {"ticker": "RND.US", "allocation": 1.0}


def test_explicit_tuning_wins_over_the_runner_binding(lake_gbm):
    lake, dates = lake_gbm
    mine, runners = _WindowTuner(), _WindowTuner()
    test = MonteCarloPermutationTest(
        n_permutations=1,
        max_p_value=1.0,
        retune=True,
        tuning=TuningSetup(tuner=mine, objective=SharpeObjective(), budget=1),
    )
    test.bind_tuning(TuningSetup(tuner=runners, objective=SharpeObjective(), budget=1))
    test.run(BuyAndHold({"ticker": "RND.US"}), _gbm_dataset(lake, dates))
    assert len(mine.datasets) == 2
    assert runners.datasets == []


# ---- parallel permutations ---------------------------------------------------


def _multi_ticker_dataset(tmp_path):
    rng = np.random.default_rng(8)
    dates = pd.bdate_range(start="2025-01-02", periods=220)
    lake = DuckDBLake(tmp_path / "multi.duckdb")
    lake.migrate()
    rows = []
    for ticker in ("A.US", "B.US"):
        close = np.exp(np.log(80.0) + np.cumsum(rng.normal(0.0004, 0.02, len(dates))))
        rows += [
            {
                "ticker": ticker,
                "date": d.date(),
                "open": c * 0.999,
                "high": c * 1.01,
                "low": c * 0.99,
                "close": c,
                "adj_close": c,
                "volume": 1_000,
            }
            for d, c in zip(dates, close, strict=False)
        ]
    lake.upsert_prices(pd.DataFrame(rows))
    ds = LabDataset(
        lake=lake,
        universe=["A.US", "B.US"],
        start=dates[30].date(),
        end=dates[-1].date(),
        train_ratio=0.6,
        interval=Interval.DAY_1,
    )
    return lake, ds


def test_parallel_permutations_are_bit_identical_to_serial(tmp_path):
    lake, ds = _multi_ticker_dataset(tmp_path)
    try:
        strategy = Momentum({"lookback_days": 5})
        reports = [
            MonteCarloPermutationTest(
                n_permutations=6, max_p_value=1.0, metric="sharpe", seed=3, max_workers=w
            ).run(strategy, ds)
            for w in (1, 3)
        ]
        assert reports[0].metrics == reports[1].metrics
        assert reports[0].metrics["perm_score_max"] != reports[0].metrics["perm_score_mean"]
    finally:
        lake.close()


def test_parallel_retune_is_bit_identical_to_serial(tmp_path):
    from stonks.lab.tuning.grid import GridTuner

    lake, ds = _multi_ticker_dataset(tmp_path)
    try:
        setup = TuningSetup(tuner=GridTuner(grid_size=3), objective=SharpeObjective(), budget=3)
        reports = [
            MonteCarloPermutationTest(
                n_permutations=4, max_p_value=1.0, retune=True, seed=5, tuning=setup, max_workers=w
            ).run(Momentum({"lookback_days": 5}), ds)
            for w in (1, 2)
        ]
        assert reports[0].metrics == reports[1].metrics
    finally:
        lake.close()


def test_parallel_scores_a_fitted_strategy_like_serial(lake_gbm):
    from stonks.strategies.examples.rsi_pca import RSIPCAStrategy

    lake, dates = lake_gbm
    ds = _gbm_dataset(lake, dates)
    strategy = RSIPCAStrategy(
        {
            "ticker": "RND.US",
            "n_components": 2,
            "long_quantile": 0.8,
            "lookahead": 3,
            "rsi_period_max": 8,
        }
    )
    strategy.fit(ds)
    reports = [
        MonteCarloPermutationTest(
            n_permutations=3, max_p_value=1.0, metric="sharpe", seed=9, max_workers=w
        ).run(strategy, ds)
        for w in (1, 2)
    ]
    assert reports[0].metrics == reports[1].metrics
    assert reports[0].metrics["real_score"] != 0.0  # it actually traded


def test_max_workers_must_be_positive():
    with pytest.raises(ValueError):
        MonteCarloPermutationTest(max_workers=0)


# ---- edge cases ------------------------------------------------------------------------


class _MinimizeSharpe(SharpeObjective):
    direction = "minimize"
    name = "neg_sharpe"


def _fixed_scores(monkeypatch, real: float, perms: list[float]):
    from stonks.lab.survival import permutation as perm

    monkeypatch.setattr(perm.PermutationScorer, "score_real", lambda self: real)
    monkeypatch.setattr(perm, "permuted_scores", lambda *a, **k: list(perms))


def test_minimize_in_retune_mode_counts_lower_scores(lake_gbm, monkeypatch):
    lake, dates = lake_gbm
    _fixed_scores(monkeypatch, 1.0, [2.0, 3.0, 0.5])
    test = MonteCarloPermutationTest(n_permutations=3, max_p_value=1.0, retune=True)
    test.bind_tuning(TuningSetup(tuner=_WindowTuner(), objective=_MinimizeSharpe(), budget=1))
    report = test.run(BuyAndHold({"ticker": "RND.US"}), _gbm_dataset(lake, dates))
    # only 0.5 is at least as good as 1.0 when lower is better
    assert report.metrics["p_value"] == pytest.approx(2 / 4)


def test_real_and_permuted_scores_all_inf_fail(lake_gbm, monkeypatch):
    lake, dates = lake_gbm
    inf = float("inf")
    _fixed_scores(monkeypatch, inf, [inf, inf, inf])
    report = MonteCarloPermutationTest(n_permutations=3).run(
        BuyAndHold({"ticker": "RND.US"}), _gbm_dataset(lake, dates)
    )
    assert report.metrics["p_value"] == pytest.approx(1.0)
    assert report.passed is False


def test_a_nan_real_score_fails(lake_gbm, monkeypatch):
    lake, dates = lake_gbm
    _fixed_scores(monkeypatch, float("nan"), [0.1, 0.2, 0.3])
    report = MonteCarloPermutationTest(n_permutations=3).run(
        BuyAndHold({"ticker": "RND.US"}), _gbm_dataset(lake, dates)
    )
    assert report.passed is False
    assert "insufficient data" in report.notes


def test_profit_factor_metric_reads_the_bar_profit_factor(monkeypatch):
    # RS-35: the MCPT reads bar_profit_factor, never the deprecated alias
    from types import SimpleNamespace

    from stonks.lab.survival import permutation as perm

    monkeypatch.setattr(
        perm, "run_backtest", lambda *a, **k: SimpleNamespace(bar_profit_factor=2.5)
    )
    score = perm.BacktestScore("profit_factor", (date(2025, 1, 1), date(2025, 2, 1)))
    assert score(None, None) == 2.5
    assert perm.BacktestScore("bar_profit_factor", score.window)(None, None) == 2.5
