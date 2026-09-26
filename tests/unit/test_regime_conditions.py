"""Regime conditions (BL-42): each condition on a canned lake, point in time."""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from stonks.features.regime_conditions import (
    ConditionContext,
    RegimeCondition,
    build_condition,
    condition_kinds,
)
from tests.unit.nt888_helpers import make_lake, write_bars

DATES = pd.bdate_range("2020-01-01", periods=400)


def _at(i: int) -> datetime:
    return DATES[i].to_pydatetime()


def _ctx(lake) -> ConditionContext:
    return ConditionContext(lake)


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    db = make_lake(tmp_path_factory.mktemp("regime") / "lake.duckdb")
    # UP: rises for 300 bars then falls hard for 100.
    up = np.concatenate([100 * np.exp(0.002 * np.arange(300)), np.zeros(100)])
    up[300:] = up[299] * np.exp(-0.01 * np.arange(1, 101))
    write_bars(db, "IDX.US", DATES, up)
    # normal, then calm, then wild noise.
    rng = np.random.default_rng(7)
    rets = np.concatenate(
        [rng.normal(0, 0.01, 150), rng.normal(0, 0.002, 150), rng.normal(0, 0.04, 100)]
    )
    write_bars(db, "VOL.US", DATES, 100 * np.exp(np.cumsum(rets)))
    # macro: annual unemployment, stamped at period end
    db.upsert_macro_indicators(
        pd.DataFrame(
            [
                {
                    "country_iso": "USA",
                    "indicator": "unemployment",
                    "observation_date": d,
                    "period": "annual",
                    "country_name": "x",
                    "value": v,
                }
                for d, v in [(date(2019, 12, 31), 3.5), (date(2020, 12, 31), 6.0)]
            ]
        )
    )
    # yields: curve normal until 2020-06-30, inverted from 2020-07-01
    days = pd.date_range("2020-01-01", "2021-06-30", freq="D")
    rows = []
    for d in days:
        inverted = d >= pd.Timestamp("2020-07-01")
        rows.append({"ticker": "US10Y", "date": d.date(), "yield_to_maturity": 2.0})
        rows.append(
            {"ticker": "US3M", "date": d.date(), "yield_to_maturity": 2.5 if inverted else 1.0}
        )
    frame = pd.DataFrame(rows)
    frame["clean_price"] = None
    db.upsert_bond_yields(frame)
    yield db
    db.close()


# ---- registry ------------------------------------------------------------------------


def test_registry_holds_the_five_conditions():
    kinds = condition_kinds()
    for kind in ("macro", "price_trend", "realized_vol", "yield_curve", "higher_timeframe"):
        assert kind in kinds
        assert issubclass(kinds[kind], RegimeCondition)


def test_unknown_kind_and_bad_fields_are_rejected():
    with pytest.raises(ValueError, match="unknown regime condition"):
        build_condition({"kind": "nope"})
    with pytest.raises(ValueError):
        build_condition({"kind": "price_trend", "ticker": "X", "bogus": 1})
    with pytest.raises(ValueError):
        build_condition({"kind": "price_trend", "ticker": "X", "hysteresis": 0.5})


def test_shared_defaults_fill_fields_the_spec_leaves_out():
    cond = build_condition({"kind": "price_trend", "ticker": "X"}, {"trend_sma": 50})
    assert cond.sma == 50
    own = build_condition({"kind": "price_trend", "ticker": "X", "sma": 80}, {"trend_sma": 50})
    assert own.sma == 80


def test_a_new_condition_registers_itself_by_subclassing():
    class _Always(RegimeCondition):
        kind = "test_always_on"

        def triggered(self, as_of, ctx):
            return True

    assert condition_kinds()["test_always_on"] is _Always
    assert build_condition({"kind": "test_always_on"}).triggered(None, None) is True


# ---- price_trend ---------------------------------------------------------------------


def test_price_trend_is_off_in_the_uptrend_and_triggers_after_the_fall(lake):
    cond = build_condition({"kind": "price_trend", "ticker": "IDX.US", "sma": 50})
    assert cond.triggered(_at(250), _ctx(lake)) is False
    assert cond.triggered(_at(399), _ctx(lake)) is True


def test_price_trend_unknown_without_enough_bars(lake):
    cond = build_condition({"kind": "price_trend", "ticker": "IDX.US", "sma": 200})
    assert cond.triggered(_at(100), _ctx(lake)) is None
    assert (
        build_condition({"kind": "price_trend", "ticker": "NONE"}).triggered(_at(399), _ctx(lake))
        is None
    )


def test_price_trend_hysteresis_delays_the_trigger(lake):
    plain = build_condition({"kind": "price_trend", "ticker": "IDX.US", "sma": 50})
    sticky = build_condition(
        {"kind": "price_trend", "ticker": "IDX.US", "sma": 50, "hysteresis": 0.03}
    )
    first = next(i for i in range(300, 400) if plain.triggered(_at(i), _ctx(lake)))
    first_sticky = next(i for i in range(300, 400) if sticky.triggered(_at(i), _ctx(lake)))
    assert first_sticky > first


