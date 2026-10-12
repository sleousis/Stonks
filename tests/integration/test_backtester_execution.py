"""Backtester execution semantics: empty-pick decisions, next-bar-open
fills, carry-forward marking, interval-aware Sharpe, and a single bulk
price query."""

from __future__ import annotations

import math
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


# ---- mixed calendars: a weekend rebalance must not drop a queued order ------------


class _OneShot:
    """Buys 10 E.US on its first decision, then never decides anything."""

    id = "oneshot"
    applicable_asset_classes = ("equity", "crypto")

    def __init__(self) -> None:
        self.done = False

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        return None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        if self.done:
            return []
        self.done = True
        return [Order(client_id=f"x:{as_of}", ticker="E.US", side="buy", quantity=10.0)]


def _daily_lake(tmp_path) -> tuple[DuckDBLake, pd.DatetimeIndex]:
    def frame(ticker: str, days) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "ticker": ticker,
                "date": [d.date() for d in days],
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "adj_close": 100.0,
                "volume": 1e6,
            }
        )

    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    days = pd.date_range("2026-01-09", periods=10)  # starts on a Friday
    lake.upsert_prices(frame("BTC-USD.CC", days))
    lake.upsert_prices(frame("E.US", [d for d in days if d.weekday() < 5]))
    return lake, days


@pytest.mark.parametrize("universe", [["E.US"], ["BTC-USD.CC", "E.US"]])
def test_queued_order_survives_a_rebalance_on_a_bar_without_its_ticker(tmp_path, universe):
    """Friday's buy of E.US must fill on Monday, crypto weekend bars or not:
    Saturday's rebalance decides nothing for E.US, so it keeps the order."""
    lake, days = _daily_lake(tmp_path)
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}), slippage_bps=0.0)
    config = BacktestConfig(start=days[0].date(), end=days[-1].date(), universe=universe)
    Backtester([_OneShot()], broker, lake, config).run()
    assert broker.fetch_portfolio().positions == {"E.US": pytest.approx(10.0)}
    (fill,) = broker.fills
    assert fill.filled_at.date() == date(2026, 1, 12)  # Monday
    lake.close()


class _BuyOnFridaySellOnSunday:
    """Buys E.US on Friday, then on Sunday decides a sell of E.US: the new
    decision for the same ticker replaces the queued buy."""

    id = "replace"
    applicable_asset_classes = ("equity", "crypto")

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        return None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        day = as_of.date()
        if day == date(2026, 1, 9):
            return [Order(client_id="buy", ticker="E.US", side="buy", quantity=10.0)]
        if day == date(2026, 1, 11):
            return [Order(client_id="sell", ticker="E.US", side="sell", quantity=4.0)]
        return []


def test_new_decision_for_the_same_ticker_replaces_the_queued_order(tmp_path):
    lake, days = _daily_lake(tmp_path)
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={"E.US": 5.0}), slippage_bps=0.0)
    config = BacktestConfig(
        start=days[0].date(), end=days[-1].date(), universe=["BTC-USD.CC", "E.US"]
    )
    Backtester([_BuyOnFridaySellOnSunday()], broker, lake, config).run()
    assert [f.side for f in broker.fills] == ["sell"]
    assert broker.fetch_portfolio().positions == {"E.US": pytest.approx(1.0)}
    lake.close()


# ---- a NaN close must not poison the equity curve ---------------------------------


def test_nan_close_is_skipped_and_the_last_mark_carries(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    dates = pd.bdate_range("2026-01-02", periods=10)
    closes = [100.0 + i for i in range(10)]
    closes[5] = float("nan")
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "A.US",
                "date": [d.date() for d in dates],
                "open": [100.0 + i for i in range(10)],
                "high": 120.0,
                "low": 90.0,
                "close": closes,
                "adj_close": closes,
                "volume": 1e6,
            }
        )
    )
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}), slippage_bps=0.0)
    config = BacktestConfig(start=dates[0].date(), end=dates[-1].date(), universe=["A.US"])
    report = Backtester([BuyAndHold({"ticker": "A.US"})], broker, lake, config).run()
    assert all(math.isfinite(v) for v in report.equity_curve)
    assert math.isfinite(report.sharpe)
    assert report.sharpe != 0.0
    assert math.isfinite(report.max_drawdown)
    assert len(report.equity_curve) == 9  # the bad bar is skipped
    lake.close()
