"""Open tax lots on a day (roadmap 13.12): what is left after FIFO,
specific picks, shorts, splits and wash sale basis, with the holding
period, and the CSV rows."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime

import pytest

from stonks.fx import FxRates
from stonks.tax import OPEN_LOT_COLUMNS, open_lot_rows, open_lots, to_csv
from stonks.tax.lots import TaxFill, TaxSettings, TaxSplit

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


def test_fifo_leaves_the_newest_lots_open_with_fees_in_the_cost():
    fills = [
        fill(1, "buy", 10, 100, "2024-01-02", fee=10),
        fill(2, "buy", 10, 120, "2024-02-01", fee=5),
        fill(3, "sell", 15, 130, "2024-03-01"),
    ]
    lots = open_lots(fills, date(2024, 3, 1), NO_WASH)
    assert [(x.open_fill_id, x.quantity) for x in lots] == [(2, 5)]
    assert lots[0].cost_basis == pytest.approx(5 * 120.5)
    assert lots[0].kind == "long"


def test_fills_after_the_day_are_left_out():
    fills = [fill(1, "buy", 10, 100, "2024-01-02"), fill(2, "sell", 10, 110, "2024-03-01")]
    assert [x.quantity for x in open_lots(fills, date(2024, 2, 1), NO_WASH)] == [10]
    assert open_lots(fills, date(2024, 3, 1), NO_WASH) == []


def test_specific_picks_decide_which_lot_stays():
    fills = [
        fill(1, "buy", 10, 100, "2024-01-02"),
        fill(2, "buy", 10, 150, "2024-02-01"),
        fill(3, "sell", 10, 160, "2024-03-01"),
    ]
    specific = TaxSettings(lot_method="specific", wash_sales=False)
    lots = open_lots(fills, date(2024, 3, 2), specific, {3: [(2, 10)]})
    assert [x.open_fill_id for x in lots] == [1]


def test_holding_period_turns_long_the_day_after_one_year():
    lots = open_lots([fill(1, "buy", 1, 10, "2023-01-02")], date(2024, 1, 2), NO_WASH)
    lot = lots[0]
    assert lot.holding_period(date(2024, 1, 2)) == "short"
    assert lot.long_term_on() == date(2024, 1, 3)
    assert lot.holding_period(date(2024, 1, 3)) == "long"
    assert lot.days_held(date(2024, 1, 2)) == 365


def test_a_short_lot_is_always_short_term():
    lots = open_lots([fill(1, "sell", 5, 50, "2022-01-03", fee=5)], date(2024, 1, 2), NO_WASH)
    assert lots[0].kind == "short"
    assert lots[0].cost_basis == pytest.approx(5 * 49)  # proceeds less the fee
    assert lots[0].holding_period(date(2024, 1, 2)) == "short"
    assert lots[0].long_term_on() is None


def test_a_split_after_the_last_fill_still_rescales_the_lot():
    fills = [fill(1, "buy", 10, 100, "2024-01-02")]
    splits = [TaxSplit("A", date(2024, 6, 3), 2.0)]
    before = open_lots(fills, date(2024, 6, 1), NO_WASH, splits=splits)
    after = open_lots(fills, date(2024, 6, 3), NO_WASH, splits=splits)
    assert before[0].quantity == 10
    assert (after[0].quantity, after[0].per_share) == (20, 50)
    assert after[0].cost_basis == pytest.approx(1000)


def test_wash_sale_basis_moves_onto_the_replacement_lot():
    fills = [
        fill(1, "buy", 10, 100, "2024-01-02"),
        fill(2, "sell", 10, 80, "2024-03-01"),
        fill(3, "buy", 10, 85, "2024-03-10"),
    ]
    lots = open_lots(fills, date(2024, 3, 10), TaxSettings())
    assert [x.open_fill_id for x in lots] == [3]
    assert lots[0].wash_sale_adjustment == pytest.approx(200)
    assert lots[0].cost_basis == pytest.approx(850 + 200)


def test_rows_value_lots_at_the_price_and_convert_to_the_base_currency():
    fx = FxRates([("EUR", "USD", date(2024, 1, 1), 1.10), ("EUR", "USD", date(2024, 6, 1), 1.20)])
    lots = open_lots(
        [
            TaxFill(1, "SAP.XETRA", "buy", 10, 100, 0, datetime(2024, 2, 1, tzinfo=UTC), "EUR"),
            TaxFill(2, "B", "sell", 2, 50, 0, datetime(2024, 2, 1, tzinfo=UTC), "USD"),
        ],
        date(2024, 7, 1),
        NO_WASH,
    )
    rows = open_lot_rows(lots, date(2024, 7, 1), "USD", fx, {"SAP.XETRA": 120.0})
    by = {r["ticker"]: r for r in rows}
    sap = by["SAP.XETRA"]
    assert sap["cost_basis"] == "1000.00"
    assert sap["market_value"] == "1200.00"
    assert sap["unrealized_gain"] == "200.00"
    assert sap["cost_basis_base"] == "1100.00"  # at the acquired day's rate
    assert sap["market_value_base"] == "1440.00"  # at the report day's rate
    assert sap["unrealized_gain_base"] == "340.00"
    assert sap["holding_period"] == "short"
    assert sap["long_term_on"] == "2025-02-02"
    short = by["B"]
    assert short["lot_kind"] == "short"
    assert short["price"] == ""
    assert short["market_value"] == ""
    assert short["unrealized_gain"] == ""
    assert short["cost_basis_base"] == "100.00"

    text = to_csv(OPEN_LOT_COLUMNS, rows)
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert list(parsed[0]) == list(OPEN_LOT_COLUMNS)


def test_a_short_gains_when_the_price_falls():
    lots = open_lots([fill(1, "sell", 2, 50, "2024-02-01")], date(2024, 3, 1), NO_WASH)
    row = open_lot_rows(lots, date(2024, 3, 1), "USD", FxRates([]), {"A": 40.0})[0]
    assert row["unrealized_gain"] == "20.00"
