"""No-picks exit path of every strategy's ``decide``.

The backtest engine calls ``decide(picks=[] ...)`` on every rebalance bar, so
an empty pick list must flatten exactly what is held — full quantity, side
``sell`` — with a client id unique per bar (the broker dedupes on it).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from stonks.core.types import Portfolio
from stonks.strategies.examples.donchian_breakout import DonchianBreakout
from stonks.strategies.examples.momentum import Momentum
from stonks.strategies.examples.rsi_pca import RSIPCAStrategy
from stonks.strategies.examples.trendline_breakout import TrendlineBreakoutStrategy

SINGLE_TICKER = [DonchianBreakout, TrendlineBreakoutStrategy, RSIPCAStrategy]
ALL = [*SINGLE_TICKER, Momentum]


def _make(cls):
    if cls is Momentum:
        return Momentum({"lookback_days": 20})
    return cls({"ticker": "X.US"})


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.id)
def test_no_picks_sells_full_held_quantity(cls):
    s = _make(cls)
    portfolio = Portfolio(cash=0.0, positions={"X.US": 12.345})
    orders = s.decide([], portfolio, {"X.US": 100.0}, date(2026, 3, 16))
    assert len(orders) == 1
    (order,) = orders
    assert order.side == "sell"
    assert order.ticker == "X.US"
    assert order.quantity == 12.345
    assert order.order_type == "market"
    assert order.strategy_id == s.id


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.id)
def test_no_picks_sells_even_without_a_price(cls):
    s = _make(cls)
    portfolio = Portfolio(cash=0.0, positions={"X.US": 3.0})
    orders = s.decide([], portfolio, {}, date(2026, 3, 16))
    assert [(o.side, o.ticker, o.quantity) for o in orders] == [("sell", "X.US", 3.0)]


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.id)
def test_no_picks_and_flat_places_nothing(cls):
    s = _make(cls)
    orders = s.decide([], Portfolio(cash=1_000.0, positions={}), {"X.US": 100.0}, date(2026, 3, 16))
    assert orders == []


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.id)
def test_exit_client_ids_are_unique_per_bar_including_intraday(cls):
    s = _make(cls)
    portfolio = Portfolio(cash=0.0, positions={"X.US": 1.0})
    bars = [
        date(2026, 3, 16),
        date(2026, 3, 17),
        datetime(2026, 3, 17, 14, 0),
        datetime(2026, 3, 17, 15, 0),
    ]
    ids = [s.decide([], portfolio, {"X.US": 1.0}, b)[0].client_id for b in bars]
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.id)
def test_exit_and_entry_client_ids_differ_on_the_same_bar(cls):
    s = _make(cls)
    as_of = datetime(2026, 3, 17, 15, 0)
    sell = s.decide([], Portfolio(cash=0.0, positions={"X.US": 1.0}), {"X.US": 1.0}, as_of)
    buy = s.decide([(0.1, "X.US")], Portfolio(cash=100.0, positions={}), {"X.US": 1.0}, as_of)
    assert sell[0].side == "sell" and buy[0].side == "buy"
    assert sell[0].client_id != buy[0].client_id


@pytest.mark.parametrize("cls", SINGLE_TICKER, ids=lambda c: c.id)
def test_single_ticker_exit_leaves_other_tickers_alone(cls):
    s = _make(cls)
    portfolio = Portfolio(cash=0.0, positions={"X.US": 2.0, "OTHER.US": 7.0})
    orders = s.decide([], portfolio, {}, date(2026, 3, 16))
    assert [(o.ticker, o.quantity) for o in orders] == [("X.US", 2.0)]


def test_momentum_no_picks_exits_every_held_position():
    s = Momentum({"lookback_days": 20})
    portfolio = Portfolio(cash=0.0, positions={"A.US": 2.0, "B.US": 0.5, "C.US": 0.0})
    orders = s.decide([], portfolio, {}, date(2026, 3, 16))
    assert sorted((o.ticker, o.side, o.quantity) for o in orders) == [
        ("A.US", "sell", 2.0),
        ("B.US", "sell", 0.5),
    ]
    assert len({o.client_id for o in orders}) == 2
