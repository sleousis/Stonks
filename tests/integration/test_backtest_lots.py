"""Whole shares in the backtest (roadmap 23.1): the lot rule sizes both
decision routes, the report carries the lot figures, and a run without the
setting is unchanged."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.portfolio.lots import LotSettings
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold

_D0 = date(2026, 1, 5)


def _day(i: int) -> datetime:
    return datetime.combine(_D0 + timedelta(days=i), datetime.min.time(), tzinfo=UTC)


@pytest.fixture
def lake(tmp_path) -> DuckDBLake:
    rows = [
        {
            "ticker": "X.US",
            "timestamp": _day(i),
            "open": 33.0,
            "high": 34.0,
            "low": 32.0,
            "close": 33.0,
            "adj_close": 33.0,
            "volume": 1e6,
        }
        for i in range(5)
    ]
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_bars(pd.DataFrame(rows), interval=Interval.DAY_1)
    return lake


def _run(lake, cash: float = 1_000.0, **kw):
    broker = SimulatedBroker(Portfolio(cash=cash, positions={}))
    config = BacktestConfig(start=_day(0), end=_day(4), universe=["X.US"], **kw)
    strategy = BuyAndHold({"ticker": "X.US", "allocation": 1.0})
    report = Backtester([strategy], broker, lake, config).run()
    return report, broker


def test_default_run_keeps_fractional_shares(lake):
    report, broker = _run(lake)
    assert broker.fetch_portfolio().positions["X.US"] == pytest.approx(1_000.0 / 33.0)
    assert report.lots is not None
    assert report.lots.profile == "fractional"
    assert report.lots.skipped == 0
    # measured against whole shares all the same: one share of 30.3 needs 33
    assert report.lots.min_capital == pytest.approx(33.0)


def test_whole_shares_floor_the_per_strategy_route(lake):
    report, broker = _run(lake, lots=LotSettings(profile="whole_shares"))
    assert broker.fetch_portfolio().positions["X.US"] == 30.0
    assert report.lots is not None
    assert report.lots.rounded == 1
    assert report.lots.mean_drift > 0


def test_whole_shares_floor_the_pipeline_route(lake):
    _, broker = _run(
        lake, lots=LotSettings(profile="whole_shares"), construction="equal_weight_top_n"
    )
    held = broker.fetch_portfolio().positions["X.US"]
    assert held == int(held) and held > 0


def test_a_small_book_skips_what_it_cannot_buy(lake):
    report, broker = _run(lake, cash=20.0, lots=LotSettings(profile="whole_shares"))
    assert broker.fetch_portfolio().positions.get("X.US", 0.0) == 0.0
    assert report.lots is not None
    assert report.lots.skipped >= 1
    assert report.lots.skipped_share > 0
