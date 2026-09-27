"""Catalog-wide look-ahead and edge-case checks on minute bars (21.3.1).

Every catalogued strategy that can read 1m bars answers at a decision in a
1m run. Two lakes share the same past. The planted one also holds the
bars after the decision, with prices that would flip any signal. The
answers must match on the raw lake and behind a point-in-time view, also
when the strategy is asked about a later minute by mistake (P12, RS-03).
Flat prices and NaN closes on minute bars never crash or leak NaN.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.lab.catalog import strategy_catalog
from stonks.store.pit import PitSession
from stonks.strategies._common import decision_interval
from stonks.strategies.examples._intraday import IntradayStrategy
from tests.minute_bars import minute_bar, minute_lake, session_frame

TICKERS = ["A.US", "B.US"]
DAYS = [date(2024, 3, 4), date(2024, 3, 5)]
LATER_DAY = date(2024, 3, 6)
DAY = DAYS[-1]
#: Decision minutes after the open: the range is done, mid session, the last half hour.
MINUTES = (45, 200, 370)


def _minute_spec(cls):
    spec = {s.name: s for s in cls.parameter_spec()}
    interval = spec.get("interval")
    return interval is not None and interval.bounds is not None and "1m" in interval.bounds


MINUTE = sorted(name for name, cls in strategy_catalog().items() if _minute_spec(cls))
INTRADAY = sorted(
    name for name, cls in strategy_catalog().items() if issubclass(cls, IntradayStrategy)
)


def _today(i: int) -> float:
    """Every intraday rule is live on this path: a range, a breakout, a
    dip below VWAP from minute 180 to 205, a strong first half hour."""
    if i < 30:
        return 100.0 + (0.5 if i % 2 else -0.5)
    if i < 180:
        return 100.0 + 2.0 * (i - 30) / 150
    if i <= 205:
        return 100.4
    return 101.5


def _frames(ticker: str, scale: float) -> list[pd.DataFrame]:
    prev = [session_frame(ticker, d, lambda i: 100.0 * scale) for d in DAYS[:-1]]
    return [*prev, session_frame(ticker, DAY, lambda i: _today(i) * scale)]


def _lake(cut_minute: int, plant: bool):
    cut = minute_bar("A.US", DAY, cut_minute)
    frames = []
    for k, ticker in enumerate(TICKERS):
        whole = pd.concat(_frames(ticker, 1.0 + 0.1 * k), ignore_index=True)
        past = whole[whole["timestamp"] <= cut]
        frames.append(past)
        if plant:
            future = pd.concat(
                [whole[whole["timestamp"] > cut], session_frame(ticker, LATER_DAY, lambda i: 1.0)],
                ignore_index=True,
            )
            factor = 0.2 if k % 2 == 0 else 3.0
            for col in ("open", "high", "low", "close", "adj_close"):
                future[col] = future[col] * factor
            frames.append(future)
    return minute_lake(frames)


@pytest.fixture(scope="module")
def lakes():
    out = {m: (_lake(m, False), _lake(m, True)) for m in MINUTES}
    yield out
    for past, planted in out.values():
        past.close()
        planted.close()


def _factory(cls):
    names = {s.name for s in cls.parameter_spec()}
    params = {"interval": "1m"}
    if "ticker" in names:
        params["ticker"] = "A.US"
    if "universe" in names:
        params["universe"] = ",".join(TICKERS)
    return lambda: cls(params)


def _answers(factory, lake, minute: int, *, view: bool, asked_minutes_later: int = 0):
    at = minute_bar("A.US", DAY, minute)
    asked = at + timedelta(minutes=asked_minutes_later)
    source = PitSession(lake).at(at, decision_interval=Interval.MIN_1) if view else lake
    strategy = factory()
    out = {}
    with decision_interval(Interval.MIN_1):
        for ticker in TICKERS:
            try:
                out[ticker] = strategy.estimate_return(ticker, asked, source)
            except Exception as exc:  # a strategy may refuse a minute lake
                out[ticker] = f"error: {type(exc).__name__}"
    return out


def test_the_catalog_has_minute_strategies():
    assert {"intraday_orb", "intraday_vwap_reversion", "intraday_momentum"} <= set(INTRADAY)
    assert set(INTRADAY) <= set(MINUTE)


@pytest.mark.parametrize("name", INTRADAY)
def test_intraday_strategies_are_live_on_the_fixture(name, lakes):
    """Guards the checks below against passing on silence."""
    factory = _factory(strategy_catalog()[name])
    live = [_answers(factory, lakes[m][0], m, view=False)["A.US"] is not None for m in MINUTES]
    assert any(live), name


@pytest.mark.parametrize("minute", MINUTES)
@pytest.mark.parametrize("name", MINUTE)
def test_minute_strategy_ignores_bars_after_the_decision(name, minute, lakes):
    past, planted = lakes[minute]
    factory = _factory(strategy_catalog()[name])
    expected = _answers(factory, past, minute, view=False)
    assert _answers(factory, planted, minute, view=False) == expected
    assert _answers(factory, planted, minute, view=True) == expected


@pytest.mark.parametrize("name", MINUTE)
def test_asking_about_a_later_minute_behind_a_view_reads_no_future(name, lakes):
    past, planted = lakes[200]
    factory = _factory(strategy_catalog()[name])
    late = {"view": True, "asked_minutes_later": 90}
    assert _answers(factory, planted, 200, **late) == _answers(factory, past, 200, **late)


# ---- edge cases on minute bars -----------------------------------------------------------


@pytest.fixture(scope="module")
def flat_lake():
    frames = [session_frame(t, d, lambda i: 50.0) for t in TICKERS for d in DAYS]
    lake = minute_lake(frames)
    yield lake
    lake.close()


@pytest.fixture(scope="module")
def nan_lake():
    rng = np.random.default_rng(4)
    frames = []
    for t in TICKERS:
        for d in DAYS:
            path = 50.0 * np.exp(np.cumsum(rng.normal(0.0, 0.001, 400)))
            frame = session_frame(t, d, lambda i, p=path: float(p[i]))
            frame.loc[100:110, "close"] = np.nan  # a hole of NaN closes
            frames.append(frame)
    lake = minute_lake(frames)
    yield lake
    lake.close()


def _finite(values: dict) -> bool:
    return all(isinstance(v, int | float) and math.isfinite(v) for v in values.values())


@pytest.mark.parametrize("name", INTRADAY)
def test_minute_features_are_finite_on_flat_prices(name, flat_lake):
    strategy = _factory(strategy_catalog()[name])()
    at = minute_bar("A.US", DAY, 200)
    with decision_interval(Interval.MIN_1):
        for ticker in TICKERS:
            values = strategy.extract_features(ticker, at, flat_lake).values
            assert _finite(values), {k: v for k, v in values.items() if not _finite({k: v})}
            r = strategy.estimate_return(ticker, at, flat_lake)
            assert r is None or math.isfinite(r)


@pytest.mark.parametrize("name", MINUTE)
def test_minute_nan_closes_never_crash_or_leak_nan(name, nan_lake):
    strategy = _factory(strategy_catalog()[name])()
    with decision_interval(Interval.MIN_1):
        for minute in (105, 200):
            at = minute_bar("A.US", DAY, minute)
            for ticker in TICKERS:
                r = strategy.estimate_return(ticker, at, nan_lake)
                assert r is None or math.isfinite(r)
