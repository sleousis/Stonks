"""The intraday reference strategies on synthetic minute bars (21.3.1):
opening range breakout, VWAP reversion and intraday momentum.

Every session is a real NYSE session (2024-03-05 opens at 14:30 UTC and
closes at 21:00 UTC). A decision on the bar stamped ``t`` is made at that
bar's close and sees bars stamped up to ``t`` only.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.strategies._common import decision_interval
from stonks.strategies.base import strategy_metadata
from stonks.strategies.examples.intraday_momentum import IntradayMomentum
from stonks.strategies.examples.intraday_orb import OpeningRangeBreakout
from stonks.strategies.examples.intraday_vwap_reversion import VwapReversion
from tests.minute_bars import bars_frame, minute_bar, minute_lake, session_frame, session_minutes

T = "A.US"
DAY = date(2024, 3, 5)  # Tuesday
PREV = date(2024, 3, 4)
NEXT = date(2024, 3, 6)
ALL = (OpeningRangeBreakout, VwapReversion, IntradayMomentum)


def at(minute: int, day: date = DAY):
    return minute_bar(T, day, minute)


def score(strategy, minute: int, lake, day: date = DAY, *, interval=Interval.MIN_1):
    with decision_interval(interval):
        return strategy.estimate_return(T, at(minute, day), lake)


# ---- metadata -----------------------------------------------------------------


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.__name__)
def test_hypothesis_card_and_capabilities(cls):
    meta = strategy_metadata(cls)
    assert len(meta.hypothesis) > 80
    assert meta.alpha_family in ("trend", "reversion")
    assert meta.label_horizon_bars > 0
    assert meta.required_history_bars > 0
    assert "equity" in meta.applicable_asset_classes
    spec = {s.name: s for s in cls.parameter_spec()}
    assert spec["interval"].default == "1m"
    assert all(Interval.parse(code).is_intraday for code in spec["interval"].bounds)
    assert not spec["interval"].tunable and not spec["ticker"].tunable
    assert any(s.tunable for s in spec.values())


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.__name__)
def test_daily_interval_is_refused(cls):
    with pytest.raises(ValueError):
        cls({"interval": "1d"})


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.__name__)
def test_label_horizon_counts_bars_of_the_chosen_interval(cls):
    one = cls({"interval": "1m"})
    five = cls({"interval": "5m"})
    assert five.label_horizon_bars == math.ceil(one.label_horizon_bars / 5)
    assert five.required_history_bars <= one.required_history_bars


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.__name__)
def test_other_tickers_and_no_lake_score_nothing(cls):
    strategy = cls({"ticker": T})
    lake = minute_lake([session_frame(T, DAY, lambda i: 100.0)])
    assert strategy.estimate_return("B.US", at(100), lake) is None
    assert strategy.estimate_return(T, at(100), None) is None


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.__name__)
def test_outside_the_session_scores_nothing(cls):
    strategy = cls({"ticker": T})
    lake = minute_lake([session_frame(T, DAY, lambda i: 100.0 + 0.01 * i)])
    before_open = at(0) - timedelta(minutes=30)
    assert strategy.estimate_return(T, before_open, lake) is None
    assert strategy.estimate_return(T, date(2024, 3, 9), lake) is None  # Saturday


# ---- opening range breakout -----------------------------------------------------


def _orb_path(breakout_at: int = 40, stop_at: int | None = None, back_at: int | None = None):
    def path(i: int) -> float:
        if i < 30:
            return 100.0 + (0.5 if i % 2 else -0.5)  # the range: about 99.5 .. 100.5
        if i < breakout_at:
            return 100.0
        if stop_at is not None and i >= stop_at:
            if back_at is not None and i >= back_at:
                return 102.0  # back above the range: no second trade
            return 99.0  # below the range low: the stop
        return 101.5

    return path


def _orb(**params):
    return OpeningRangeBreakout({"ticker": T, "range_minutes": 30, **params})


def test_orb_waits_for_the_range_then_buys_a_close_above_it():
    lake = minute_lake([session_frame(T, DAY, _orb_path())])
    s = _orb()
    assert score(s, 10, lake) is None  # the range is still forming
    assert score(s, 35, lake) is None  # range done, no breakout yet
    r = score(s, 45, lake)
    assert r is not None and r > 0
    assert score(s, 200, lake) is not None  # the trade stays on


def test_orb_stops_below_the_range_and_trades_once_a_session():
    lake = minute_lake([session_frame(T, DAY, _orb_path(stop_at=100, back_at=150))])
    s = _orb()
    assert score(s, 90, lake) is not None
    assert score(s, 110, lake) is None  # stopped out
    assert score(s, 200, lake) is None  # a second breakout is not traded


def test_orb_is_flat_before_the_close_and_at_the_next_open():
    frames = [session_frame(T, DAY, _orb_path()), session_frame(T, NEXT, lambda i: 101.5)]
    lake = minute_lake(frames)
    s = _orb(exit_minutes_before_close=5)
    last = len(session_minutes(T, DAY)) - 1  # the bar that closes at 21:00
    assert score(s, last - 10, lake) is not None
    assert score(s, last - 3, lake) is None  # 4 minutes or less to the close
    assert score(s, 0, lake, NEXT) is None  # a new session starts flat


def test_orb_ignores_pre_market_bars():
    pre = bars_frame(
        T, [at(0) - timedelta(minutes=m) for m in range(60, 0, -1)], [120.0] * 60
    )  # a pre-market spike above the whole session
    lake = minute_lake([pre, session_frame(T, DAY, _orb_path())])
    assert score(_orb(), 45, lake) is not None


def test_orb_on_five_minute_bars():
    frame = session_frame(T, DAY, _orb_path())
    frame = frame[[ts.minute % 5 == 0 for ts in frame["timestamp"]]].reset_index(drop=True)
    lake = minute_lake([frame])
    from stonks.core.interval import Interval as I

    lake.con.execute("UPDATE bars SET interval = '5m'")
    s = _orb(interval="5m")
    assert score(s, 20, lake, interval=I.MIN_5) is None
    assert score(s, 50, lake, interval=I.MIN_5) is not None


# ---- VWAP reversion -------------------------------------------------------------------


def _vwap_path(drop_at: int = 25, drop_to: float = 99.5, recover_at: int | None = 40):
    def path(i: int) -> float:
        if i < drop_at:
            return 100.0
        if recover_at is not None and i >= recover_at:
            return 100.3
        return drop_to

    return path


def _vwap(**params):
    return VwapReversion(
        {"ticker": T, "entry_bps": 30.0, "stop_multiple": 3.0, "warmup_minutes": 15, **params}
    )


def test_vwap_buys_a_stretch_below_vwap_and_sells_at_vwap():
    lake = minute_lake([session_frame(T, DAY, _vwap_path())])
    s = _vwap()
    assert score(s, 20, lake) is None
    r = score(s, 27, lake)
    assert r is not None and r > 0
    assert score(s, 45, lake) is None  # back above VWAP: the trade is done


def test_vwap_waits_for_the_warmup():
    lake = minute_lake([session_frame(T, DAY, _vwap_path(drop_at=3, recover_at=None))])
    s = _vwap()
    assert score(s, 5, lake) is None  # inside the warm-up
    # after the warm-up VWAP has moved toward the new level: no stretch left
    assert score(s, 60, lake) is None


def test_vwap_stops_on_a_deeper_fall_and_does_not_reenter():
    def path(i: int) -> float:
        if i < 25:
            return 100.0
        if i < 30:
            return 99.6  # -40 bps: enter
        if i < 60:
            return 98.6  # far past three times the entry band: stop
        return 99.0

    lake = minute_lake([session_frame(T, DAY, path)])
    s = _vwap()
    assert score(s, 28, lake) is not None
    assert score(s, 35, lake) is None
    assert score(s, 90, lake) is None


def test_vwap_without_volume_uses_the_running_mean():
    lake = minute_lake([session_frame(T, DAY, _vwap_path(), volume=0.0)])
    assert score(_vwap(), 27, lake) is not None


# ---- intraday momentum ----------------------------------------------------------------


def _momentum_lake(first_half_hour: float):
    prev = session_frame(T, PREV, lambda i: 100.0)
    today = session_frame(T, DAY, lambda i: first_half_hour if i >= 29 else 100.0)
    return minute_lake([prev, today])


def _momentum(**params):
    return IntradayMomentum(
        {
            "ticker": T,
            "signal_minutes": 30,
            "entry_minutes_before_close": 30,
            "exit_minutes_before_close": 5,
            **params,
        }
    )


def test_momentum_buys_the_last_half_hour_after_a_strong_first_half_hour():
    lake = _momentum_lake(101.0)
    s = _momentum()
    last = len(session_minutes(T, DAY)) - 1
    assert score(s, 200, lake) is None  # not in the entry window yet
    r = score(s, last - 20, lake)
    assert r == pytest.approx(0.01)
    assert score(s, last - 3, lake) is None  # out before the close


def test_momentum_stays_flat_after_a_weak_first_half_hour():
    lake = _momentum_lake(99.0)
    last = len(session_minutes(T, DAY)) - 1
    assert score(_momentum(), last - 20, lake) is None


def test_momentum_needs_the_previous_close():
    lake = minute_lake([session_frame(T, DAY, lambda i: 101.0 if i >= 29 else 100.0)])
    last = len(session_minutes(T, DAY)) - 1
    assert score(_momentum(), last - 20, lake) is None


def test_momentum_threshold():
    lake = _momentum_lake(100.2)  # +20 bps
    last = len(session_minutes(T, DAY)) - 1
    assert score(_momentum(threshold_bps=10.0), last - 20, lake) is not None
    assert score(_momentum(threshold_bps=50.0), last - 20, lake) is None


def test_momentum_on_an_early_close_uses_that_close():
    half_day = date(2024, 11, 29)  # closes at 18:00 UTC
    prev = session_frame(T, date(2024, 11, 27), lambda i: 100.0)
    today = session_frame(T, half_day, lambda i: 101.0 if i >= 29 else 100.0)
    lake = minute_lake([prev, today])
    last = len(session_minutes(T, half_day)) - 1
    assert last == 209
    assert score(_momentum(), last - 20, lake, half_day) is not None


# ---- closed bars only (P12) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("factory", "minute", "path"),
    [
        (_orb, 38, _orb_path(breakout_at=39)),
        (_vwap, 24, _vwap_path(drop_at=25)),
    ],
    ids=["orb", "vwap"],
)
def test_the_next_bar_is_never_read(factory, minute, path):
    """The bar after the decision bar would flip the answer; it must not."""
    full = minute_lake([session_frame(T, DAY, path)])
    frame = session_frame(T, DAY, path)
    cut = minute_lake([frame[frame["timestamp"] <= at(minute)]])
    assert score(factory(), minute, full) is None
    assert score(factory(), minute, cut) is None
    assert score(factory(), minute + 1, full) is not None


def test_a_five_minute_bar_is_hidden_until_it_closes():
    """In a 1m run a 5m bar stamped 14:40 closes at 14:45: a decision on
    the 14:42 bar must not see it."""
    frame = session_frame(T, DAY, _orb_path(breakout_at=40))
    frame = frame[[ts.minute % 5 == 0 for ts in frame["timestamp"]]].reset_index(drop=True)
    lake = minute_lake([frame])
    lake.con.execute("UPDATE bars SET interval = '5m'")
    s = _orb(interval="5m")
    assert score(s, 42, lake) is None  # the 14:40 bar is still open
    assert score(s, 44, lake) is not None  # it closed at the end of this bar


# ---- decide ----------------------------------------------------------------------------


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.__name__)
def test_decide_buys_when_picked_and_sells_when_not(cls):
    s = cls({"ticker": T, "allocation": 0.5})
    when = at(100)
    buy = s.decide([(0.01, T)], Portfolio(cash=10_000.0, positions={}), {T: 100.0}, when)
    assert len(buy) == 1 and buy[0].side == "buy"
    assert buy[0].quantity == pytest.approx(50.0)
    sell = s.decide([], Portfolio(cash=0.0, positions={T: 50.0}), {T: 100.0}, when)
    assert len(sell) == 1 and sell[0].side == "sell" and sell[0].quantity == 50.0


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.__name__)
def test_features_are_finite(cls):
    lake = _momentum_lake(101.0)
    values = cls({"ticker": T}).extract_features(T, at(300), lake).values
    assert values
    assert all(math.isfinite(v) for v in values.values())
