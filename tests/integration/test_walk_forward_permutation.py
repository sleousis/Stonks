"""WalkForwardPermutationTest: Masters' walk-forward MCPT — the real
walk-forward OOS score against the same walk-forward run on lakes whose
bars after the first training window are permuted."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pydantic
import pytest

from stonks.core.interval import Interval
from stonks.core.protocols import TunerResult
from stonks.lab.dataset import LabDataset
from stonks.lab.objectives import SharpeObjective
from stonks.lab.survival.base import TuningSetup
from stonks.lab.survival.walk_forward import WalkForwardConfig, WalkForwardTest
from stonks.lab.survival.walk_forward_permutation import (
    WalkForwardPermutationConfig,
    WalkForwardPermutationTest,
)
from stonks.lab.tuning.grid import GridTuner
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.momentum import Momentum

FAR_PAST, FAR_FUTURE = datetime(1990, 1, 1), datetime(2100, 1, 1)
TICKERS = ("A.US", "B.US")


@pytest.fixture
def ds(tmp_path):
    rng = np.random.default_rng(21)
    dates = pd.bdate_range(start="2025-01-02", periods=260)
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    rows = []
    for ticker in TICKERS:
        close = np.exp(np.log(60.0) + np.cumsum(rng.normal(0.0003, 0.02, len(dates))))
        rows += [
            {
                "ticker": ticker,
                "date": d.date(),
                "open": c * 1.001,
                "high": c * 1.01,
                "low": c * 0.99,
                "close": c,
                "adj_close": c,
                "volume": 1_000,
            }
            for d, c in zip(dates, close, strict=False)
        ]
    lake.upsert_prices(pd.DataFrame(rows))
    yield LabDataset(
        lake=lake,
        universe=list(TICKERS),
        start=dates[20].date(),
        end=dates[-15].date(),  # real bars after the dataset end must never be used
        train_ratio=0.6,
        interval=Interval.DAY_1,
    )
    lake.close()


WF = WalkForwardConfig(n_splits=2, metric="sharpe")


def _setup(budget=3):
    # threshold pinned at 0 so every candidate trades; lookback in {5, 128, 252}
    return TuningSetup(
        tuner=GridTuner(grid_size=3),
        objective=SharpeObjective(),
        budget=budget,
        fixed_params={"threshold": 0.0},
    )


class _RecordingTuner:
    """Returns fixed Momentum params; records each train window it tunes on
    and the bars its lake holds."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict[str, pd.DataFrame]]] = []

    def tune(self, strategy_cls, param_space, objective, dataset, budget):
        bars = {t: dataset.lake.get_bars(t, Interval.DAY_1, FAR_PAST, FAR_FUTURE) for t in TICKERS}
        self.calls.append((dataset.train_window, bars))
        params = {"lookback_days": 5, "threshold": 0.0, "allocation": 1.0}
        return TunerResult(best_params=params, best_score=0.0, history=[])


def test_needs_a_tuning_setup(ds):
    with pytest.raises(ValueError, match="tuning"):
        WalkForwardPermutationTest().run(Momentum({}), ds)


def test_real_score_is_the_walk_forward_oos_mean(ds):
    setup = _setup()
    expected = WalkForwardTest(WF, setup).run(Momentum({}), ds).metrics["oos_score_mean"]
    cfg = WalkForwardPermutationConfig(walk_forward=WF, n_permutations=2, max_p_value=1.0)
    report = WalkForwardPermutationTest(cfg, setup).run(Momentum({}), ds)
    assert report.test_id == "walk_forward_mcpt"
    assert report.metrics["real_score"] == expected
    assert report.metrics["n_permutations"] == 2.0
    assert report.metrics["n_folds"] == 2.0
    assert 0.0 < report.metrics["p_value"] <= 1.0
    assert report.passed is True  # max_p_value=1.0


