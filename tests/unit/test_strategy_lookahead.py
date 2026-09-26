"""RS-03 / BL-49: a strategy never sees a bar before that bar has closed.

The engine calls a strategy with ``as_of`` = the start of the decision bar
(it decides at that bar's close). A daily bar stamped D 00:00 closes at the
end of day D, so an intraday decision on day D must not see it. We plant a
different day-D bar in a copy of the lake: every catalogued strategy must
give the same answer at D 09:30 on both lakes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.lab.catalog import strategy_catalog
from stonks.store.lake import DuckDBLake
from stonks.strategies._common import BarCache, decision_interval, visible_cutoff
from stonks.strategies.examples.momentum import Momentum
from stonks.strategies.examples.quant_momentum import QuantMomentum
from stonks.strategies.examples.tsmom import TimeSeriesMomentum
from stonks.strategies.trailing_stop import TrailingStopWrapper

TICKERS = ["A.US", "B.US", "C.US", "SPY.US"]
N_DAYS = 420
DATES = pd.bdate_range("2023-01-02", periods=N_DAYS)
D = DATES[-1].to_pydatetime()
D_PREV = DATES[-2].to_pydatetime()
INTRADAY = D.replace(hour=9, minute=30)


def _frame(plant: bool) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    frames = []
    for i, ticker in enumerate(TICKERS):
        close = (50.0 + 10 * i) * np.exp(np.cumsum(rng.normal(0.0008, 0.012, N_DAYS)))
        if plant:
            close = close.copy()
            close[-1] *= 2.0 if i % 2 == 0 else 0.5
        frames.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": [d.date() for d in DATES],
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "adj_close": close,
                    "volume": 1_000_000.0,
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def _lake(plant: bool) -> DuckDBLake:
    lake = DuckDBLake(Path(":memory:"))
    lake.migrate()
    lake.upsert_prices(_frame(plant))
    for ticker in TICKERS:
        lake.con.execute("INSERT INTO instruments (id, asset_class) VALUES (?, 'equity')", [ticker])
    return lake


@pytest.fixture(scope="module")
def lakes():
    base, planted = _lake(False), _lake(True)
    yield base, planted
    base.close()
    planted.close()


def _answers(strategy_factory, lake, as_of):
    strategy = strategy_factory()
    out = {}
    for ticker in TICKERS:
        try:
            out[ticker] = strategy.estimate_return(ticker, as_of, lake)
        except Exception as exc:  # a strategy may refuse a daily-only lake
            out[ticker] = f"error: {type(exc).__name__}"
    return out


# ---- the rule --------------------------------------------------------------------


def test_visible_cutoff_rule():
    day = datetime(2024, 3, 5)
    # daily decision (midnight stamp) sees its own day's daily bar
    assert visible_cutoff(day, Interval.DAY_1) == day
    # intraday decision without a known decision interval: only closed days
    assert visible_cutoff(day.replace(hour=9, minute=30), Interval.DAY_1) < day
    # intraday reads keep the bar the decision stands on
    at = day.replace(hour=10)
    assert visible_cutoff(at, Interval.HOUR_1) == at
    # known decision interval: a 24/7 1h run sees day D at its 23:00 bar only
    with decision_interval(Interval.HOUR_1):
        assert visible_cutoff(day.replace(hour=23), Interval.DAY_1) == day
        assert visible_cutoff(day, Interval.DAY_1) < day
        # a 4h bar stamped 08:00 closes at 12:00: not visible at the 10:00 bar
        assert visible_cutoff(at, Interval.HOUR_4) < day.replace(hour=8)
    with decision_interval(Interval.DAY_1):
        # a daily run sees every hourly bar of its day
        assert visible_cutoff(day, Interval.HOUR_1) == day.replace(hour=23)


def test_bar_cache_hides_the_open_daily_bar_intraday(lakes):
    base, _ = lakes
    cache = BarCache(base)
    ts, _ = cache.last_close("A.US", Interval.DAY_1, INTRADAY)
    assert ts.date() == D_PREV.date()
    ts, _ = cache.last_close("A.US", Interval.DAY_1, D)
    assert ts.date() == D.date()


# ---- named strategies ----------------------------------------------------------------

NAMED = {
    "momentum": lambda: Momentum({"lookback_days": 5, "skip_days": 0, "threshold": -1.0}),
    "tsmom": lambda: TimeSeriesMomentum({}),
    "quant_momentum": lambda: QuantMomentum({"universe": "A.US,B.US,C.US"}),
    "trailing_stop": lambda: TrailingStopWrapper(
        {
            "inner_class_path": "stonks.strategies.examples.momentum:Momentum",
            "inner_params": {"lookback_days": 5, "skip_days": 0, "threshold": -1.0},
        }
    ),
}


@pytest.mark.parametrize("name", sorted(NAMED))
def test_intraday_answer_equals_previous_close_answer(name, lakes):
    base, planted = lakes
    factory = NAMED[name]
    at_prev_close = _answers(factory, base, D_PREV)
    assert _answers(factory, base, INTRADAY) == at_prev_close
    assert _answers(factory, planted, INTRADAY) == at_prev_close


@pytest.mark.parametrize("name", ["momentum", "trailing_stop"])
def test_daily_decision_sees_its_own_day(name, lakes):
    """The rule must not lag daily runs: at the daily decision on D the
    day's bar is complete and counts. (quant_momentum skips the last month
    and tsmom's capped forecast keeps its sign, so the planted bar does not
    move them.)"""
    base, planted = lakes
    factory = NAMED[name]
    assert _answers(factory, base, D) != _answers(factory, planted, D)


# ---- the whole catalog ------------------------------------------------------------------


def _catalog_factory(cls):
    names = {s.name for s in cls.parameter_spec()}
    params = {}
    if "ticker" in names:
        params["ticker"] = "A.US"
    if "interval" in names:
        params["interval"] = "1d"
    if "universe" in names:
        params["universe"] = "A.US,B.US,C.US"
    return lambda: cls(params)


@pytest.mark.parametrize("name", sorted(strategy_catalog()))
def test_catalog_strategy_ignores_the_open_daily_bar(name, lakes):
    base, planted = lakes
    factory = _catalog_factory(strategy_catalog()[name])
    assert _answers(factory, base, INTRADAY) == _answers(factory, planted, INTRADAY)


@pytest.mark.parametrize("name", sorted(strategy_catalog()))
def test_catalog_strategy_ignores_bars_after_a_daily_decision(name, lakes):
    base, planted = lakes
    factory = _catalog_factory(strategy_catalog()[name])
    assert _answers(factory, base, D_PREV) == _answers(factory, planted, D_PREV)


def test_decision_interval_context_is_reset():
    with decision_interval(Interval.HOUR_1):
        pass
    day = datetime(2024, 3, 5)
    assert visible_cutoff(day, Interval.DAY_1) == day
    assert visible_cutoff(day + timedelta(hours=9), Interval.DAY_1) < day