def test_price_trend_hysteresis_keeps_the_state_inside_the_band(tmp_path):
    db = make_lake(tmp_path / "l.duckdb")
    dates = pd.bdate_range("2021-01-01", periods=40)
    # flat at 100, a dip to 90 (well below the 1% band: off), then 97: just
    # above the dragged-down SMA (96.7) but inside the band
    closes = np.array([100.0] * 30 + [90.0] * 3 + [97.0] * 7)
    write_bars(db, "H.US", dates, closes)
    cond = build_condition({"kind": "price_trend", "ticker": "H.US", "sma": 10, "hysteresis": 0.01})
    plain = build_condition({"kind": "price_trend", "ticker": "H.US", "sma": 10})
    at = dates[33].to_pydatetime()
    # close 97 is above the (dragged-down) SMA, so plain is risk on, but
    # within the band the sticky condition stays triggered
    assert plain.triggered(at, ConditionContext(db)) is False
    assert cond.triggered(at, ConditionContext(db)) is True
    db.close()


def test_price_trend_never_reads_after_as_of(lake):
    cond = build_condition({"kind": "price_trend", "ticker": "IDX.US", "sma": 50})
    # bar 299 is the peak: the fall after it must not leak in
    assert cond.triggered(_at(299), _ctx(lake)) is False


# ---- realized_vol --------------------------------------------------------------------


def test_realized_vol_triggers_when_vol_is_high_versus_its_history(lake):
    cond = build_condition(
        {"kind": "realized_vol", "ticker": "VOL.US", "window": 21, "pct": 0.8, "lookback_years": 1}
    )
    assert cond.triggered(_at(280), _ctx(lake)) is False
    assert cond.triggered(_at(330), _ctx(lake)) is True


def test_realized_vol_unknown_without_history(lake):
    cond = build_condition({"kind": "realized_vol", "ticker": "VOL.US", "window": 21})
    assert cond.triggered(_at(10), _ctx(lake)) is None


# ---- higher_timeframe ----------------------------------------------------------------


def test_higher_timeframe_uses_weekly_closes(lake):
    cond = build_condition(
        {"kind": "higher_timeframe", "ticker": "IDX.US", "interval": "1w", "ema": 10}
    )
    assert cond.triggered(_at(290), _ctx(lake)) is False
    assert cond.triggered(_at(399), _ctx(lake)) is True


def test_higher_timeframe_partial_week_uses_only_bars_to_as_of(tmp_path):
    db = make_lake(tmp_path / "l.duckdb")
    dates = pd.bdate_range("2021-01-04", periods=60)  # starts on a Monday
    closes = np.full(60, 100.0)
    closes[-3:] = 50.0  # a crash on the last Wed..Fri
    write_bars(db, "W.US", dates, closes)
    cond = build_condition(
        {"kind": "higher_timeframe", "ticker": "W.US", "interval": "1w", "ema": 4}
    )
    tuesday = dates[-4].to_pydatetime()
    assert cond.triggered(tuesday, ConditionContext(db)) is False
    assert cond.triggered(dates[-1].to_pydatetime(), ConditionContext(db)) is True
    db.close()


# ---- macro ---------------------------------------------------------------------------


def test_macro_waits_for_the_publication_lag(lake):
    cond = build_condition(
        {
            "kind": "macro",
            "indicator": "unemployment",
            "transform": "change",
            "threshold": 1.0,
            "publication_lag_days": 60,
            "observation_stamp": "period_end",
        }
    )
    # 2020 value (+2.5) is public from 2021-03-01
    assert cond.triggered(datetime(2021, 2, 26), _ctx(lake)) is None  # one obs only
    assert cond.triggered(datetime(2021, 3, 1), _ctx(lake)) is True


def test_macro_level_below(lake):
    cond = build_condition(
        {
            "kind": "macro",
            "indicator": "unemployment",
            "transform": "level",
            "threshold": 4.0,
            "risk_off_when": "below",
            "publication_lag_days": 0,
            "observation_stamp": "period_end",
        }
    )
    assert cond.triggered(datetime(2020, 6, 1), _ctx(lake)) is True
    assert cond.triggered(datetime(2021, 6, 1), _ctx(lake)) is False


# ---- yield_curve ---------------------------------------------------------------------


def test_yield_curve_triggers_when_inverted(lake):
    cond = build_condition({"kind": "yield_curve", "long_ticker": "US10Y", "short_ticker": "US3M"})
    assert cond.triggered(datetime(2020, 6, 30), _ctx(lake)) is False
    assert cond.triggered(datetime(2020, 7, 1), _ctx(lake)) is True


def test_yield_curve_unknown_when_missing_or_stale(lake):
    cond = build_condition({"kind": "yield_curve", "long_ticker": "US10Y", "short_ticker": "X"})
    assert cond.triggered(datetime(2020, 7, 1), _ctx(lake)) is None
    stale = build_condition(
        {
            "kind": "yield_curve",
            "long_ticker": "US10Y",
            "short_ticker": "US3M",
            "max_staleness_days": 10,
        }
    )
    assert stale.triggered(datetime(2021, 12, 31), _ctx(lake)) is None
    assert stale.triggered(datetime(2019, 12, 31), _ctx(lake)) is None


# ---- lake helper ---------------------------------------------------------------------


def test_get_bond_yields_is_oldest_first_and_cut_at_as_of(lake):
    df = lake.get_bond_yields("US3M", as_of=datetime(2020, 1, 3))
    assert list(df["date"]) == [date(2020, 1, 1), date(2020, 1, 2), date(2020, 1, 3)]
    assert list(df["yield_to_maturity"]) == [1.0, 1.0, 1.0]
    assert lake.get_bond_yields("NONE").empty