def test_only_bars_after_the_first_training_window_are_permuted(ds):
    tuner = _RecordingTuner()
    folds = WF.folds_for(ds, Momentum({}))
    first_train_end = folds[0].train_end
    cfg = WalkForwardPermutationConfig(walk_forward=WF, n_permutations=3, max_p_value=1.0, seed=4)
    WalkForwardPermutationTest(cfg, TuningSetup(tuner, SharpeObjective(), budget=1)).run(
        Momentum({}), ds
    )

    real = {t: ds.lake.get_bars(t, Interval.DAY_1, FAR_PAST, FAR_FUTURE) for t in TICKERS}
    assert len(tuner.calls) == 4 * len(folds)  # (real + 3 permutations) x folds
    assert [w for w, _ in tuner.calls[: len(folds)]] == [
        (f.train_start, f.train_end) for f in folds
    ]
    later_variants = set()
    for _, bars_by_ticker in tuner.calls:
        for ticker, bars in bars_by_ticker.items():
            ts = bars["timestamp"].dt.date
            assert ts.max() <= ds.end  # nothing past the dataset end
            real_bars = real[ticker][real[ticker]["timestamp"].dt.date <= ds.end]
            pd.testing.assert_series_equal(  # same timeline as the real bars
                bars["timestamp"].reset_index(drop=True),
                real_bars["timestamp"].reset_index(drop=True),
            )
            early = ts <= first_train_end
            np.testing.assert_array_equal(  # the first training window stays real
                bars[early.to_numpy()]["close"].to_numpy(),
                real_bars[(real_bars["timestamp"].dt.date <= first_train_end).to_numpy()][
                    "close"
                ].to_numpy(),
            )
            if ticker == TICKERS[0]:
                later_variants.add(tuple(np.round(bars[~early.to_numpy()]["close"], 10)))
    assert len(later_variants) == 4  # real + 3 distinct permutations


def test_same_seed_same_report_and_seed_changes_the_null(ds):
    def run(seed):
        cfg = WalkForwardPermutationConfig(
            walk_forward=WF, n_permutations=3, max_p_value=1.0, seed=seed
        )
        return WalkForwardPermutationTest(cfg, _setup()).run(Momentum({}), ds)

    a, b, c = run(1), run(1), run(2)
    assert a.metrics == b.metrics
    assert a.metrics["perm_score_mean"] != c.metrics["perm_score_mean"]


def test_parallel_is_bit_identical_to_serial(ds):
    reports = [
        WalkForwardPermutationTest(
            WalkForwardPermutationConfig(
                walk_forward=WF, n_permutations=3, max_p_value=1.0, seed=7, max_workers=w
            ),
            _setup(),
        ).run(Momentum({}), ds)
        for w in (1, 3)
    ]
    assert reports[0].metrics == reports[1].metrics
    assert reports[0].metrics["perm_score_max"] != reports[0].metrics["perm_score_mean"]


def test_runner_binding_is_used_when_no_tuning_is_given(ds):
    tuner = _RecordingTuner()
    test = WalkForwardPermutationTest(
        WalkForwardPermutationConfig(walk_forward=WF, n_permutations=1, max_p_value=1.0)
    )
    test.bind_tuning(TuningSetup(tuner, SharpeObjective(), budget=1))
    test.run(Momentum({}), ds)
    assert len(tuner.calls) == 2 * 2


@pytest.mark.parametrize(
    "kwargs",
    [{"n_permutations": 0}, {"max_p_value": 0.0}, {"max_p_value": 1.5}, {"max_workers": 0}],
)
def test_config_rejects_bad_values(kwargs):
    with pytest.raises(pydantic.ValidationError):
        WalkForwardPermutationConfig(**kwargs)


class _LabelledMomentum(Momentum):
    """Labels ten bars ahead: the folds need a ten-bar embargo (BL-20)."""

    label_horizon_bars = 10


def test_folds_honour_the_strategys_label_horizon(ds, monkeypatch):
    import stonks.lab.survival.walk_forward_permutation as wfp

    windows = []

    def build(strategy, context, window, evaluate):
        windows.append(window)
        return  # skip the permutations; only the window matters here

    monkeypatch.setattr(wfp.PermutationScorer, "build", staticmethod(build))
    strategy = _LabelledMomentum({})
    cfg = WalkForwardPermutationConfig(walk_forward=WF, n_permutations=1, max_p_value=1.0)
    WalkForwardPermutationTest(cfg, _setup(budget=1)).run(strategy, ds)
    embargoed = WF.folds_for(ds, strategy)[0].test_start
    assert embargoed != WF.folds_for(ds)[0].test_start
    assert windows == [(embargoed, ds.end)]
