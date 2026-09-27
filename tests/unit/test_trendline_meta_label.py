"""TrendlineMetaLabelStrategy: trendline breakouts filtered by a random forest."""

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
from stonks.strategies.examples.trendline_meta_label import (
    FEATURE_NAMES,
    TrendlineMetaLabelStrategy,
    adx,
)
from tests.unit.nt888_helpers import make_lake, write_bars

DATES = pd.bdate_range("2020-01-01", periods=700)
TRAIN_END_I = 549
TRAIN_END = DATES[TRAIN_END_I].date()


def _closes(seed=8):
    rng = np.random.default_rng(seed)
    drift = np.where((np.arange(len(DATES)) // 60) % 2 == 0, 0.002, -0.001)
    return 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.012, len(DATES))))


def _volumes(seed=9):
    return np.random.default_rng(seed).uniform(5e5, 2e6, len(DATES))


@pytest.fixture
def lake(tmp_path):
    db = make_lake(tmp_path / "lake.duckdb")
    write_bars(db, "X.US", DATES, _closes(), _volumes())
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
    params = {
        "ticker": "X.US",
        "lookback": 24,
        "hold_period": 6,
        "tp_mult": 2.0,
        "sl_mult": 2.0,
        "atr_lookback": 50,
        "n_estimators": 50,
        **overrides,
    }
    return TrendlineMetaLabelStrategy(params)


PROBE = np.array([[0.01, 0.5, 1.0, 1.0, 20.0], [-0.02, 1.5, 3.0, 0.5, 40.0]])


# ---- ADX --------------------------------------------------------------------------


def test_adx_is_100_in_a_pure_uptrend():
    close = pd.Series(np.arange(100.0, 200.0))
    out = adx(close + 1.0, close - 1.0, close, 14)
    assert np.isnan(out.iloc[0])
    assert out.iloc[-1] == pytest.approx(100.0)


def test_adx_is_low_without_direction():
    close = pd.Series(100.0 + np.tile([0.0, 1.0], 100))
    out = adx(close + 1.0, close - 1.0, close, 14)
    assert out.iloc[-1] < 20.0


def test_adx_is_causal():
    rng = np.random.default_rng(0)
    close = pd.Series(100 + np.cumsum(rng.normal(size=200)))
    full = adx(close + 1, close - 1, close, 14)
    head = adx(close[:120] + 1, close[:120] - 1, close[:120], 14)
    np.testing.assert_allclose(full[:120], head)


# ---- surface ----------------------------------------------------------------------


def test_surface_and_params():
    s = _strategy()
    assert isinstance(s, Strategy)
    assert s.applicable_asset_classes == ("crypto", "equity")
    spec = {p.name: p for p in TrendlineMetaLabelStrategy.parameter_spec()}
    assert spec["lookback"].bounds == (24, 300) and spec["lookback"].default == 72
    assert spec["hold_period"].bounds == (4, 48)
    assert spec["tp_mult"].bounds == (1, 6) and spec["sl_mult"].bounds == (1, 6)
    assert spec["atr_lookback"].bounds == (50, 400)
    assert spec["prob_thresh"].bounds == (0.0, 0.8)
    assert spec["prob_margin"].bounds == (0.0, 0.2)
    assert not spec["bet_sizing"].tunable and not spec["cv_folds"].tunable
    assert not spec["seed"].tunable and not spec["n_estimators"].tunable
    assert FEATURE_NAMES == ("resist_slope", "tl_err", "max_dist", "volume", "adx")


def test_unfitted_strategy_abstains(lake):
    s = _strategy()
    assert all(
        s.estimate_return("X.US", DATES[i].to_pydatetime(), lake) is None
        for i in range(560, 700, 5)
    )


# ---- fit ------------------------------------------------------------------------------


def test_fit_reads_training_bars_through_the_strategy_bar_cache(lake):
    s = _strategy()
    assert len(s._bar_caches) == 0
    s.fit(_dataset(lake))
    assert len(s._bar_caches) == 1  # roadmap 11.7: one fetch shared with predictions


