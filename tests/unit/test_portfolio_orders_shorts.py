"""Phase 16.1: ``orders_from_targets`` with ``allow_short`` (signed targets)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.types import Portfolio
from stonks.portfolio.orders import orders_from_targets

AS_OF = date(2026, 1, 2)
PRICES = {"X": 10.0, "Y": 20.0}


def _orders(targets, portfolio, prices=PRICES, **kw):
    return orders_from_targets(targets, portfolio, prices, as_of=AS_OF, allow_short=True, **kw)


def _legs(orders):
    return [(o.ticker, o.side, o.position_effect, round(o.quantity, 9)) for o in orders]


def test_negative_targets_still_raise_without_the_switch() -> None:
    with pytest.raises(ValueError, match="short"):
        orders_from_targets({"X": -0.1}, Portfolio(cash=1.0), PRICES, as_of=AS_OF)


def test_short_from_flat_goes_to_the_band_edge() -> None:
    # Equity 1,000, target -0.5, band 0.05: to -0.45 = 45 shares at 10.
    [order] = _orders({"X": -0.5}, Portfolio(cash=1_000.0))
    assert _legs([order]) == [("X", "sell", "open", 45.0)]
    assert order.client_id == "2026-01-02:portfolio:X:short"


def test_long_to_short_flip_closes_then_opens() -> None:
    # Long 50 at 10 of 1,000 (0.5). Target -0.2, band 0.02: to -0.18.
    # Trade -0.68 x 1,000 / 10 = -68: sell-close 50, sell-open 18.
    orders = _orders({"X": -0.2}, Portfolio(cash=500.0, positions={"X": 50.0}))
    assert _legs(orders) == [("X", "sell", "close", 50.0), ("X", "sell", "open", 18.0)]
    assert [o.client_id.rsplit(":", 1)[1] for o in orders] == ["sell", "short"]


def test_short_within_the_band_does_not_trade() -> None:
    # Short 20 at 10 with cash 1,200: equity 1,000, weight -0.2.
    book = Portfolio(cash=1_200.0, positions={"X": -20.0})
    assert _orders({"X": -0.21}, book) == []
    assert _orders({"X": -0.19}, book) == []


def test_short_grows_and_shrinks_to_the_edge() -> None:
    book = Portfolio(cash=1_200.0, positions={"X": -20.0})
    # Target -0.4, band 0.04: to -0.36, sell-open 16 more.
    assert _legs(_orders({"X": -0.4}, book)) == [("X", "sell", "open", 16.0)]
    # Target -0.1, band 0.01: to -0.11, cover 9.
    [cover] = _orders({"X": -0.1}, book)
    assert _legs([cover]) == [("X", "buy", "close", 9.0)]
    assert cover.client_id.endswith(":cover")


def test_short_to_long_flip_covers_then_buys_even_inside_min_trade() -> None:
    # Short -0.2, target +0.1, band 0.01: to 0.09, buy 29: cover 20, buy 9.
    book = Portfolio(cash=1_200.0, positions={"X": -20.0})
    orders = _orders({"X": 0.1}, book, min_trade_weight=0.5)
    assert _legs(orders) == [("X", "buy", "close", 20.0), ("X", "buy", "open", 9.0)]


def test_zero_target_covers_the_whole_short() -> None:
    [cover] = _orders({}, Portfolio(cash=1_200.0, positions={"X": -20.0}))
    assert _legs([cover]) == [("X", "buy", "close", 20.0)]


def test_opening_buys_are_scaled_but_covers_never_are() -> None:
    # Cash 1,100, short 10 X at 10: equity 1,000. Target Y 2.0 (band 0.2):
    # buy to 1.8 = 90 shares, 1,800. Cover X: 100. Budget 1,100 - 100 =
    # 1,000, so Y scales by 1,000 / 1,800 to 50 shares; the cover stays 10.
    orders = _orders({"Y": 2.0}, Portfolio(cash=1_100.0, positions={"X": -10.0}))
    assert _legs(orders) == [("X", "buy", "close", 10.0), ("Y", "buy", "open", 50.0)]


def test_non_finite_targets_and_empty_books() -> None:
    with pytest.raises(ValueError, match="finite"):
        _orders({"X": float("nan")}, Portfolio(cash=1.0))
    assert _orders({"X": -0.5}, Portfolio(cash=0.0)) == []


def test_unpriced_and_tiny_trades_are_skipped() -> None:
    book = Portfolio(cash=1_200.0, positions={"X": -20.0})
    assert _orders({"X": -0.204, "Q": -0.1}, book, buffer_fraction=0.0) == []


def test_a_long_only_target_set_matches_the_long_only_route() -> None:
    book = Portfolio(cash=300.0, positions={"X": 50.0, "Y": 10.0})
    targets = {"X": 0.2, "Y": 0.6}
    long_only = orders_from_targets(targets, book, PRICES, as_of=AS_OF)
    both = _orders(targets, book)
    assert [(o.client_id, o.side, o.quantity) for o in both] == [
        (o.client_id, o.side, o.quantity) for o in long_only
    ]


def test_be45_an_unpriced_short_holding_stops_the_sizing() -> None:
    # Q is short with no price: the liability is unknown, so equity is too.
    book = Portfolio(cash=2_000.0, positions={"Q": -50.0})
    assert _orders({"X": 0.3}, book, buffer_fraction=0.0) == []
