"""Backtester execution semantics: empty-pick decisions, next-bar-open
fills, carry-forward marking, interval-aware Sharpe, and a single bulk
price query."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.report import compute_report, periods_per_year
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.types import Order, Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold

_T0 = datetime(2026, 4, 1, 14, 30, tzinfo=UTC)
_STEP = timedelta(minutes=5)


def _bar(ticker: str, i: int, open_: float, close: float) -> dict:
    return {
        "ticker": ticker,
        "timestamp": _T0 + i * _STEP,
        "open": open_,
        "high": max(open_, close) + 0.1,
        "low": min(open_, close) - 0.1,
        "close": close,
        "adj_close": close,
        "volume": 1_000,
    }


def _lake(tmp_path, rows: list[dict]) -> DuckDBLake:
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_bars(pd.DataFrame(rows), interval=Interval.MIN_5)
    return lake


def _config(universe: Sequence[str], **kw: Any) -> BacktestConfig:
    return BacktestConfig(
        start=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        end=datetime(2026, 4, 1, 23, 59, tzinfo=UTC),
        universe=list(universe),
        interval=Interval.MIN_5,
        **kw,
    )


def _ts(i: int) -> datetime:
    return _T0 + i * _STEP


def _same_instant(a: datetime, b: datetime) -> bool:
    if a.tzinfo is None:
        a = a.replace(tzinfo=UTC)
    return a == b


class _ExitOnNoPicks:
    """Holds AAPL once bought; exits via the empty-picks branch of decide."""

    id = "exit_on_no_picks"
    applicable_asset_classes = ("equity",)

    def __init__(self) -> None:
        self.calls: list[tuple[list, datetime]] = []

    def estimate_return(self, ticker: str, as_of: date, lake: Any) -> float | None:
        return None  # never a pick

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: date,
    ) -> list[Order]:
        self.calls.append((list(my_picks), as_of))
        qty = portfolio.positions.get("AAPL.US", 0.0)
        if not my_picks and qty > 0:
            return [
                Order(
                    client_id=f"{self.id}:sell:{as_of.isoformat()}",
                    ticker="AAPL.US",
                    side="sell",
                    quantity=qty,
                )
            ]
        return []


def test_decide_is_called_with_empty_picks_on_every_rebalance(tmp_path):
    lake = _lake(tmp_path, [_bar("AAPL.US", i, 100.0, 100.0) for i in range(4)])
    strategy = _ExitOnNoPicks()
    broker = SimulatedBroker(Portfolio(cash=0.0, positions={"AAPL.US": 10.0}))
    Backtester([strategy], broker, lake, _config(["AAPL.US"])).run()

    assert len(strategy.calls) == 4
    assert all(picks == [] for picks, _ in strategy.calls)
    # the exit decided on bar 0 filled at bar 1
    assert broker.fetch_portfolio().positions == {}
    lake.close()


def test_orders_fill_at_next_bar_open(tmp_path):
    rows = [
        _bar("AAPL.US", 0, 99.0, 100.0),
        _bar("AAPL.US", 1, 110.0, 120.0),
        _bar("AAPL.US", 2, 121.0, 125.0),
    ]
    lake = _lake(tmp_path, rows)
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}))
    strategy = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})
    report = Backtester([strategy], broker, lake, _config(["AAPL.US"])).run()

    fills = broker.reconcile()
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(110.0)
    assert _same_instant(fills[0].filled_at, _ts(1))
    # sized at bar-0 close (100) but filled at bar-1 open (110): scaled down
    qty = 10_000.0 / 110.0
    assert fills[0].quantity == pytest.approx(qty)
    # bar 0: nothing filled yet; later bars marked at close
    assert report.equity_curve[0] == pytest.approx(10_000.0)
    assert report.equity_curve[1] == pytest.approx(qty * 120.0)
    assert report.equity_curve[2] == pytest.approx(qty * 125.0)
    lake.close()


def test_orders_queued_on_last_bar_are_not_filled(tmp_path):
    lake = _lake(tmp_path, [_bar("AAPL.US", 0, 100.0, 100.0)])
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}))
    strategy = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})
    report = Backtester([strategy], broker, lake, _config(["AAPL.US"])).run()
    assert broker.reconcile() == []
    assert report.equity_curve == [pytest.approx(10_000.0)]
    lake.close()


def test_held_ticker_without_bar_is_marked_at_last_close(tmp_path):
    # BTC trades every bar; AAPL has no bar at index 2 (e.g. weekend).
    rows = [_bar("BTC-USD.CC", i, 50.0, 50.0) for i in range(4)]
    rows += [_bar("AAPL.US", i, 100.0, 100.0) for i in (0, 1, 3)]
    lake = _lake(tmp_path, rows)
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}))
    strategy = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})
    report = Backtester([strategy], broker, lake, _config(["AAPL.US", "BTC-USD.CC"])).run()

    # bought 100 shares at bar-1 open; bar 2 has no AAPL bar but the
    # position is still worth 100 × last close (100).
    assert report.equity_curve[2] == pytest.approx(10_000.0)
    assert min(report.equity_curve) == pytest.approx(10_000.0)
    lake.close()


def test_pending_order_waits_for_the_tickers_next_bar(tmp_path):
    # Decided at bar 1; AAPL has no bar at 2, so it fills at bar 3's open.
    rows = [_bar("BTC-USD.CC", i, 50.0, 50.0) for i in range(4)]
    rows += [_bar("AAPL.US", i, 100.0 + i, 100.0 + i) for i in (1, 3)]
    lake = _lake(tmp_path, rows)
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}))
    strategy = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})
    Backtester([strategy], broker, lake, _config(["AAPL.US", "BTC-USD.CC"])).run()

    fills = broker.reconcile()
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(103.0)
    assert _same_instant(fills[0].filled_at, _ts(3))
    lake.close()


def test_sharpe_is_annualized_for_the_configured_interval(tmp_path):
    closes = [100.0, 101.0, 100.5, 102.0, 101.0, 103.0, 102.5]
    rows = [_bar("AAPL.US", i, c, c) for i, c in enumerate(closes)]
    lake = _lake(tmp_path, rows)
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}))
    strategy = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})
    report = Backtester([strategy], broker, lake, _config(["AAPL.US"])).run()

    expected = compute_report(
        report.strategy_id,
        report.equity_dates,
        report.equity_curve,
        periods_per_year=periods_per_year(Interval.MIN_5),
    )
    assert report.sharpe != 0.0
    assert report.sharpe == pytest.approx(expected.sharpe)
    lake.close()


def test_engine_loads_prices_with_a_single_query(tmp_path, monkeypatch):
    lake = _lake(tmp_path, [_bar("AAPL.US", i, 100.0, 100.0 + i) for i in range(10)])
    calls: list[str] = []
    real_sql = lake.sql

    def counting_sql(query, params=None):
        calls.append(query)
        return real_sql(query, params)

    monkeypatch.setattr(lake, "sql", counting_sql)
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}))
    strategy = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})
    report = Backtester([strategy], broker, lake, _config(["AAPL.US"])).run()

    assert len(calls) == 1
    assert len(report.equity_curve) == 10
    lake.close()