def test_fit_builds_a_trade_dataset_from_complete_trades_only(lake):
    s = _strategy()
    s.fit(_dataset(lake))
    assert s.is_fitted
    trades = s.training_trades
    assert len(trades) >= 5
    last_train_ts = DATES[TRAIN_END_I]
    for t in trades:
        assert t.exit_ts <= last_train_ts
        assert t.entry_ts < t.exit_ts or t.exit_ts == t.entry_ts
        assert len(t.features) == len(FEATURE_NAMES)
        assert t.label == (t.exit_log_price > t.entry_log_price)
    assert s.fitted_state()["n_trades"] == len(trades)


def test_fit_is_deterministic(lake):
    a, b = _strategy(), _strategy()
    a.fit(_dataset(lake))
    b.fit(_dataset(lake))
    np.testing.assert_array_equal(a.predict_proba(PROBE), b.predict_proba(PROBE))
    assert a.fitted_state() == b.fitted_state()


def test_fit_never_reads_bars_after_train_end(lake, tmp_path):
    closes, volumes = _closes(), _volumes()
    closes[TRAIN_END_I + 1 :] *= np.linspace(0.4, 3.0, len(closes) - TRAIN_END_I - 1)
    volumes[TRAIN_END_I + 1 :] *= 10
    other = make_lake(tmp_path / "future.duckdb")
    try:
        write_bars(other, "X.US", DATES, closes, volumes)
        a, b = _strategy(), _strategy()
        a.fit(_dataset(lake))
        b.fit(_dataset(other))
        assert a.fitted_state() == b.fitted_state()
        assert [t.features for t in a.training_trades] == [t.features for t in b.training_trades]
        np.testing.assert_array_equal(a.predict_proba(PROBE), b.predict_proba(PROBE))
    finally:
        other.close()


def test_training_features_match_inference_features(lake):
    """A trade's features are computed the same way in fit and at run time."""
    s = _strategy()
    s.fit(_dataset(lake))
    t = s.training_trades[-1]
    live = s.open_trade("X.US", t.entry_ts.to_pydatetime(), lake)
    assert live is not None
    assert live.entry_ts == t.entry_ts
    np.testing.assert_allclose(live.features, t.features)


# ---- inference --------------------------------------------------------------------


class _FixedProb:
    def __init__(self, p):
        self.p = p

    def predict_proba(self, x):
        return np.full(len(np.atleast_2d(x)), self.p)


def _open_trade_day(s, lake):
    for i in range(560, 700):
        as_of = DATES[i].to_pydatetime()
        if s.open_trade("X.US", as_of, lake) is not None:
            return as_of
    pytest.fail("no base breakout in the validation window")


def test_probability_gate(lake):
    s = _strategy(prob_thresh=0.6)
    s.fit(_dataset(lake))
    as_of = _open_trade_day(s, lake)
    s._classifier = _FixedProb(0.9)
    s._prob_memo.clear()
    assert s.estimate_return("X.US", as_of, lake) is not None
    s._classifier = _FixedProb(0.55)
    s._prob_memo.clear()
    assert s.estimate_return("X.US", as_of, lake) is None
    assert s.estimate_return("OTHER.US", as_of, lake) is None


def test_no_open_trade_means_no_estimate(lake):
    s = _strategy()
    s.fit(_dataset(lake))
    s._classifier = _FixedProb(1.0)
    for i in range(560, 700, 3):
        as_of = DATES[i].to_pydatetime()
        if s.open_trade("X.US", as_of, lake) is None:
            assert s.estimate_return("X.US", as_of, lake) is None


def test_save_load_round_trip(lake, tmp_path):
    s = _strategy()
    s.fit(_dataset(lake))
    s.save(tmp_path / "art")
    meta = json.loads((tmp_path / "art" / "fitted_state.json").read_text())
    assert meta["feature_names"] == list(FEATURE_NAMES)
    loaded = TrendlineMetaLabelStrategy.load(tmp_path / "art")
    assert loaded.params == s.params
    np.testing.assert_array_equal(loaded.predict_proba(PROBE), s.predict_proba(PROBE))
    for i in range(560, 700, 4):
        as_of = DATES[i].to_pydatetime()
        assert loaded.estimate_return("X.US", as_of, lake) == s.estimate_return("X.US", as_of, lake)


