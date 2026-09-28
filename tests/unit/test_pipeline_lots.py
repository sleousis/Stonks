"""The lot rule inside the shared order pipeline (roadmap 23.1, P21)."""

from __future__ import annotations

from datetime import date

from stonks.config import RiskPolicy
from stonks.core.types import Order, Portfolio
from stonks.portfolio.lots import LotSettings
from stonks.portfolio.pipeline import (
    BookInput,
    ConstructionSettings,
    MarketView,
    apply_book_risk,
    build_orders,
)

AS_OF = date(2026, 3, 20)
PRICES = {"X": 30.0, "Y": 70.0}
CLASSES = {"X": "equity", "Y": "equity"}


class Buyer:
    id = "a"

    def decide(self, my_picks, portfolio, prices, as_of):
        return [
            Order(client_id=f"raw:{t}", ticker=t, side="buy", quantity=2.6) for _, t in my_picks
        ]


def _market() -> MarketView:
    return MarketView(as_of=AS_OF, prices=PRICES, asset_classes=CLASSES)


def _whole():
    return LotSettings(profile="whole_shares").rule()


def test_single_winner_orders_are_sized_to_whole_shares():
    book = BookInput(portfolio=Portfolio(cash=1_000.0), lots=_whole())
    result = build_orders({"a": {"X": 0.3}}, book, _market(), strategies=lambda s: Buyer())
    assert [o.quantity for o in result.orders] == [2.0]
    assert result.lots is not None
    assert [c.ticker for c in result.lots.changes] == ["X"]


def test_target_weight_orders_are_sized_and_small_ones_skipped():
    book = BookInput(
        portfolio=Portfolio(cash=100.0),
        construction=ConstructionSettings(method="equal_weight_top_n", params={"n": 2}),
        lots=_whole(),
    )
    result = build_orders({"a": {"X": 0.3, "Y": 0.2}}, book, _market())
    # about 45 each after the buffer: X buys 1 share, Y is below one share
    assert {o.ticker: o.quantity for o in result.orders} == {"X": 1.0}
    assert result.lots is not None
    assert [c.ticker for c in result.lots.skipped] == ["Y"]


def test_no_rule_changes_nothing():
    book = BookInput(portfolio=Portfolio(cash=1_000.0))
    result = build_orders({"a": {"X": 0.3}}, book, _market(), strategies=lambda s: Buyer())
    assert [o.quantity for o in result.orders] == [2.6]
    assert result.lots is None


def test_the_risk_recheck_sizes_to_lots_too():
    book = BookInput(portfolio=Portfolio(cash=1_000.0), risk=RiskPolicy(), lots=_whole())
    orders = [Order(client_id="b", ticker="X", side="buy", quantity=3.4)]
    assert [o.quantity for o in apply_book_risk(orders, book, _market()).orders] == [3.0]
