"""Tax exports as CSV: realized gains per lot and dividends with
withholding (roadmap 20.5), and the open lots on a day (roadmap 13.12), in
the trade currency and the base currency.

Base amounts: the cost at the acquired day's FX rate, the proceeds at the
disposed day's rate, dividends at the ex-date's rate, and an open lot's
market value at the report day's rate. An amount whose
currency has no stored rate is left empty (never guessed)."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from stonks.fx import FxRates
from stonks.tax.lots import Disposal, OpenLot

GAINS_COLUMNS = (
    "ticker",
    "lot_kind",
    "quantity",
    "acquired",
    "disposed",
    "holding_period",
    "currency",
    "proceeds",
    "cost_basis",
    "wash_sale_disallowed",
    "gain",
    "base_currency",
    "proceeds_base",
    "cost_basis_base",
    "wash_sale_disallowed_base",
    "gain_base",
    "open_fill_id",
    "close_fill_id",
)

DIVIDEND_COLUMNS = (
    "ticker",
    "ex_date",
    "quantity",
    "per_share",
    "currency",
    "gross",
    "withholding",
    "net",
    "base_currency",
    "gross_base",
    "withholding_base",
    "net_base",
)


OPEN_LOT_COLUMNS = (
    "ticker",
    "lot_kind",
    "quantity",
    "acquired",
    "days_held",
    "holding_period",
    "long_term_on",
    "currency",
    "cost_per_share",
    "cost_basis",
    "wash_sale_adjustment",
    "price",
    "market_value",
    "unrealized_gain",
    "base_currency",
    "cost_basis_base",
    "market_value_base",
    "unrealized_gain_base",
    "open_fill_id",
)


@dataclass(frozen=True)
class DividendEvent:
    ticker: str
    ex_date: date
    per_share: float
    quantity: float
    #: Cash credited after withholding.
    net: float
    currency: str | None

    @property
    def gross(self) -> float:
        return self.per_share * self.quantity

    @property
    def withholding(self) -> float:
        return self.gross - self.net


def _money(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def _qty(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def gains_rows(
    disposals: Iterable[Disposal], year: int, base: str, fx: FxRates
) -> list[dict[str, Any]]:
    """One row per disposal realized in ``year``, oldest first."""
    rows: list[dict[str, Any]] = []
    for d in disposals:
        if d.disposed.year != year:
            continue
        ccy = d.currency or base
        proceeds_base = fx.convert(d.proceeds, ccy, base, d.disposed)
        # a short's cost is paid at the cover, a long's at the purchase
        cost_day = d.disposed if d.kind == "short" else d.acquired
        cost_base = fx.convert(d.cost_basis, ccy, base, cost_day)
        wash_base = fx.convert(d.wash_sale_disallowed, ccy, base, d.disposed)
        gain_base = (
            None
            if proceeds_base is None or cost_base is None or wash_base is None
            else proceeds_base - cost_base + wash_base
        )
        rows.append(
            {
                "ticker": d.ticker,
                "lot_kind": d.kind,
                "quantity": _qty(d.quantity),
                "acquired": d.acquired.isoformat(),
                "disposed": d.disposed.isoformat(),
                "holding_period": d.holding_period,
                "currency": ccy,
                "proceeds": _money(d.proceeds),
                "cost_basis": _money(d.cost_basis),
                "wash_sale_disallowed": _money(d.wash_sale_disallowed),
                "gain": _money(d.gain),
                "base_currency": base,
                "proceeds_base": _money(proceeds_base),
                "cost_basis_base": _money(cost_base),
                "wash_sale_disallowed_base": _money(wash_base),
                "gain_base": _money(gain_base),
                "open_fill_id": d.open_fill_id,
                "close_fill_id": d.close_fill_id,
            }
        )
    return rows


def open_lot_rows(
    lots: Iterable[OpenLot],
    as_of: date,
    base: str,
    fx: FxRates,
    prices: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """One row per open lot. A short lot's basis is its sale proceeds and
    its unrealized gain is the proceeds less today's cost to cover. Price
    columns stay empty for a ticker with no price in ``prices``."""
    rows: list[dict[str, Any]] = []
    for lot in lots:
        ccy = lot.currency or base
        price = (prices or {}).get(lot.ticker)
        value = None if price is None else price * lot.quantity
        gain = None
        if value is not None:
            gain = value - lot.cost_basis if lot.kind == "long" else lot.cost_basis - value
        # a long's cost is paid at the purchase; a short's proceeds come in then too
        cost_base = fx.convert(lot.cost_basis, ccy, base, lot.acquired)
        value_base = None if value is None else fx.convert(value, ccy, base, as_of)
        gain_base = None
        if cost_base is not None and value_base is not None:
            gain_base = value_base - cost_base if lot.kind == "long" else cost_base - value_base
        starts = lot.long_term_on()
        rows.append(
            {
                "ticker": lot.ticker,
                "lot_kind": lot.kind,
                "quantity": _qty(lot.quantity),
                "acquired": lot.acquired.isoformat(),
                "days_held": lot.days_held(as_of),
                "holding_period": lot.holding_period(as_of),
                "long_term_on": starts.isoformat() if starts else "",
                "currency": ccy,
                "cost_per_share": f"{lot.cost_basis / lot.quantity:.6f}" if lot.quantity else "",
                "cost_basis": _money(lot.cost_basis),
                "wash_sale_adjustment": _money(lot.wash_sale_adjustment),
                "price": "" if price is None else f"{price:.6f}",
                "market_value": _money(value),
                "unrealized_gain": _money(gain),
                "base_currency": base,
                "cost_basis_base": _money(cost_base),
                "market_value_base": _money(value_base),
                "unrealized_gain_base": _money(gain_base),
                "open_fill_id": lot.open_fill_id,
            }
        )
    return rows


def dividend_rows(
    events: Iterable[DividendEvent], year: int, base: str, fx: FxRates
) -> list[dict[str, Any]]:
    """One row per dividend with its ex-date in ``year``, oldest first."""
    rows: list[dict[str, Any]] = []
    for e in sorted(events, key=lambda x: (x.ex_date, x.ticker)):
        if e.ex_date.year != year:
            continue
        ccy = e.currency or base

        def conv(amount: float, ccy: str = ccy, day: date = e.ex_date) -> float | None:
            return fx.convert(amount, ccy, base, day)

        rows.append(
            {
                "ticker": e.ticker,
                "ex_date": e.ex_date.isoformat(),
                "quantity": _qty(e.quantity),
                "per_share": f"{e.per_share:.6f}".rstrip("0").rstrip("."),
                "currency": ccy,
                "gross": _money(e.gross),
                "withholding": _money(e.withholding),
                "net": _money(e.net),
                "base_currency": base,
                "gross_base": _money(conv(e.gross)),
                "withholding_base": _money(conv(e.withholding)),
                "net_base": _money(conv(e.net)),
            }
        )
    return rows


def to_csv(columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> str:
    """A CSV document with a header row (``\\n`` line ends)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()
