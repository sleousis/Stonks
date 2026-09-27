"""Hard to borrow short sales wait for a person (roadmap 19.16).

A short opening order for a name the borrow source marks hard to borrow
(its status is ``hard``, or its fee is at or above
``[production.live] hard_to_borrow_fee_rate``) becomes a ticket held with
``hard_to_borrow``, even in an auto book."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.types import Order
from stonks.execution.borrow import BorrowQuote, BorrowSettings, FlatBorrow, is_hard_to_borrow
from stonks.production.live.settings import LiveSettings
from stonks.production.tickets import hard_to_borrow_orders, ticket_hold

DAY = date(2026, 9, 28)


def short(ticker: str = "GME.US", **kw) -> Order:
    base = {"client_id": f"c-{ticker}", "ticker": ticker, "side": "sell", "quantity": 10.0,
            "position_effect": "open", "strategy_id": "s_auto"}  # fmt: skip
    base.update(kw)
    return Order(**base)


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        (BorrowQuote("hard", 0.01), True),  # the source says hard (IBKR's shortable level)
        (BorrowQuote("easy", 0.05), True),  # the fee is above the setting
        (BorrowQuote("easy", 0.03), True),  # at the setting counts
        (BorrowQuote("easy", 0.005), False),
        (BorrowQuote("none"), False),  # no locate: the broker refuses it anyway
        (None, False),  # no quote: no short at all
    ],
)
def test_is_hard_to_borrow(quote, expected):
    assert is_hard_to_borrow(quote, fee_rate=0.03) is expected


def test_the_setting_defaults_to_three_percent_a_year():
    assert LiveSettings().hard_to_borrow_fee_rate == 0.03
    with pytest.raises(ValueError):
        LiveSettings(hard_to_borrow_fee_rate=-0.01)


def test_only_short_opening_orders_of_hard_names_are_held():
    borrow = FlatBorrow(BorrowSettings(hard=("GME.US",), hard_fee_rate_annual=0.25))
    orders = [
        short("GME.US"),
        short("AAPL.US"),  # easy, at the general fee
        short("GME.US", client_id="close-GME", position_effect="close"),  # a long's sale
        Order("buy-GME", "GME.US", "buy", 5, position_effect="open"),
    ]
    assert hard_to_borrow_orders(orders, borrow, DAY, fee_rate=0.03) == {"c-GME.US"}


def test_no_borrow_source_holds_nothing():
    assert hard_to_borrow_orders([short()], None, DAY, fee_rate=0.03) == frozenset()


def test_a_failing_borrow_source_holds_nothing_and_never_raises():
    class Broken(FlatBorrow):
        def quote(self, ticker, day, asset_class="equity"):
            raise RuntimeError("gateway down")

    assert hard_to_borrow_orders([short()], Broken(), DAY, fee_rate=0.03) == frozenset()


def test_ticket_hold_names_hard_to_borrow_even_in_auto():
    order = short()
    auto = {"s_auto"}

    def hold(**kw):
        return ticket_hold(order, approve_strategies=set(), auto_strategies=auto, **kw)

    assert hold(runaway=False, hard_to_borrow=True) == "hard_to_borrow"
    # a runaway still wins: every order of the run waits for that reason
    assert hold(runaway=True, hard_to_borrow=True) == "runaway"
    assert hold(runaway=False) is None
