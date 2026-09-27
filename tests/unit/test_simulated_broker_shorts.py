"""Phase 16.1: short sales, margin and financing in ``SimulatedBroker``.

Every number here is worked out by hand in the comments.
"""

from __future__ import annotations

from datetime import date

import pytest

from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import AssetClass, Order, Portfolio
from stonks.execution.borrow import BorrowQuote, BorrowSource, FlatBorrow
from stonks.execution.margin import CashMargin, MarginSettings, RegTMargin

FRI = date(2026, 3, 20)
MON = date(2026, 3, 23)
TUE = date(2026, 3, 24)


def _order(cid: str, side: str = "sell", qty: float = 10.0, ticker: str = "X") -> Order:
    return Order(client_id=cid, ticker=ticker, side=side, quantity=qty)  # type: ignore[arg-type]


def _short_broker(cash: float = 10_000.0, **kw) -> SimulatedBroker:
    return SimulatedBroker(Portfolio(cash=cash), margin=RegTMargin(), allow_short=True, **kw)


def test_short_then_cover_pnl_by_hand() -> None:
    # Short 10 at 100 with a 1.00 fee: cash 10,000 + 1,000 - 1 = 10,999.
    b = _short_broker(fee_per_trade=1.0)
    b.set_prices({"X": 100.0}, as_of=FRI)
    fill = b.place_order(_order("s1"))
    assert fill is not None and fill.side == "sell" and fill.quantity == 10.0
    p = b.fetch_portfolio()
    assert p.positions == {"X": -10.0}
    assert p.cash == pytest.approx(10_999.0)
    assert b.average_cost("X") == 100.0
    # Cover 10 at 90 with a 1.00 fee: cash 10,999 - 900 - 1 = 10,098.
    # P&L = (100 - 90) x 10 - 2 fees = 98.
    b.set_prices({"X": 90.0}, as_of=MON)
    assert b.place_order(_order("c1", side="buy")) is not None
    assert p.positions == {}
    assert p.cash == pytest.approx(10_098.0)
    assert b.average_cost("X") is None


def test_short_is_off_by_default_and_the_cash_account_refuses_it() -> None:
    b = SimulatedBroker(Portfolio(cash=10_000.0))
    assert not b.allow_short and b.margin is None
    b.set_prices({"X": 100.0}, as_of=FRI)
    assert b.place_order(_order("s1")) is None
    assert b.fetch_portfolio().positions == {}
    with pytest.raises(ValueError, match="allows shorts"):
        SimulatedBroker(Portfolio(cash=1.0), allow_short=True)
    with pytest.raises(ValueError, match="cash"):
        SimulatedBroker(Portfolio(cash=1.0), margin=CashMargin(), allow_short=True)


def test_explicit_cash_margin_is_the_legacy_path() -> None:
    b = SimulatedBroker(Portfolio(cash=100.0), margin=CashMargin())
    assert b.margin is None
    b.set_prices({"X": 10.0}, as_of=FRI)
    fill = b.place_order(_order("b1", side="buy", qty=50.0))
    assert fill is not None and fill.quantity == 10.0  # scaled to cash


def test_margin_without_shorts_clips_an_oversell_to_the_holding() -> None:
    b = SimulatedBroker(Portfolio(cash=0.0, positions={"X": 4.0}), margin=RegTMargin())
    b.set_prices({"X": 10.0}, as_of=FRI)
    fill = b.place_order(_order("s1", qty=10.0))
    assert fill is not None and fill.quantity == 4.0
    assert b.fetch_portfolio().positions == {}


def test_a_sell_that_crosses_zero_closes_then_shorts() -> None:
    b = _short_broker(cash=0.0)
    b.fetch_portfolio().positions["X"] = 10.0
    b.set_prices({"X": 50.0}, as_of=FRI)
    fill = b.place_order(_order("s1", qty=15.0))
    assert fill is not None and fill.quantity == 15.0
    assert b.fetch_portfolio().positions == {"X": -5.0}
    assert b.fetch_portfolio().cash == pytest.approx(750.0)
    assert b.average_cost("X") == 50.0  # flipped: a fresh short at 50


def test_short_is_scaled_to_the_margin_room() -> None:
    # Cash 1,000, no positions: excess 1,000. A short at 100 needs 50 a
    # share (50 % initial), so at most 20 shares.
    b = _short_broker(cash=1_000.0)
    b.set_prices({"X": 100.0}, as_of=FRI)
    fill = b.place_order(_order("s1", qty=30.0))
    assert fill is not None and fill.quantity == pytest.approx(20.0)
    assert b.unfilled_quantity("s1") == 0.0


def test_leveraged_long_on_reg_t() -> None:
    # Cash 1,000: a long at 10 needs 5 a share, so 200 shares on margin.
    b = SimulatedBroker(Portfolio(cash=1_000.0), margin=RegTMargin())
    b.set_prices({"X": 10.0}, as_of=FRI)
    fill = b.place_order(_order("b1", side="buy", qty=500.0))
    assert fill is not None and fill.quantity == pytest.approx(200.0)
    assert b.fetch_portfolio().cash == pytest.approx(-1_000.0)


