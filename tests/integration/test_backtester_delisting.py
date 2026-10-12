"""Delisted holdings leave the book at their last close (P14).

A name that stops trading must not sit frozen in the portfolio at its
last mark forever. The engine sells it for cash at ``last_close x (1 +
[backtest.delisting] return)`` on the first bar after its last bar, once
the name is past ``instruments.delisted_date`` or past the end of its
membership with no later bar. Queued orders for it are dropped, and the
report lists the event so the trade ledger closes the lot.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.backtest.trades import with_trades
from stonks.config import BacktestSettings
from stonks.core.types import Order, Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold

_DATES = pd.bdate_range("2026-01-02", periods=20)


def _frame(ticker: str, days: Sequence[pd.Timestamp], closes: Sequence[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ticker,
            "date": [d.date() for d in days],
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "adj_close": closes,
            "volume": [1e6] * len(days),
        }
    )


@pytest.fixture
def lake(tmp_path):
    """A.US trades all 20 days. B.US has 8 bars, its last close is 10, and
    its membership of universe ``u`` ends on day 9 (index 8)."""
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_prices(_frame("A.US", _DATES, [100.0] * 20))
    lake.upsert_prices(_frame("B.US", _DATES[:8], [50.0] * 7 + [10.0]))
    lake.upsert_universe_membership(
        pd.DataFrame(
            [
                {
                    "universe_id": "u",
                    "ticker": "A.US",
                    "start_date": _DATES[0].date(),
                    "end_date": None,
                },
                {
                    "universe_id": "u",
                    "ticker": "B.US",
                    "start_date": _DATES[0].date(),
                    "end_date": _DATES[8].date(),
                },
            ]
        )
    )
    yield lake
    lake.close()


class _BuyThenSell:
    """Buys 200 B.US on the first bar, then queues a sell on its last bar
    (that sell can never fill: B.US has no later bar)."""

    id = "buy_then_sell"
    applicable_asset_classes = ("equity",)

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
        if day == _DATES[0].date():
            return [Order(client_id="buy", ticker="B.US", side="buy", quantity=200.0)]
        if day == _DATES[7].date():
            return [Order(client_id="sell", ticker="B.US", side="sell", quantity=200.0)]
        return []


def _broker() -> SimulatedBroker:
    return SimulatedBroker(
        portfolio=Portfolio(cash=10_000.0, positions={}), slippage_bps=0.0, fee_per_trade=0.0
    )


def _config(**kw: Any) -> BacktestConfig:
    return BacktestConfig(
        start=_DATES[0].date(), end=_DATES[-1].date(), universe=["A.US", "B.US"], **kw
    )


def test_p14_membership_end_without_later_bar_delists_at_last_close(lake):
    broker = _broker()
    bt = Backtester([_BuyThenSell()], broker, lake, _config(universe_id="u"))
    report = bt.run()

    portfolio = broker.fetch_portfolio()
    assert portfolio.positions == {}
    # bought 200 at 50, delisted at the last close of 10
    assert portfolio.cash == pytest.approx(10_000.0 - 200 * 50.0 + 200 * 10.0)
    assert report.equity_curve[-1] == pytest.approx(portfolio.cash)

    (event,) = report.delistings
    assert event.ticker == "B.US"
    assert event.timestamp.date() == _DATES[8].date()
    assert event.quantity == pytest.approx(200.0)
    assert event.last_close == pytest.approx(10.0)
    assert event.price == pytest.approx(10.0)
    assert event.cash_delta == pytest.approx(2_000.0)
    # the queued sell of day 8 is dropped, never left waiting
    assert any(cid.endswith("sell") for cid in event.dropped_orders)
    assert [f.side for f in broker.fills] == ["buy"]

    traded = with_trades(report, broker.fills)
    (trip,) = traded.trades
    assert not trip.is_open
    assert trip.exit_px == pytest.approx(10.0)
    assert trip.pnl == pytest.approx(200 * (10.0 - 50.0))
    assert trip.exit_ref == "delisting"


def test_p14_delisting_return_haircut(lake):
    broker = _broker()
    report = Backtester(
        [_BuyThenSell()], broker, lake, _config(universe_id="u", delisting_return=-0.3)
    ).run()
    (event,) = report.delistings
    assert event.price == pytest.approx(7.0)
    assert broker.fetch_portfolio().cash == pytest.approx(10_000.0 - 200 * 50.0 + 200 * 7.0)


def test_p14_delisted_date_delists_without_a_universe(lake):
    lake.con.execute(
        "INSERT INTO instruments (id, asset_class, delisted_date) VALUES (?, 'equity', ?)",
        ["B.US", _DATES[8].date()],
    )
    broker = _broker()
    strategy = BuyAndHold({"ticker": "B.US", "allocation": 1.0})
    report = Backtester([strategy], broker, lake, _config()).run()
    assert broker.fetch_portfolio().positions == {}
    assert broker.fetch_portfolio().cash == pytest.approx(2_000.0)
    (event,) = report.delistings
    assert event.timestamp.date() == _DATES[8].date()


def test_p14_name_without_delisting_evidence_stays_held(lake):
    broker = _broker()
    report = Backtester([_BuyThenSell()], broker, lake, _config()).run()
    assert report.delistings == ()
    assert broker.fetch_portfolio().positions == {"B.US": pytest.approx(200.0)}


def test_delisting_return_setting_defaults_to_zero():
    assert BacktestSettings().delisting.return_ == 0.0
    assert BacktestSettings.model_validate({"delisting": {"return": -0.3}}).delisting.return_ == (
        pytest.approx(-0.3)
    )
