"""Realized gains per lot (roadmap 20.5): FIFO, specific lots, shorts,
holding period and US wash sales."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks.tax.lots import TaxFill, TaxSettings, realized_disposals

NO_WASH = TaxSettings(wash_sales=False)


def fill(i: int, side: str, qty: float, price: float, day: str, fee: float = 0.0, t: str = "A"):
    return TaxFill(
        id=i,
        ticker=t,
        side=side,  # type: ignore[arg-type]
        quantity=qty,
        price=price,
        fee=fee,
        filled_at=datetime.fromisoformat(day).replace(hour=12, tzinfo=UTC),
        currency="USD",
    )


def test_fifo_closes_oldest_lots_with_fees():
    fills = [
        fill(1, "buy", 10, 100, "2024-01-02", fee=10),
        fill(2, "buy", 10, 120, "2024-02-01"),
        fill(3, "sell", 15, 130, "2024-03-01", fee=15),
    ]
    d = realized_disposals(fills, NO_WASH)
    assert [(x.open_fill_id, x.quantity) for x in d] == [(1, 10), (2, 5)]
    assert d[0].cost_basis == pytest.approx(1010)  # fee in the cost
    assert d[0].proceeds == pytest.approx(10 * 129)  # sell fee off the proceeds
    assert d[1].gain == pytest.approx(5 * 129 - 5 * 120)
    assert all(x.holding_period == "short" for x in d)


def test_holding_period_is_long_after_more_than_a_year():
    fills = [
        fill(1, "buy", 1, 10, "2023-01-02"),
        fill(2, "buy", 1, 10, "2023-01-03"),
        fill(3, "sell", 1, 12, "2024-01-02"),  # exactly one year: short
        fill(4, "sell", 1, 12, "2024-01-04"),
    ]
    d = realized_disposals(fills, NO_WASH)
    assert [x.holding_period for x in d] == ["short", "long"]


def test_specific_lots_then_fifo_for_the_rest():
    fills = [
        fill(1, "buy", 10, 100, "2024-01-02"),
        fill(2, "buy", 10, 150, "2024-02-01"),
        fill(3, "sell", 12, 160, "2024-03-01"),
    ]
    picks = {3: [(2, 8.0)]}
    d = realized_disposals(fills, TaxSettings(lot_method="specific", wash_sales=False), picks)
    assert [(x.open_fill_id, x.quantity) for x in d] == [(2, 8), (1, 4)]
    fifo = realized_disposals(fills, NO_WASH, picks)  # picks ignored under FIFO
    assert [(x.open_fill_id, x.quantity) for x in fifo] == [(1, 10), (2, 2)]


def test_short_sale_is_realized_at_the_cover():
    fills = [fill(1, "sell", 5, 50, "2024-01-02"), fill(2, "buy", 8, 40, "2024-01-10")]
    d = realized_disposals(fills, NO_WASH)
    assert len(d) == 1
    short = d[0]
    assert (short.kind, short.quantity, short.gain) == ("short", 5, pytest.approx(50))
    assert short.acquired.isoformat() == "2024-01-02" and short.holding_period == "short"
    # the 3 extra shares opened a long lot
    more = realized_disposals([*fills, fill(3, "sell", 3, 45, "2024-01-11")], NO_WASH)
    assert more[-1].open_fill_id == 2 and more[-1].gain == pytest.approx(15)


def test_wash_sale_with_a_buy_after_the_loss():
    fills = [
        fill(1, "buy", 10, 100, "2024-01-02"),
        fill(2, "sell", 10, 80, "2024-03-01"),  # loss 200
        fill(3, "buy", 4, 85, "2024-03-20"),  # 4 replacement shares inside 30 days
        fill(4, "sell", 4, 90, "2024-06-01"),
    ]
    d = realized_disposals(fills, TaxSettings())
    loss, later = d
    assert loss.wash_sale_disallowed == pytest.approx(200 * 4 / 10)
    assert loss.gain == pytest.approx(-200 + 80)
    # the disallowed 80 is added to the replacement's basis
    assert later.cost_basis == pytest.approx(4 * 85 + 80)
    assert later.gain == pytest.approx(4 * 90 - 4 * 85 - 80)


def test_wash_sale_with_a_buy_before_the_loss():
    fills = [
        fill(1, "buy", 10, 100, "2024-01-02"),
        fill(2, "buy", 10, 90, "2024-02-20"),  # inside 30 days before the sale
        fill(3, "sell", 10, 80, "2024-03-01"),  # FIFO sells lot 1 at a loss of 200
        fill(4, "sell", 10, 85, "2024-05-01"),
    ]
    first, second = realized_disposals(fills, TaxSettings())
    assert first.open_fill_id == 1 and first.wash_sale_disallowed == pytest.approx(200)
    assert second.cost_basis == pytest.approx(900 + 200)


def test_no_wash_sale_outside_the_window_for_gains_or_outside_us():
    base = [fill(1, "buy", 10, 100, "2024-01-02"), fill(2, "sell", 10, 80, "2024-03-01")]
    outside = realized_disposals([*base, fill(3, "buy", 10, 70, "2024-04-15")], TaxSettings())
    assert outside[0].wash_sale_disallowed == 0
    eu = realized_disposals(
        [*base, fill(3, "buy", 10, 70, "2024-03-10")], TaxSettings(jurisdiction="eu")
    )
    assert eu[0].wash_sale_disallowed == 0
    gain = realized_disposals(
        [
            fill(1, "buy", 10, 50, "2024-01-02"),
            fill(2, "sell", 10, 80, "2024-03-01"),
            fill(3, "buy", 10, 70, "2024-03-10"),
        ],
        TaxSettings(),
    )
    assert gain[0].wash_sale_disallowed == 0


def test_replacement_shares_are_used_once():
    fills = [
        fill(1, "buy", 10, 100, "2024-01-02"),
        fill(2, "sell", 5, 90, "2024-03-01"),  # loss 50
        fill(3, "sell", 5, 90, "2024-03-02"),  # loss 50
        fill(4, "buy", 5, 95, "2024-03-10"),  # covers the first sale only
    ]
    a, b = realized_disposals(fills, TaxSettings())
    assert a.wash_sale_disallowed == pytest.approx(50)
    assert b.wash_sale_disallowed == 0


def test_wash_sale_does_not_use_a_lot_already_sold_as_replacement():
    # Two lots, both sold at a loss: the position ends flat, so the whole
    # loss is allowed. Lot 1 was sold on day 3 and cannot replace lot 2.
    fills = [
        fill(1, "buy", 100, 10, "2024-03-01"),
        fill(2, "buy", 100, 10, "2024-03-02"),
        fill(3, "sell", 100, 8, "2024-03-03"),
        fill(4, "sell", 100, 8, "2024-03-04"),
    ]
    d = realized_disposals(fills, TaxSettings())
    assert sum(x.gain for x in d) == pytest.approx(-400)


def test_a_split_rescales_open_lots():
    from datetime import date

    from stonks.tax.lots import TaxSplit

    fills = [
        fill(1, "buy", 100, 100, "2024-01-02"),
        fill(2, "sell", 400, 26, "2024-03-01"),
    ]
    splits = [TaxSplit(ticker="A", ex_date=date(2024, 2, 1), ratio=4.0)]
    d = realized_disposals(fills, NO_WASH, splits=splits)
    assert [(x.kind, x.quantity) for x in d] == [("long", 400)]
    assert d[0].gain == pytest.approx(400)
    assert d[0].acquired == date(2024, 1, 2)
