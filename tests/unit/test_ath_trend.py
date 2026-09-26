"""AllTimeHighTrend (Wilcox & Crittenden): weekly all-time-high entries and
a 10-ATR trailing stop, replayed statelessly."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.protocols import Strategy
from stonks.core.types import Portfolio
from stonks.features.trailing_stop import Trade
from stonks.lab.catalog import strategy_catalog
from stonks.strategies.base import strategy_metadata
from stonks.strategies.examples.ath_trend import AllTimeHighTrend
from tests.unit.trend_helpers import DATES, LAST, build_lake

N = len(DATES)  # DATES[0] is a Monday and bdate_range has no holidays: bar i is weekday i % 5
CRASH = 600


def _ath_series() -> np.ndarray:
    closes = np.empty(N)
    closes[:400] = 100.0 * np.exp(-0.001 * np.arange(400))  # the all-time high is bar 0
    # week of bar 400: Wednesday makes a new high, but the week closes below it
    closes[400:405] = [90.0, 95.0, 101.0, 99.0, 99.5]
    # next week: Friday (bar 409) closes at a new high -> the entry
    closes[405:410] = [100.0, 100.5, 100.8, 101.5, 102.0]
    closes[410:CRASH] = 102.0 * np.exp(0.003 * np.arange(1, CRASH - 409))
    closes[CRASH:] = closes[CRASH - 1] * 0.6 * np.exp(-0.001 * np.arange(N - CRASH))
    return closes


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    db = build_lake(tmp_path_factory.mktemp("ath") / "lake.duckdb", {"ATH.US": _ath_series()})
    yield db
    db.close()


def _at(i: int):
    return DATES[i].to_pydatetime()


# ---- surface -------------------------------------------------------------------------


def test_catalogued_with_metadata():
    assert strategy_catalog()["ath_trend"] is AllTimeHighTrend
    meta = strategy_metadata(AllTimeHighTrend)
    assert meta.alpha_family == "trend"
    assert meta.required_history_bars == 252
    assert meta.label_horizon_bars == 63
    assert meta.applicable_asset_classes == ("equity", "crypto")
    assert meta.hypothesis
    s = AllTimeHighTrend({})
    assert isinstance(s, Strategy)
    assert (s.params["atr_bars"], s.params["k_atr"]) == (50, 10.0)


# ---- entries and exits -----------------------------------------------------------------


def test_enters_only_on_a_weekly_close_at_an_all_time_high_and_exits_on_the_stop(lake):
    trades = AllTimeHighTrend({}).trade_log("ATH.US", LAST, lake)
    assert trades == [Trade(409, CRASH, "stop")]


def test_position_follows_the_replay_bar_by_bar(lake):
    s = AllTimeHighTrend({})
    assert s.estimate_return("ATH.US", _at(402), lake) is None  # mid-week high
    assert s.estimate_return("ATH.US", _at(404), lake) is None  # week closed below it
    assert s.estimate_return("ATH.US", _at(408), lake) is None
    assert s.estimate_return("ATH.US", _at(409), lake) == 10.0
    assert s.estimate_return("ATH.US", _at(CRASH - 1), lake) == 10.0
    assert s.estimate_return("ATH.US", _at(CRASH), lake) is None
    assert s.estimate_return("ATH.US", LAST, lake) is None  # no new high since


def test_no_entry_before_min_history(lake):
    trades = AllTimeHighTrend({"min_history_bars": 500}).trade_log("ATH.US", LAST, lake)
    # bar 499 is a Friday in the steady climb, the first seasoned weekly high
    assert trades == [Trade(499, CRASH, "stop")]
    assert AllTimeHighTrend({"min_history_bars": 800}).forecast("ATH.US", LAST, lake) is None


def test_a_tighter_stop_exits_on_a_smaller_pullback(tmp_path):
    closes = _ath_series()
    closes[500:CRASH] = closes[499] * np.exp(0.003 * np.arange(1, 101))
    closes[520] *= 0.85  # a one-day 15% dip, then back on trend
    db = build_lake(tmp_path / "dip.duckdb", {"ATH.US": closes})
    try:
        wide = AllTimeHighTrend({}).trade_log("ATH.US", _at(CRASH - 1), db)
        tight = AllTimeHighTrend({"k_atr": 3.0}).trade_log("ATH.US", _at(CRASH - 1), db)
        assert wide == [Trade(409, None, None)]
        assert tight[0] == Trade(409, 520, "stop")
        # re-entered on the next weekly all-time-high close after the dip
        assert tight[1].entry > 520 and tight[1].entry % 5 == 4
    finally:
        db.close()


def test_never_reads_bars_after_as_of(tmp_path):
    closes = _ath_series()
    changed = closes.copy()
    changed[CRASH:] = closes[CRASH - 1] * 1.5  # no crash: a new high instead
    a = build_lake(tmp_path / "a.duckdb", {"ATH.US": closes})
    b = build_lake(tmp_path / "b.duckdb", {"ATH.US": changed})
    try:
        s = AllTimeHighTrend({})
        for i in (404, 409, CRASH - 1):
            assert s.trade_log("ATH.US", _at(i), a) == s.trade_log("ATH.US", _at(i), b)
            assert s.forecast("ATH.US", _at(i), a) == s.forecast("ATH.US", _at(i), b)
    finally:
        a.close()
        b.close()


# ---- backtest ------------------------------------------------------------------------


def test_small_backtest_buys_the_breakout_and_sells_on_the_stop(lake):
    broker = SimulatedBroker(Portfolio(cash=100_000.0), slippage_bps=0.0, fee_per_trade=0.0)
    config = BacktestConfig(start=DATES[395].date(), end=LAST.date(), universe=["ATH.US"])
    Backtester([AllTimeHighTrend({})], broker, lake, config).run()
    book = broker.fetch_portfolio()
    assert book.positions.get("ATH.US", 0.0) == 0.0  # stopped out after the crash
    assert book.cash > 0.0
    days = [pd.Timestamp(f.filled_at).date() for f in broker.fills]
    # decided at the close of bars 409 and CRASH, filled at the next open
    assert days[0] == DATES[410].date()
    assert days[-1] == DATES[CRASH + 1].date()
    assert all(DATES[410].date() <= d <= DATES[CRASH + 1].date() for d in days)
