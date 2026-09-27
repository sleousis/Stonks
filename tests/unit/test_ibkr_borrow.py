"""IBKR borrow checks (roadmap 19.3): ``IbkrBorrowSource`` over
``FakeIbGateway`` (the shortable indicator and shares) plus the lake fee,
and ``IbkrBroker`` refusing a short sale without a locate."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stonks.core.clock import FixedClock
from stonks.core.types import Order
from stonks.execution.borrow import BorrowQuote, FlatBorrow
from stonks.execution.brokers.base import OrderRejectedError
from stonks.execution.brokers.ibkr.borrow import IbkrBorrowSource, borrow_status
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbShortability
from tests.fakes.ib_gateway import AAPL, MSFT, T0, FakeIbGateway

TODAY = T0.date()
CLOCK = FixedClock(T0)


def short(ticker: str = "AAPL.US", qty: float = 10.0) -> Order:
    return Order(
        client_id=f"t1-s1-{ticker}-sell",
        ticker=ticker,
        side="sell",
        quantity=qty,
        position_effect="open",
        decision_price=200.0,
    )


def broker(gw: FakeIbGateway | None = None, **kw) -> tuple[IbkrBroker, FakeIbGateway]:
    gw = gw or FakeIbGateway()
    kw.setdefault("mode", "paper")
    kw.setdefault("clock", CLOCK)
    return IbkrBroker(gw, **kw), gw


def lake_fees(**quotes: BorrowQuote) -> FlatBorrow:
    return FlatBorrow(overrides={k.replace("_", "."): v for k, v in quotes.items()})


@pytest.mark.parametrize(
    ("indicator", "status"),
    [(3.0, "easy"), (2.6, "easy"), (2.0, "hard"), (1.0, "none"), (None, None)],
)
def test_borrow_status_follows_ibkrs_indicator(indicator, status):
    assert borrow_status(indicator) == status


def test_easy_name_takes_the_lake_fee_and_live_shares():
    b, gw = broker()
    gw.shortable_data[AAPL.contract.con_id] = IbShortability(AAPL.contract.con_id, 3.0, 50_000.0)
    fees = lake_fees(AAPL_US=BorrowQuote("easy", 0.0025, 9_000_000.0))
    source = IbkrBorrowSource(b, fees=fees)
    assert source.quote("AAPL.US", TODAY) == BorrowQuote("easy", 0.0025, 50_000.0)


def test_hard_name_without_a_known_fee_is_no_quote():
    b, gw = broker()
    gw.shortable_data[AAPL.contract.con_id] = IbShortability(AAPL.contract.con_id, 2.0, 100.0)
    assert IbkrBorrowSource(b).quote("AAPL.US", TODAY) is None
    fees = lake_fees(AAPL_US=BorrowQuote("hard", 0.12))
    assert IbkrBorrowSource(b, fees=fees).quote("AAPL.US", TODAY) == BorrowQuote("hard", 0.12, 100.0)


def test_easy_name_without_a_known_fee_uses_the_general_fee():
    b, gw = broker()
    gw.shortable_data[AAPL.contract.con_id] = IbShortability(AAPL.contract.con_id, 3.0, None)
    quote = IbkrBorrowSource(b, general_fee_rate=0.004).quote("AAPL.US", TODAY)
    assert quote == BorrowQuote("easy", 0.004, None)


def test_not_shortable_is_none_even_with_a_lake_fee():
    b, gw = broker()
    gw.shortable_data[AAPL.contract.con_id] = IbShortability(AAPL.contract.con_id, 1.0, 0.0)
    fees = lake_fees(AAPL_US=BorrowQuote("easy", 0.0025))
    assert IbkrBorrowSource(b, fees=fees).quote("AAPL.US", TODAY) == BorrowQuote("none", 0.0, 0.0)


def test_no_answer_down_gateway_or_unknown_ticker_is_no_quote():
    b, gw = broker()
    source = IbkrBorrowSource(b)
    assert source.quote("AAPL.US", TODAY) is None  # no shortable data
    assert source.quote("ZZZZ.US", TODAY) is None  # contract not found
    down, gw2 = broker()
    gw2.connect_failures = 1
    gw2.shortable_data[AAPL.contract.con_id] = IbShortability(AAPL.contract.con_id, 3.0, 1.0)
    assert IbkrBorrowSource(down).quote("AAPL.US", TODAY) is None


def test_an_earlier_day_reads_the_fee_history_only():
    b, gw = broker()
    gw.shortable_data[AAPL.contract.con_id] = IbShortability(AAPL.contract.con_id, 1.0, 0.0)
    fees = lake_fees(AAPL_US=BorrowQuote("easy", 0.0025, 1.0))
    source = IbkrBorrowSource(b, fees=fees)
    assert source.has_history
    assert source.quote("AAPL.US", TODAY - timedelta(days=3)) == BorrowQuote("easy", 0.0025, 1.0)
    assert IbkrBorrowSource(b).quote("AAPL.US", date(2020, 1, 1)) is None


def test_live_answers_are_cached_for_the_day():
    b, gw = broker()
    gw.shortable_data[MSFT.contract.con_id] = IbShortability(MSFT.contract.con_id, 3.0, 5.0)
    source = IbkrBorrowSource(b)
    source.quote("MSFT.US", TODAY)
    source.quote("MSFT.US", TODAY)
    assert gw.shortable_requests == 1


# ---- the broker refuses a short sale without a locate ------------------------------------


def test_cash_account_refuses_every_short_sale():
    b, gw = broker()
    gw.shortable_data[AAPL.contract.con_id] = IbShortability(AAPL.contract.con_id, 3.0, 1e6)
    b.borrow = IbkrBorrowSource(b)
    with pytest.raises(OrderRejectedError, match="long only"):
        b.place_order(short())
    assert gw.sent == []


def test_margin_account_shorts_only_with_a_locate():
    b, gw = broker(allow_short=True, account_type="margin")
    with pytest.raises(OrderRejectedError, match="borrow"):
        b.place_order(short())  # no borrow source at all
    b.borrow = IbkrBorrowSource(b)
    with pytest.raises(OrderRejectedError, match="borrow"):
        b.place_order(short())  # no quote
    gw.shortable_data[AAPL.contract.con_id] = IbShortability(AAPL.contract.con_id, 3.0, 5.0)
    b.borrow = IbkrBorrowSource(b)
    with pytest.raises(OrderRejectedError, match="5"):
        b.place_order(short(qty=10))  # fewer shares to lend than the order
    assert gw.sent == []
    gw.shortable_data[AAPL.contract.con_id] = IbShortability(AAPL.contract.con_id, 3.0, 1e6)
    b.borrow = IbkrBorrowSource(b)
    b.place_order(short(qty=10))
    assert len(gw.sent) == 1
    assert gw.sent[0][1].action == "SELL"


def test_closing_sells_never_need_a_locate():
    b, gw = broker()
    b.place_order(
        Order(client_id="t1-s1-AAPL.US-close", ticker="AAPL.US", side="sell", quantity=5.0,
              position_effect="close", decision_price=200.0)  # fmt: skip
    )
    assert len(gw.sent) == 1
