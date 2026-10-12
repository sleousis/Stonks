"""Rsi2Reversion: Connors' short-term dip buying in an uptrend."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.lab.catalog import strategy_catalog
from stonks.store.lake import DuckDBLake
from stonks.store.pit import PitSession
from stonks.strategies._common import decision_interval
from stonks.strategies.examples.rsi2_reversion import Rsi2Reversion

DATES = pd.bdate_range("2024-01-01", periods=300)
#: after 250 bars of a steady rise: two 2% drops, two small gains, a rally
MOVES = [-0.02, -0.02, 0.005, 0.004, 0.03, 0.01]


def _closes(drift: float, n_extra: int = 0) -> list[float]:
    closes = list(100 * (1 + drift) ** np.arange(250))
    for move in MOVES:
        closes.append(closes[-1] * (1 + move))
    for _ in range(n_extra):
        closes.append(closes[-1] * 0.97)  # a future that would read as a new dip
    return closes


def _lake(series: dict[str, list[float]]) -> DuckDBLake:
    lake = DuckDBLake(Path(":memory:"))
    lake.migrate()
    frames = []
    for ticker, closes in series.items():
        n = len(closes)
        frames.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": [d.date() for d in DATES[:n]],
                    "open": closes,
                    "high": closes,
                    "low": closes,
                    "close": closes,
                    "adj_close": closes,
                    "volume": 1_000_000.0,
                }
            )
        )
        lake.con.execute("INSERT INTO instruments (id, asset_class) VALUES (?, 'equity')", [ticker])
    lake.upsert_prices(pd.concat(frames, ignore_index=True))
    return lake


@pytest.fixture(scope="module")
def lake():
    db = _lake({"SPY.US": _closes(0.002), "DOWN.US": _closes(-0.002)})
    yield db
    db.close()


def _day(i: int) -> date:
    return DATES[i].date()


def test_catalogued_with_a_plain_card():
    cls = strategy_catalog()["rsi2_reversion"]
    assert cls is Rsi2Reversion
    assert cls.title and cls.summary
    assert cls.applicable_asset_classes == ("equity",)
    assert cls.alpha_family == "reversion"
    assert cls.premise == "mean_reversion"
    tunable = {s.name for s in cls.parameter_spec() if s.tunable}
    assert {"entry_rsi", "exit_rsi", "exit_ma_bars"} <= tunable


def test_buys_a_deep_dip_in_an_uptrend_and_holds_until_the_close_tops_its_short_average(lake):
    s = Rsi2Reversion({})
    assert s.estimate_return("SPY.US", _day(249), lake) is None  # no dip yet
    entry = s.estimate_return("SPY.US", _day(250), lake)
    assert entry is not None and entry > 0  # RSI(2) about 9 after the first drop
    for i in (251, 252, 253):  # still below the 5-day average, RSI under 70
        assert s.estimate_return("SPY.US", _day(i), lake) == pytest.approx(entry)
    assert s.estimate_return("SPY.US", _day(254), lake) is None  # close tops the 5-day average


def test_exits_when_rsi_tops_the_exit_level(lake):
    s = Rsi2Reversion({"exit_rsi": 40.0})
    assert s.estimate_return("SPY.US", _day(252), lake) is not None  # RSI about 26
    assert s.estimate_return("SPY.US", _day(253), lake) is None  # RSI about 47


def test_no_entry_below_the_long_average(lake):
    s = Rsi2Reversion({})
    for i in range(248, 256):
        assert s.estimate_return("DOWN.US", _day(i), lake) is None


def test_a_higher_entry_level_buys_earlier_dips(lake):
    assert Rsi2Reversion({"entry_rsi": 5.0}).estimate_return("SPY.US", _day(250), lake) is None
    assert Rsi2Reversion({"entry_rsi": 5.0}).estimate_return("SPY.US", _day(251), lake) is not None


def test_no_look_ahead_future_bars_change_nothing(lake):
    """A lake that also holds the days after the decision (a new crash that
    would read as fresh dips) gives the same answers, raw and point in time."""
    planted = _lake({"SPY.US": _closes(0.002, n_extra=30), "DOWN.US": _closes(-0.002)})
    try:
        for i in (249, 250, 252, 254, 255):
            day = _day(i)
            for source in (lake, planted):
                view = PitSession(source).at(DATES[i].to_pydatetime(), decision_interval=None)
                with decision_interval(Interval.DAY_1):
                    raw = Rsi2Reversion({}).estimate_return("SPY.US", day, source)
                    pit = Rsi2Reversion({}).estimate_return("SPY.US", day, view)
                assert raw == pit
            assert Rsi2Reversion({}).estimate_return("SPY.US", day, planted) == Rsi2Reversion(
                {}
            ).estimate_return("SPY.US", day, lake)
    finally:
        planted.close()


def test_features_show_the_indicators(lake):
    f = Rsi2Reversion({}).extract_features("SPY.US", _day(250), lake).values
    assert f["signal"] == 1.0
    assert f["rsi"] < 10.0
    assert f["close"] > f["trend_ma"]
    assert all(math.isfinite(v) for v in f.values())


def test_flat_prices_and_short_history_give_no_pick():
    flat = _lake({"FLAT.US": [50.0] * 260, "NEW.US": [10.0, 9.0, 8.0]})
    try:
        s = Rsi2Reversion({})
        assert s.estimate_return("FLAT.US", _day(259), flat) is None
        assert s.estimate_return("NEW.US", _day(2), flat) is None
        values = s.extract_features("FLAT.US", _day(259), flat).values
        assert all(math.isfinite(v) for v in values.values())
    finally:
        flat.close()


def test_decide_buys_new_picks_equally_and_sells_the_rest():
    s = Rsi2Reversion({})
    book = Portfolio(cash=1000.0, positions={"OLD.US": 5.0, "SPY.US": 2.0})
    orders = s.decide(
        [(0.9, "SPY.US"), (0.95, "QQQ.US"), (0.92, "IWM.US")],
        book,
        {"SPY.US": 500.0, "QQQ.US": 10.0, "IWM.US": 20.0},
        date(2024, 12, 2),
    )
    by = {(o.side, o.ticker): o.quantity for o in orders}
    assert by == {("sell", "OLD.US"): 5.0, ("buy", "QQQ.US"): 50.0, ("buy", "IWM.US"): 25.0}
    assert all(o.strategy_id == "rsi2_reversion" for o in orders)
