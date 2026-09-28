"""CSV statements (roadmap 23.17): columns mapped onto the activities a
broker connection syncs, with stable ids for duplicate protection."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.connections.statement_csv import (
    ColumnMapping,
    StatementError,
    guess_mapping,
    parse_statement,
)

CSV = """Date,Action,Symbol,Quantity,Price,Amount,Commission,Currency,Notes
2026-01-05,BUY,AAPL,10,190.00,-1900.00,1.00,USD,first buy
2026-01-05,BUY,AAPL,10,190.00,-1900.00,1.00,USD,first buy
2026-02-12,DIVIDEND,AAPL,,,2.50,,USD,
2026-03-01,SELL,MSFT.US,5,400,2000,1,USD,
2026-03-02,DEPOSIT,,,,5000,,USD,wire
2026-03-03,TRANSFER,,,,1,,USD,
,BUY,AAPL,1,1,1,,USD,
"""

MAPPING = ColumnMapping(
    date="Date",
    type="Action",
    symbol="Symbol",
    quantity="Quantity",
    price="Price",
    amount="Amount",
    fee="Commission",
    currency="Currency",
    description="Notes",
    types={"BUY": "trade", "SELL": "trade", "DIVIDEND": "dividend", "DEPOSIT": "deposit"},
    sell_values=["SELL"],
)


def test_rows_become_activities():
    rows = parse_statement(CSV, MAPPING)
    assert len(rows) == 7
    buy, twin, div, sell, dep, transfer, blank = rows
    assert buy.activity is not None and buy.activity.kind == "trade"
    assert buy.activity.ticker == "AAPL.US" and buy.activity.quantity == 10
    assert buy.activity.trade_date == date(2026, 1, 5) and buy.activity.fee == 1.0
    assert div.activity.kind == "dividend" and div.activity.amount == 2.5
    assert sell.activity.quantity == -5 and sell.activity.ticker == "MSFT.US"
    assert dep.activity.kind == "deposit" and dep.activity.raw_symbol is None
    # identical rows in one file are two activities with two ids
    assert buy.activity.provider_activity_id != twin.activity.provider_activity_id
    assert transfer.activity is None and "TRANSFER" in transfer.skipped
    assert blank.activity is None and "date" in blank.skipped


def test_ids_are_stable_across_imports():
    first = [r.activity.provider_activity_id for r in parse_statement(CSV, MAPPING) if r.activity]
    again = [r.activity.provider_activity_id for r in parse_statement(CSV, MAPPING) if r.activity]
    assert first == again and all(i.startswith("csv:") for i in first)


def test_a_fixed_kind_and_day_first_dates():
    text = "When,What,Qty,Px\n05/01/2026,VOD.LSE,3,1.2\n"
    mapping = ColumnMapping(
        date="When", symbol="What", quantity="Qty", price="Px", kind="trade", date_format="%d/%m/%Y"
    )
    [row] = parse_statement(text, mapping)
    assert row.activity.trade_date == date(2026, 1, 5)
    assert row.activity.ticker == "VOD.LSE"
    assert row.activity.amount == pytest.approx(-3.6)  # bought: money out


def test_unknown_columns_are_refused():
    with pytest.raises(StatementError, match="Nope"):
        parse_statement(CSV, ColumnMapping(date="Nope", kind="trade"))
    with pytest.raises(ValueError, match="type column or a kind"):
        ColumnMapping(date="Date")


def test_guess_mapping_reads_common_headers():
    guess = guess_mapping(["Trade Date", "Type", "Ticker", "Shares", "Price", "Net Amount", "Fees"])
    assert guess.date == "Trade Date" and guess.type == "Type" and guess.symbol == "Ticker"
    assert guess.quantity == "Shares" and guess.amount == "Net Amount" and guess.fee == "Fees"
    assert guess.types["BUY"] == "trade" and "SELL" in guess.sell_values


def test_too_many_rows_are_refused():
    text = "Date,Action\n" + "2026-01-01,BUY\n" * 10
    with pytest.raises(StatementError, match="at most 5"):
        parse_statement(text, ColumnMapping(date="Date", type="Action"), max_rows=5)
