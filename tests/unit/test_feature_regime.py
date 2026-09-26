"""FeatureRegimeFilter: gate any strategy on a per-ticker complexity feature."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.core.types import Portfolio
from stonks.features.complexity import (
    rolling_permutation_entropy,
    rolling_ptsr,
    rolling_rai,
    rolling_runs_z,
)
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.feature_regime import FeatureRegimeFilter
from tests.unit.nt888_helpers import STUB, FittedStub, make_lake, write_bars

BUY_AND_HOLD = "stonks.strategies.examples.buy_and_hold:BuyAndHold"
DATES = pd.bdate_range("2023-01-02", periods=200)


def _series(seed=0):
    rng = np.random.default_rng(seed)
    noisy = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 100)))
    trend = noisy[-1] * np.exp(0.005 * np.arange(1, 101))
    closes = np.concatenate([noisy, trend])
    volumes = np.concatenate([rng.uniform(1e5, 2e5, 100), np.full(100, 1.5e5)])
    return closes, volumes


@pytest.fixture
def lake(tmp_path):
    db = make_lake(tmp_path / "lake.duckdb")
    closes, volumes = _series()
    write_bars(db, "X.US", DATES, closes, volumes)
    yield db
    db.close()


NOISY_DAY = DATES[95].to_pydatetime()
TREND_DAY = DATES[190].to_pydatetime()


def _filter(**overrides):
    params = {
        "inner_class_path": BUY_AND_HOLD,
        "inner_params": {"ticker": "X.US"},
        "feature": "perm_entropy_close",
        "window": 24,
        "threshold": 0.5,
        "direction": "risk_off_below",
        **overrides,
    }
    return FeatureRegimeFilter(params)


# ---- surface ------------------------------------------------------------------


def test_satisfies_the_protocol_and_mirrors_the_inner_strategy():
    f = _filter()
    assert isinstance(f, Strategy)
    assert isinstance(f.inner, BuyAndHold)
    assert f.applicable_asset_classes == BuyAndHold.applicable_asset_classes
    assert f.id.startswith("buy_and_hold")
    assert f.params["inner_params"] == {"ticker": "X.US", "allocation": 1.0}


def test_parameter_spec():
    spec = {p.name: p for p in FeatureRegimeFilter.parameter_spec()}
    assert spec["threshold"].tunable
    assert spec["window"].tunable
    assert not spec["d"].tunable and spec["d"].bounds == (3, 5)
    assert set(spec["feature"].bounds) == {
        "perm_entropy_close",
        "perm_entropy_volume",
        "ptsr",
        "rai",
        "runs_z",
    }
    assert set(spec["direction"].bounds) == {"risk_off_above", "risk_off_below"}


def test_bad_inner_class_path_is_rejected():
    with pytest.raises(ValueError, match="inner"):
        _filter(inner_class_path="nope.module:Thing")


# ---- regime -------------------------------------------------------------------


def test_trend_is_risk_off_and_noise_is_risk_on(lake):
    f = _filter()
    assert f.is_risk_off("X.US", NOISY_DAY, lake) is False
    assert f.is_risk_off("X.US", TREND_DAY, lake) is True
    assert f.estimate_return("X.US", NOISY_DAY, lake) == 1.0
    assert f.estimate_return("X.US", TREND_DAY, lake) is None


def test_direction_flips_the_regime(lake):
    f = _filter(direction="risk_off_above")
    assert f.is_risk_off("X.US", NOISY_DAY, lake) is True
    assert f.is_risk_off("X.US", TREND_DAY, lake) is False


def test_too_little_history_is_risk_on(lake):
    f = _filter(window=120, direction="risk_off_above", threshold=-1.0)
    assert f.feature_value("X.US", DATES[10].to_pydatetime(), lake) is None
    assert f.is_risk_off("X.US", DATES[10].to_pydatetime(), lake) is False


@pytest.mark.parametrize(
    ("feature", "reference"),
    [
        ("perm_entropy_close", lambda c, v: rolling_permutation_entropy(c, 30, 3)),
        ("perm_entropy_volume", lambda c, v: rolling_permutation_entropy(v, 30, 3)),
        ("ptsr", lambda c, v: rolling_ptsr(c, 30, 3)),
        ("rai", lambda c, v: rolling_rai(c, 30, smooth_com=None)),
        ("runs_z", lambda c, v: rolling_runs_z(c, 30)),
    ],
)
def test_feature_value_matches_the_rolling_reference(lake, feature, reference):
    closes, volumes = _series()
    f = _filter(feature=feature, window=30, rai_smooth_com=0.0)
    days = (60, 99) if feature == "ptsr" else (60, 99, 150)
    for i in days:
        expected = reference(closes[: i + 1], volumes[: i + 1])[-1]
        got = f.feature_value("X.US", DATES[i].to_pydatetime(), lake)
        if np.isnan(expected):
            assert got is None
        else:
            assert got == pytest.approx(expected)


def test_ptsr_carry_is_bounded_to_window_windows(lake):
    # deep in the pure trend every recent window misses a pattern; the
    # unbounded reference still carries a value from before the trend
    closes, _ = _series()
    assert not np.isnan(rolling_ptsr(closes[:181], 30, 3)[-1])
    f = _filter(feature="ptsr", window=30)
    assert f.feature_value("X.US", DATES[180].to_pydatetime(), lake) is None


def test_smoothed_rai_uses_a_bounded_tail(lake):
    f = _filter(feature="rai", window=30, rai_smooth_com=7.0)
    value = f.feature_value("X.US", DATES[150].to_pydatetime(), lake)
    closes, _ = _series()
    raw = rolling_rai(closes[:151], 30)
    tail = raw[-f.smoothing_tail() :]
    assert value == pytest.approx(pd.Series(tail).ewm(com=7.0).mean().iloc[-1])


@pytest.mark.parametrize("feature", ["perm_entropy_close", "ptsr", "rai", "runs_z"])
def test_feature_never_reads_bars_after_as_of(tmp_path, lake, feature):
    closes, volumes = _series()
    changed = closes.copy()
    changed[121:] *= np.linspace(0.5, 3.0, len(changed) - 121)
    other = make_lake(tmp_path / "future.duckdb")
    try:
        write_bars(other, "X.US", DATES, changed, volumes)
        f = _filter(feature=feature, window=30)
        as_of = DATES[120].to_pydatetime()
        assert f.feature_value("X.US", as_of, lake) == f.feature_value("X.US", as_of, other)
    finally:
        other.close()


# ---- decide -------------------------------------------------------------------


def test_decide_in_risk_on_delegates(lake):
    f = _filter()
    f.estimate_return("X.US", NOISY_DAY, lake)
    orders = f.decide([(1.0, "X.US")], Portfolio(cash=1000.0), {"X.US": 10.0}, NOISY_DAY)
    assert [(o.side, o.ticker) for o in orders] == [("buy", "X.US")]


def test_decide_in_risk_off_exits_the_position(lake):
    f = _filter()
    f.estimate_return("X.US", NOISY_DAY, lake)  # remembers the lake
    portfolio = Portfolio(cash=0.0, positions={"X.US": 5.0})
    # picks are ignored for a risk-off ticker
    orders = f.decide([(1.0, "X.US")], portfolio, {"X.US": 10.0}, TREND_DAY)
    assert [(o.side, o.ticker, o.quantity) for o in orders] == [("sell", "X.US", 5.0)]


def test_decide_without_a_lake_is_transparent():
    f = _filter()
    orders = f.decide([(1.0, "X.US")], Portfolio(cash=1000.0), {"X.US": 10.0}, TREND_DAY)
    assert [(o.side, o.ticker) for o in orders] == [("buy", "X.US")]


# ---- persistence ----------------------------------------------------------------


def test_save_load_round_trip_restores_inner_state(tmp_path, lake):
    f = _filter(inner_class_path=STUB, inner_params={"ticker": "X.US"}, threshold=0.4)
    f.fit(None)
    f.save(tmp_path / "art")
    loaded = FeatureRegimeFilter.load(tmp_path / "art")
    assert loaded.params == f.params
    assert isinstance(loaded.inner, FittedStub)
    assert loaded.inner.fitted_value == 42.0
    assert loaded.is_risk_off("X.US", TREND_DAY, lake) is True


def test_registry_round_trip(tmp_path, lake):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        f = _filter(inner_class_path=STUB, inner_params={"ticker": "X.US"})
        f.fit(None)
        sid = registry.register(f, [SurvivalReport(test_id="oos", passed=True, metrics={})])
        loaded = registry.load(sid)
        assert isinstance(loaded, FeatureRegimeFilter)
        assert loaded.inner.fitted_value == 42.0
        assert loaded.estimate_return("X.US", NOISY_DAY, lake) == 1.0
    finally:
        state.close()