def test_no_margin_room_rejects() -> None:
    b = _short_broker(cash=0.0)
    b.set_prices({"X": 10.0}, as_of=FRI)
    assert b.place_order(_order("s1")) is None


def test_a_cover_is_never_limited_by_margin() -> None:
    # Deep in the red: cash 1,000, short 100 at 50 now 60. The cover still fills.
    b = _short_broker(cash=1_000.0)
    b.fetch_portfolio().positions["X"] = -100.0
    b.set_prices({"X": 60.0}, as_of=FRI)
    fill = b.place_order(_order("c1", side="buy", qty=100.0))
    assert fill is not None and fill.quantity == 100.0
    assert b.fetch_portfolio().cash == pytest.approx(-5_000.0)


def test_not_borrowable_rejects_and_available_shares_cap() -> None:
    borrow = FlatBorrow(
        overrides={
            "NONE": BorrowQuote("none"),
            "FEW": BorrowQuote("hard", 0.2, available_shares=3.0),
        }
    )
    b = _short_broker(borrow=borrow)
    b.set_prices({"NONE": 10.0, "FEW": 10.0}, as_of=FRI)
    assert b.place_order(_order("s1", ticker="NONE")) is None
    fill = b.place_order(_order("s2", ticker="FEW"))
    assert fill is not None and fill.quantity == 3.0


def test_borrow_fee_accrues_daily_by_hand() -> None:
    # Short 100 at 50, 3.6 %/yr: 5,000 x 0.036 / 360 = 0.50 a calendar day.
    borrow = FlatBorrow(overrides={"X": BorrowQuote("easy", 0.036)})
    b = _short_broker(borrow=borrow)
    b.set_prices({"X": 50.0}, as_of=FRI)
    b.place_order(_order("s1", qty=100.0))
    cash = b.fetch_portfolio().cash
    assert b.accrue(FRI) == []  # the first call only starts the clock
    assert b.accrue(FRI) == []  # same day again: nothing
    # Friday to Monday is three calendar days: 1.50.
    [event] = b.accrue(MON)
    assert (event.kind, event.ticker, event.days) == ("borrow_fee", "X", 3)
    assert event.amount == pytest.approx(-1.5)
    # At 60 the next day costs 6,000 x 0.036 / 360 = 0.60.
    b.set_prices({"X": 60.0}, as_of=TUE)
    [event] = b.accrue(TUE)
    assert event.amount == pytest.approx(-0.6)
    assert b.fetch_portfolio().cash == pytest.approx(cash - 2.1)
    assert [e.amount for e in b.financing] == pytest.approx([-1.5, -0.6])


def test_accrue_since_overrides_the_clock() -> None:
    borrow = FlatBorrow(overrides={"X": BorrowQuote("easy", 0.036)})
    b = _short_broker(borrow=borrow)
    b.set_prices({"X": 50.0}, as_of=MON)
    b.place_order(_order("s1", qty=100.0))
    [event] = b.accrue(MON, since=FRI)
    assert event.amount == pytest.approx(-1.5)


def test_debit_interest_on_negative_cash() -> None:
    # Cash -1,000 at 7.2 %/yr: 1,000 x 0.072 / 360 = 0.20 a day.
    margin = MarginSettings(model="reg_t", debit_rate_annual=0.072).build()
    b = SimulatedBroker(Portfolio(cash=-1_000.0, positions={"X": 100.0}), margin=margin)
    b.set_prices({"X": 50.0}, as_of=FRI)
    b.accrue(FRI)
    [event] = b.accrue(date(2026, 3, 21))
    assert (event.kind, event.ticker) == ("debit_interest", None)
    assert event.amount == pytest.approx(-0.2)


def test_a_long_only_cash_book_accrues_nothing() -> None:
    b = SimulatedBroker(Portfolio(cash=100.0, positions={"X": 1.0}))
    b.set_prices({"X": 10.0}, as_of=FRI)
    b.accrue(FRI)
    assert b.accrue(MON) == []
    assert b.fetch_portfolio().cash == 100.0
    assert b.margin_deficit() == 0.0
    assert b.margin_call() == []
    assert b.recalled(MON) == []


def test_unpriced_or_unquoted_shorts_pay_no_fee() -> None:
    class NoData(BorrowSource):
        def quote(self, ticker: str, day: date, asset_class: AssetClass = "equity"):
            return None

    b = SimulatedBroker(
        Portfolio(cash=10_000.0, positions={"X": -1.0, "Y": -1.0}),
        margin=RegTMargin(),
        allow_short=True,
        borrow=NoData(),
    )
    b.set_prices({"X": 10.0}, as_of=FRI)
    b.accrue(FRI)
    assert b.accrue(MON) == []


