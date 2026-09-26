"""PIPMinerStrategy: cluster perceptually-important-point patterns, trade the best."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.protocols import Strategy
from stonks.core.types import Portfolio
from stonks.lab.dataset import LabDataset
from stonks.strategies.examples.pip_miner import PIPMinerStrategy, find_pips
from tests.unit.nt888_helpers import make_lake, write_bars

DATES = pd.bdate_range("2021-01-04", periods=520)
TRAIN_END = DATES[399].date()


def _closes(seed=3):
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, len(DATES))))


@pytest.fixture
def lake(tmp_path):
    db = make_lake(tmp_path / "lake.duckdb")
    write_bars(db, "X.US", DATES, _closes())
    yield db
    db.close()


def _dataset(lake):
    return LabDataset(
        lake=lake,
        universe=["X.US"],
        start=DATES[0].date(),
        end=DATES[-1].date(),
        interval=Interval.DAY_1,
        train_end=TRAIN_END,
    )


def _strategy(**overrides):
    return PIPMinerStrategy({"ticker": "X.US", "k_min": 5, "k_max": 10, **overrides})


def _state(strategy, tmp_path, name):
    strategy.save(tmp_path / name)
    return json.loads((tmp_path / name / "fitted_state.json").read_text())


# ---- PIPs -------------------------------------------------------------------------


def test_find_pips_by_hand():
    data = np.array([0.0, 1.0, 0.0, 3.0, 0.0])
    x, y = find_pips(data, 3)
    assert x == [0, 3, 4] and y == [0.0, 3.0, 0.0]
    x, _ = find_pips(data, 4)
    assert x == [0, 2, 3, 4]


def test_find_pips_endpoints_only():
    x, y = find_pips(np.array([1.0, 5.0, 2.0]), 2)
    assert x == [0, 2] and y == [1.0, 2.0]


# ---- surface ----------------------------------------------------------------------


def test_surface_and_params():
    s = _strategy()
    assert isinstance(s, Strategy)
    assert s.applicable_asset_classes == ("crypto", "equity")
    spec = {p.name: p for p in PIPMinerStrategy.parameter_spec()}
    assert spec["n_pips"].bounds == (3, 8) and spec["n_pips"].default == 5
    assert spec["lookback"].bounds == (12, 96) and spec["lookback"].default == 24
    assert spec["hold"].bounds == (1, 24) and spec["hold"].default == 6
    assert not spec["seed"].tunable


def test_unfitted_strategy_abstains(lake):
    assert _strategy().estimate_return("X.US", DATES[450].to_pydatetime(), lake) is None


# ---- fit ------------------------------------------------------------------------------


def test_fit_reads_training_bars_through_the_strategy_bar_cache(lake):
    s = _strategy()
    assert len(s._bar_caches) == 0
    s.fit(_dataset(lake))
    assert len(s._bar_caches) == 1  # roadmap 11.7: one fetch shared with predictions


def test_fit_picks_the_best_martin_cluster(lake):
    s = _strategy()
    s.fit(_dataset(lake))
    assert s.is_fitted
    state = s.fitted_state()
    k = len(state["centroids"])
    assert 5 <= k <= 10
    assert len(state["centroids"][0]) == 5
    martins = state["cluster_martins"]
    if state["long_cluster"] is not None:
        assert martins[state["long_cluster"]] == max(martins)
        assert state["long_mean_return"] > 0


def test_fit_is_deterministic(lake, tmp_path):
    a, b = _strategy(), _strategy()
    a.fit(_dataset(lake))
    b.fit(_dataset(lake))
    assert _state(a, tmp_path, "a") == _state(b, tmp_path, "b")


def test_fit_never_reads_bars_after_train_end(lake, tmp_path):
    closes = _closes()
    changed = closes.copy()
    changed[400:] = changed[400:] * np.linspace(0.3, 4.0, len(changed) - 400)
    other = make_lake(tmp_path / "future.duckdb")
    try:
        write_bars(other, "X.US", DATES, changed)
        a, b = _strategy(), _strategy()
        a.fit(_dataset(lake))
        b.fit(_dataset(other))
        assert _state(a, tmp_path, "a") == _state(b, tmp_path, "b")
    finally:
        other.close()


def test_forward_returns_stay_inside_the_train_window(lake, tmp_path):
    """Truncating the lake at train_end must not change the fit: patterns
    near train_end whose hold would cross it are simply not used."""
    trunc = make_lake(tmp_path / "trunc.duckdb")
    try:
        write_bars(trunc, "X.US", DATES[:400], _closes()[:400])
        a, b = _strategy(), _strategy()
        a.fit(_dataset(lake))
        b.fit(_dataset(trunc))
        assert _state(a, tmp_path, "a") == _state(b, tmp_path, "b")
    finally:
        trunc.close()


def test_fit_rejects_a_window_too_short(tmp_path):
    db = make_lake(tmp_path / "short.duckdb")
    try:
        write_bars(db, "X.US", DATES[:30], _closes()[:30])
        ds = LabDataset(
            lake=db,
            universe=["X.US"],
            start=DATES[0].date(),
            end=DATES[29].date(),
            train_end=DATES[25].date(),
        )
        with pytest.raises(ValueError):
            _strategy().fit(ds)
    finally:
        db.close()


# ---- inference --------------------------------------------------------------------


def test_signal_fires_on_a_best_cluster_pattern_and_holds(lake):
    s = _strategy()
    s.fit(_dataset(lake))
    best = s.fitted_state()["long_cluster"]
    if best is None:
        pytest.skip("no profitable cluster on this synthetic series")
    lookback, hold = 24, 6
    closes = np.log(_closes())
    hit = next(
        i
        for i in range(lookback - 1, 400)
        if s.nearest_cluster(closes[i - lookback + 1 : i + 1]) == best
    )
    expected = s.fitted_state()["long_mean_return"]
    for k in range(hold):
        as_of = DATES[hit + k].to_pydatetime()
        assert s.estimate_return("X.US", as_of, lake) == pytest.approx(expected)
    assert s.estimate_return("OTHER.US", DATES[hit].to_pydatetime(), lake) is None


def test_save_load_round_trip(lake, tmp_path):
    s = _strategy()
    s.fit(_dataset(lake))
    s.save(tmp_path / "art")
    assert not list((tmp_path / "art").glob("*.joblib"))
    assert not list((tmp_path / "art").glob("*.pkl"))
    loaded = PIPMinerStrategy.load(tmp_path / "art")
    assert loaded.params == s.params
    assert loaded.fitted_state() == s.fitted_state()
    for i in range(400, 520, 7):
        as_of = DATES[i].to_pydatetime()
        assert loaded.estimate_return("X.US", as_of, lake) == s.estimate_return("X.US", as_of, lake)


def test_short_backtest_runs(lake):
    s = _strategy()
    s.fit(_dataset(lake))
    broker = SimulatedBroker(Portfolio(cash=10_000.0), slippage_bps=0.0, fee_per_trade=0.0)
    config = BacktestConfig(start=DATES[400].date(), end=DATES[-1].date(), universe=["X.US"])
    report = Backtester([s], broker, lake, config).run()
    assert len(report.equity_curve) == 120
    assert all(np.isfinite(report.equity_curve))
