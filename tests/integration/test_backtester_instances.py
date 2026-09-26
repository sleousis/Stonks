"""Two instances of one strategy class (same ``id``, different params) in a
backtest must not collide on ``client_id`` or on the per-strategy picks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.types import Order, Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold

_T0 = datetime(2026, 4, 1, tzinfo=UTC)


def _lake(tmp_path, tickers: list[str], n: int = 3) -> DuckDBLake:
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    rows = [
        {
            "ticker": t,
            "timestamp": _T0 + timedelta(days=i),
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "adj_close": 100.0,
            "volume": 1_000,
        }
        for t in tickers
        for i in range(n)
    ]
    lake.upsert_bars(pd.DataFrame(rows), interval=Interval.DAY_1)
    return lake


def _config(universe: list[str]) -> BacktestConfig:
    return BacktestConfig(start=_T0, end=_T0 + timedelta(days=5), universe=universe)


def test_two_instances_on_the_same_ticker_both_fill(tmp_path):
    lake = _lake(tmp_path, ["AAPL.US"])
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}))
    a = BuyAndHold({"ticker": "AAPL.US", "allocation": 0.3})
    b = BuyAndHold({"ticker": "AAPL.US", "allocation": 0.2})
    Backtester([a, b], broker, lake, _config(["AAPL.US"])).run()

    fills = broker.reconcile()
    assert len(fills) == 2
    assert len({f.order_client_id for f in fills}) == 2
    assert sorted(f.quantity for f in fills) == pytest.approx([20.0, 30.0])
    lake.close()


class _PicksOne:
    """Picks only its configured ticker and records what ``decide`` saw."""

    id = "picks_one"
    applicable_asset_classes = ("equity",)

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self.seen: list[list[str]] = []

    def estimate_return(self, ticker: str, as_of: date, lake: Any) -> float | None:
        return 1.0 if ticker == self.ticker else None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: date,
    ) -> list[Order]:
        self.seen.append([t for _, t in my_picks])
        return []


def test_each_instance_sees_only_its_own_picks(tmp_path):
    lake = _lake(tmp_path, ["AAPL.US", "MSFT.US"], n=1)
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}))
    a, b = _PicksOne("AAPL.US"), _PicksOne("MSFT.US")
    Backtester([a, b], broker, lake, _config(["AAPL.US", "MSFT.US"])).run()

    assert a.seen == [["AAPL.US"]]
    assert b.seen == [["MSFT.US"]]
    lake.close()


def test_single_strategy_client_ids_are_still_unique_and_stable(tmp_path):
    lake = _lake(tmp_path, ["AAPL.US"])
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}))
    Backtester(
        [BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})],
        broker,
        lake,
        _config(["AAPL.US"]),
    ).run()
    fills = broker.reconcile()
    assert len(fills) == 1
    assert fills[0].order_client_id.startswith("0:buy_and_hold:AAPL.US:")
    lake.close()