def test_margin_call_plans_the_cover_by_hand() -> None:
    # Cash 15,000 short 100 X (entered at 50), now 120: equity 3,000,
    # maintenance 0.3 x 12,000 = 3,600, deficit 600. A cover frees
    # 0.3 x 120 = 36 a share: 600 / 36 = 16.67 shares.
    b = _short_broker(cash=10_000.0)
    b.set_prices({"X": 50.0}, as_of=FRI)
    b.place_order(_order("s1", qty=100.0))
    b.set_prices({"X": 120.0}, as_of=MON)
    assert b.margin_deficit() == pytest.approx(600.0)
    [(ticker, qty)] = b.margin_call()
    assert ticker == "X" and qty == pytest.approx(600.0 / 36.0)


def test_margin_call_closes_the_most_losing_position_first() -> None:
    b = _short_broker(cash=10_000.0)
    b.set_prices({"A": 50.0, "B": 50.0, "L": 10.0}, as_of=FRI)
    b.place_order(_order("a", qty=100.0, ticker="A"))
    b.place_order(_order("b", qty=100.0, ticker="B"))
    b.place_order(_order("l", side="buy", qty=10.0, ticker="L"))
    b.set_prices({"A": 130.0, "B": 100.0, "L": 5.0}, as_of=MON)
    plan = b.margin_call()
    assert plan[0][0] == "A"  # lost 8,000; B lost 5,000; L lost 50
    assert all(q > 0 for t, q in plan if t in {"A", "B"})


def test_margin_call_sells_a_losing_long() -> None:
    b = SimulatedBroker(Portfolio(cash=0.0), margin=RegTMargin())
    b.fetch_portfolio().cash = -800.0
    b.fetch_portfolio().positions["X"] = 100.0
    b.set_prices({"X": 10.0}, as_of=FRI)
    # Equity 200, maintenance 0.25 x 1,000 = 250, deficit 50: sell 50 / 2.5 = 20.
    [(ticker, qty)] = b.margin_call()
    assert ticker == "X" and qty == pytest.approx(-20.0)


def test_recall_from_a_source_with_history() -> None:
    class Recalling(BorrowSource):
        has_history = True

        def quote(self, ticker: str, day: date, asset_class: AssetClass = "equity"):
            return BorrowQuote("none") if day >= MON else BorrowQuote("easy", 0.01)

    b = _short_broker(borrow=Recalling())
    b.set_prices({"X": 10.0}, as_of=FRI)
    b.place_order(_order("s1"))
    b.fetch_portfolio().positions["L"] = 1.0
    assert b.recalled(FRI) == []
    assert b.recalled(MON) == ["X"]


def test_average_cost_grows_shrinks_and_splits() -> None:
    b = _short_broker(cash=100_000.0)
    b.set_prices({"X": 10.0}, as_of=FRI)
    b.place_order(_order("s1", qty=10.0))
    b.set_prices({"X": 20.0}, as_of=MON)
    b.place_order(_order("s2", qty=10.0))
    assert b.average_cost("X") == pytest.approx(15.0)
    b.place_order(_order("c1", side="buy", qty=5.0))
    assert b.average_cost("X") == pytest.approx(15.0)  # a partial cover keeps it
    b.rescale_cost("X", 3.0)
    assert b.average_cost("X") == pytest.approx(5.0)
    b.rescale_cost("NOPE", 3.0)


# ---- BE-13, BE-31: closes never open, dust never leaves a short ----------------------------


def test_be13_a_close_larger_than_the_holding_fills_only_the_holding() -> None:
    b = _short_broker(borrow=FlatBorrow())
    b.set_prices({"X": 10.0}, as_of=FRI)
    b.place_order(_order("b1", side="buy", qty=5.0))
    close = Order("s1", "X", "sell", 8.0, position_effect="close")
    fill = b.place_order(close)
    assert fill is not None and fill.quantity == pytest.approx(5.0)
    assert b.fetch_portfolio().positions == {}
    # nothing left to close: refused
    assert b.place_order(Order("s2", "X", "sell", 1.0, position_effect="close")) is None


def test_be13_an_open_on_the_wrong_side_is_refused() -> None:
    b = _short_broker(borrow=FlatBorrow())
    b.set_prices({"X": 10.0}, as_of=FRI)
    b.place_order(_order("b1", side="buy", qty=5.0))
    assert b.place_order(Order("s1", "X", "sell", 3.0, position_effect="open")) is None
    assert b.fetch_portfolio().positions == {"X": 5.0}


def test_be31_a_dust_oversell_on_margin_ends_flat() -> None:
    b = _short_broker(cash=100_000.0, borrow=FlatBorrow())
    b.set_prices({"X": 10.0}, as_of=FRI)
    b.place_order(_order("b1", side="buy", qty=1000.0))
    fill = b.place_order(_order("s1", side="sell", qty=1000.0 * (1 + 5e-10)))
    assert fill is not None and fill.quantity == 1000.0
    assert b.fetch_portfolio().positions == {}
