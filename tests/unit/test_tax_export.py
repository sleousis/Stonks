"""Tax CSV exports (roadmap 20.5): year filter, FX to the base currency at
the trade days, empty base amounts when a rate is missing, dividends."""

from __future__ import annotations

import csv
import io
from datetime import date

import pytest

from stonks.fx import FxRates
from stonks.tax import (
    DIVIDEND_COLUMNS,
    GAINS_COLUMNS,
    Disposal,
    DividendEvent,
    dividend_rows,
    gains_rows,
    to_csv,
)

FX = FxRates([("EUR", "USD", date(2024, 1, 1), 1.10), ("EUR", "USD", date(2024, 6, 1), 1.20)])


def disposal(**kw) -> Disposal:
    base = {
        "ticker": "SAP.XETRA",
        "kind": "long",
        "quantity": 10.0,
        "acquired": date(2024, 2, 1),
        "disposed": date(2024, 7, 1),
        "proceeds": 1500.0,
        "cost_basis": 1000.0,
        "wash_sale_disallowed": 0.0,
        "currency": "EUR",
        "open_fill_id": 1,
        "close_fill_id": 2,
    }
    base.update(kw)
    return Disposal(**base)  # type: ignore[arg-type]


def parse(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def test_gains_convert_cost_and_proceeds_at_their_own_days():
    rows = gains_rows([disposal()], 2024, "USD", FX)
    (row,) = parse(to_csv(GAINS_COLUMNS, rows))
    assert row["gain"] == "500.00"
    assert float(row["cost_basis_base"]) == pytest.approx(1000 * 1.10)
    assert float(row["proceeds_base"]) == pytest.approx(1500 * 1.20)
    assert float(row["gain_base"]) == pytest.approx(1500 * 1.20 - 1000 * 1.10)
    assert row["holding_period"] == "short" and row["base_currency"] == "USD"


def test_gains_keep_only_the_year_and_leave_missing_fx_empty():
    rows = gains_rows([disposal(), disposal(disposed=date(2025, 1, 5))], 2024, "USD", FX)
    assert len(rows) == 1
    (row,) = gains_rows([disposal(currency="CHF")], 2024, "USD", FX)
    assert row["proceeds_base"] == "" and row["gain_base"] == "" and row["gain"] == "500.00"


def test_dividends_with_withholding():
    events = [
        DividendEvent("SAP.XETRA", date(2024, 6, 15), 2.0, 10.0, 15.0, "EUR"),
        DividendEvent("SAP.XETRA", date(2023, 6, 15), 2.0, 10.0, 20.0, "EUR"),
    ]
    text = to_csv(DIVIDEND_COLUMNS, dividend_rows(events, 2024, "USD", FX))
    (row,) = parse(text)
    assert (row["gross"], row["withholding"], row["net"]) == ("20.00", "5.00", "15.00")
    assert float(row["net_base"]) == pytest.approx(15 * 1.20)
    assert text.splitlines()[0] == ",".join(DIVIDEND_COLUMNS)
