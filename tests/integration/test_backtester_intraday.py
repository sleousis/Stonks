"""Intraday backtester smoke test.

Proves the engine is interval-agnostic: running a BuyAndHold strategy over
5-minute bars yields one equity point per bar, and the portfolio tracks
the intraday price movement bar-by-bar. Exactly the same code paths
(Strategy Protocol, SimulatedBroker, BacktestReport) as the daily test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold


@pytest.fixture
def lake_intraday(tmp_path):
    """Lake with 12 five-minute bars on a single trading session,
    climbing linearly from 100 → 106."""
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()

    start = datetime(2026, 4, 1, 14, 30, tzinfo=UTC)  # 14:30 UTC
    bars = []
    for i in range(12):
        ts = start + timedelta(minutes=5 * i)
        close = 100.0 + 0.5 * i
        bars.append(
            {
                "ticker": "AAPL.US",
                "timestamp": ts,
                "open": close - 0.1,
                "high": close + 0.1,
                "low": close - 0.2,
                "close": close,
                "adj_close": close,
                "volume": 1_000_000,
            }
        )
    lake.upsert_bars(pd.DataFrame(bars), interval=Interval.MIN_5)
    yield lake
    lake.close()


def test_backtester_steps_through_5m_bars(lake_intraday):
    broker = SimulatedBroker(portfolio=Portfolio(cash=10_000.0, positions={}))
    strategy = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})

    config = BacktestConfig(
        start=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        end=datetime(2026, 4, 1, 23, 59, tzinfo=UTC),
        universe=["AAPL.US"],
        interval=Interval.MIN_5,
        threshold=0.0,
    )
    report = Backtester([strategy], broker, lake_intraday, config).run()

    # one equity point per bar
    assert len(report.equity_curve) == 12
    assert len(report.equity_dates) == 12
    # the buy decided on the first bar only fills at the second bar's open,
    # so the first point is still all cash.
    assert report.equity_curve[0] == pytest.approx(10_000.0)
    # filled at bar-1 open (100.4), marked at the last close (105.5)
    assert report.final_return == pytest.approx(105.5 / 100.4 - 1.0)


def test_intraday_engine_rebalance_every_bars(lake_intraday):
    """`rebalance_every_bars=3` should trigger the winner's decide() on the
    1st, 4th, 7th, … bars regardless of wall-clock duration."""
    calls: list[datetime] = []

    class _Tap(BuyAndHold):
        def decide(self, my_picks, portfolio, prices, as_of):
            calls.append(as_of)
            return super().decide(my_picks, portfolio, prices, as_of)

    broker = SimulatedBroker(portfolio=Portfolio(cash=10_000.0, positions={}))
    strategy = _Tap({"ticker": "AAPL.US", "allocation": 1.0})

    config = BacktestConfig(
        start=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        end=datetime(2026, 4, 1, 23, 59, tzinfo=UTC),
        universe=["AAPL.US"],
        interval=Interval.MIN_5,
        rebalance_every_bars=3,
    )
    Backtester([strategy], broker, lake_intraday, config).run()

    # 12 bars; rebalance cadence 3 → decisions at bar indices 0, 3, 6, 9 = 4 calls
    assert len(calls) == 4


def test_backtest_interval_filter_ignores_other_intervals(lake_intraday, tmp_path):
    """If the lake has both daily and 5-minute bars for the same ticker, a
    backtest configured for 5m must not include the daily bars."""
    # add a daily bar that shouldn't show up
    lake_intraday.upsert_bars(
        pd.DataFrame(
            [
                {
                    "ticker": "AAPL.US",
                    "timestamp": datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
                    "open": 50.0,
                    "high": 50.0,
                    "low": 50.0,
                    "close": 50.0,
                    "adj_close": 50.0,
                    "volume": 10,
                }
            ]
        ),
        interval=Interval.DAY_1,
    )

    broker = SimulatedBroker(portfolio=Portfolio(cash=10_000.0, positions={}))
    strategy = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})
    config = BacktestConfig(
        start=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        end=datetime(2026, 4, 1, 23, 59, tzinfo=UTC),
        universe=["AAPL.US"],
        interval=Interval.MIN_5,
    )
    report = Backtester([strategy], broker, lake_intraday, config).run()
    assert len(report.equity_curve) == 12  # only the 5m bars


# ---- RS-03: the engine declares its bar length to the strategies -----------------


class _SeesInterval:
    """Records the decision interval visible to strategy code."""

    id = "sees_interval"
    applicable_asset_classes = ("equity", "crypto")

    def __init__(self) -> None:
        self.seen: list = []
        self.cutoffs: list = []

    def estimate_return(self, ticker, as_of, lake):
        from stonks.strategies._common import _DECISION_INTERVAL, visible_cutoff

        self.seen.append(_DECISION_INTERVAL.get())
        self.cutoffs.append((as_of, visible_cutoff(as_of, Interval.DAY_1)))
        return None

    def decide(self, my_picks, portfolio, prices, as_of):
        return []


@pytest.mark.parametrize("construction", [None, "equal_weight_top_n"])
def test_strategies_run_inside_the_engine_decision_interval(tmp_path, construction):
    import pandas as pd

    from stonks.store.lake import DuckDBLake

    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    stamps = pd.date_range("2026-01-03 22:00", periods=4, freq="h")  # crosses midnight
    lake.upsert_bars(
        pd.DataFrame(
            {
                "ticker": "BTC-USD.CC",
                "timestamp": stamps,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "adj_close": 100.0,
                "volume": 1.0,
            }
        ),
        interval=Interval.HOUR_1,
    )
    strategy = _SeesInterval()
    Backtester(
        [strategy],
        SimulatedBroker(Portfolio(cash=1_000.0)),
        lake,
        BacktestConfig(
            start=stamps[0].to_pydatetime(),
            end=stamps[-1].to_pydatetime(),
            universe=["BTC-USD.CC"],
            interval=Interval.HOUR_1,
            construction=construction,
        ),
    ).run()
    assert strategy.seen and all(s == Interval.HOUR_1 for s in strategy.seen)
    # the midnight bar of a 24/7 hourly run never sees that day's daily bar
    midnight = [(a, c) for a, c in strategy.cutoffs if a.hour == 0]
    assert midnight and all(c < a for a, c in midnight)
    lake.close()
