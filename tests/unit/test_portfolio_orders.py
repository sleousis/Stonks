"""Buffered order diff: target weights to orders with a no-trade band."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from stonks.core.types import Portfolio
from stonks.execution.orders import make_client_id
from stonks.portfolio.orders import orders_from_targets

AS_OF = date(2026, 1, 2)
PRICES = {"X": 10.0, "Y": 20.0, "Z": 5.0}


def _orders(targets, portfolio, prices=PRICES, **kw):
    return orders_from_targets(targets, portfolio, prices, as_of=AS_OF, **kw)


def test_buffer_hit_produces_no_order() -> None:
    book = Portfolio(cash=500.0, positions={"X": 50.0})  # X at 0.50 of 1000
    assert _orders({"X": 0.52}, book) == []
    assert _orders({"X": 0.46}, book) == []


def test_outside_the_band_trades_to_the_nearest_edge() -> None:
    buy = _orders({"X": 0.5}, Portfolio(cash=1000.0))
    assert [(o.ticker, o.side) for o in buy] == [("X", "buy")]
    assert buy[0].quantity == pytest.approx(45.0)  # to 0.45 = 0.5 - 0.1*0.5

    sell = _orders({"X": 0.5}, Portfolio(cash=200.0, positions={"X": 80.0}))
    assert [(o.ticker, o.side) for o in sell] == [("X", "sell")]
    assert sell[0].quantity == pytest.approx(25.0)  # 0.8 -> 0.55


def test_full_exit_always_trades_the_whole_position() -> None:
    book = Portfolio(cash=999.0, positions={"X": 0.1})  # a sliver, far below min trade
    orders = _orders({}, book)
    assert [(o.ticker, o.side, o.quantity) for o in orders] == [("X", "sell", 0.1)]
    assert _orders({"X": 0.0}, book)[0].quantity == 0.1


def test_trades_below_min_trade_weight_are_skipped() -> None:
    assert _orders({"X": 0.004}, Portfolio(cash=1000.0)) == []
    assert _orders({"X": 0.004}, Portfolio(cash=1000.0), min_trade_weight=0.0)


def test_zero_buffer_trades_exactly_to_target() -> None:
    orders = _orders({"X": 0.5}, Portfolio(cash=1000.0), buffer_fraction=0.0)
    assert orders[0].quantity == pytest.approx(50.0)


def test_client_ids_are_deterministic() -> None:
    orders = _orders({"X": 0.5}, Portfolio(cash=1000.0), strategy_id="book")
    assert orders[0].client_id == make_client_id(
        as_of=AS_OF, strategy_id="book", ticker="X", side="buy"
    )
    assert orders[0].strategy_id == "book"
    again = _orders({"X": 0.5}, Portfolio(cash=1000.0), strategy_id="book")
    assert again == orders


def test_sells_come_first_then_buys_in_ticker_order() -> None:
    book = Portfolio(cash=0.0, positions={"X": 100.0})
    orders = _orders({"Z": 0.5, "Y": 0.5}, book)
    assert [(o.ticker, o.side) for o in orders] == [("X", "sell"), ("Y", "buy"), ("Z", "buy")]


def test_buys_never_exceed_cash_plus_sell_proceeds() -> None:
    orders = _orders({"X": 0.9, "Y": 0.9}, Portfolio(cash=1000.0), buffer_fraction=0.0)
    notional = sum(o.quantity * PRICES[o.ticker] for o in orders)
    assert notional == pytest.approx(1000.0)
    assert orders[0].quantity * 10 == pytest.approx(orders[1].quantity * 20)


def test_unpriced_tickers_are_skipped() -> None:
    book = Portfolio(cash=1000.0, positions={"Q": 5.0})
    assert _orders({"W": 0.5}, book) == []


def test_negative_targets_are_rejected() -> None:
    with pytest.raises(ValueError, match="short"):
        _orders({"X": -0.1}, Portfolio(cash=1000.0))


def test_argument_bounds() -> None:
    with pytest.raises(ValueError):
        _orders({}, Portfolio(cash=1.0), buffer_fraction=0.6)
    with pytest.raises(ValueError):
        _orders({}, Portfolio(cash=1.0), min_trade_weight=-0.1)


def test_empty_or_negative_equity_trades_nothing() -> None:
    assert _orders({"X": 0.5}, Portfolio(cash=0.0)) == []
    assert _orders({"X": 0.5}, Portfolio(cash=-10.0)) == []


def test_random_books_never_go_short_or_overspend() -> None:
    rng = np.random.default_rng(5)
    tickers = [f"T{i}" for i in range(12)]
    for _ in range(300):
        prices = {t: float(rng.uniform(1, 300)) for t in tickers}
        positions = {t: float(rng.uniform(0, 50)) for t in tickers if rng.random() < 0.4}
        book = Portfolio(cash=float(rng.uniform(0, 5000)), positions=positions)
        raw = {t: float(rng.uniform(0, 1)) for t in tickers if rng.random() < 0.5}
        total = sum(raw.values()) or 1.0
        scale = rng.uniform(0.5, 1.5)  # sometimes asks for more than 100%
        targets = {t: scale * w / total for t, w in raw.items()}
        orders = orders_from_targets(
            targets, book, prices, as_of=AS_OF, buffer_fraction=float(rng.uniform(0, 0.5))
        )
        sold = sum(o.quantity * prices[o.ticker] for o in orders if o.side == "sell")
        bought = sum(o.quantity * prices[o.ticker] for o in orders if o.side == "buy")
        assert bought <= book.cash + sold + 1e-6
        for o in orders:
            assert o.quantity > 0
            if o.side == "sell":
                assert o.quantity <= positions.get(o.ticker, 0.0) + 1e-9
        assert len({o.client_id for o in orders}) == len(orders)