def test_unfitted_save_load_stays_unfitted(tmp_path):
    s = _strategy()
    s.save(tmp_path / "art")
    assert not TrendlineMetaLabelStrategy.load(tmp_path / "art").is_fitted


def test_short_backtest_runs(lake):
    s = _strategy()
    s.fit(_dataset(lake))
    broker = SimulatedBroker(Portfolio(cash=10_000.0))
    config = BacktestConfig(start=DATES[560].date(), end=DATES[-1].date(), universe=["X.US"])
    report = Backtester([s], broker, lake, config).run()
    assert len(report.equity_curve) == 140
    assert all(np.isfinite(report.equity_curve))


# ---- ML hygiene (BL-45) -------------------------------------------------------------


def test_threshold_is_the_break_even_probability_plus_margin():
    assert _strategy().threshold == pytest.approx(0.5)  # tp = sl
    assert _strategy(tp_mult=3.0, sl_mult=1.0).threshold == pytest.approx(0.25)
    assert _strategy(tp_mult=3.0, sl_mult=1.0, prob_margin=0.1).threshold == pytest.approx(0.35)
    assert _strategy(tp_mult=3.0, sl_mult=1.0, prob_thresh=0.6).threshold == pytest.approx(0.6)


def test_break_even_threshold_admits_a_cheap_win(lake):
    """With tp = 3 * sl a 40% win rate has a positive expectancy, so it trades."""
    s = _strategy(tp_mult=3.0, sl_mult=1.0)
    s.fit(_dataset(lake))
    as_of = _open_trade_day(s, lake)
    s._classifier = _FixedProb(0.4)
    s._prob_memo.clear()
    assert s.estimate_return("X.US", as_of, lake) is not None
    s._classifier = _FixedProb(0.2)
    s._prob_memo.clear()
    assert s.estimate_return("X.US", as_of, lake) is None


def test_fit_records_uniqueness_and_the_purged_cv_diagnostic(lake):
    s = _strategy()
    s.fit(_dataset(lake))
    state = s.fitted_state()
    assert state["n_segments"] == 1
    assert state["mean_uniqueness"] == pytest.approx(1.0)  # base trades never overlap
    assert state["cv_folds"] == 3.0
    assert 0.0 <= state["cv_accuracy"] <= 1.0 and 0.0 <= state["cv_brier"] <= 1.0
    off = _strategy(cv_folds=0)
    off.fit(_dataset(lake))
    assert "cv_accuracy" not in off.fitted_state()


def test_fit_on_cv_segments_never_spans_a_gap(lake):
    """A CV fold trains on segments either side of a test block: no trade
    may start in one segment and end in another, and nothing is read from
    the gap."""
    ds = _dataset(lake)
    gap_start, gap_end = DATES[200].date(), DATES[300].date()
    fold = ds.with_train_segments(
        [(DATES[0].date(), DATES[199].date()), (DATES[301].date(), TRAIN_END)]
    )
    s = _strategy()
    s.fit(fold)
    assert s.fitted_state()["n_segments"] == 2
    for t in s.training_trades:
        entry, exit_ = t.entry_ts.date(), t.exit_ts.date()
        assert not (gap_start <= entry <= gap_end) and not (gap_start <= exit_ <= gap_end)
        assert (entry < gap_start) == (exit_ < gap_start)


def test_bet_sizing_scales_the_entry(lake):
    s = _strategy(bet_sizing=True)
    s.fit(_dataset(lake))
    as_of = _open_trade_day(s, lake)
    s._classifier = _FixedProb(0.7)
    s._prob_memo.clear()
    assert s.estimate_return("X.US", as_of, lake) is not None
    orders = s.decide([(1.0, "X.US")], Portfolio(cash=1000.0), {"X.US": 10.0}, as_of)
    # bet_size(0.7) = 2 * Phi(0.436) - 1 = 0.337, rounded to 0.3
    assert orders[0].quantity == pytest.approx(1000.0 * 0.3 / 10.0)
    s._classifier = _FixedProb(0.52)  # admitted, but the bet size rounds to 0
    s._prob_memo.clear()
    assert s.estimate_return("X.US", as_of, lake) is None
