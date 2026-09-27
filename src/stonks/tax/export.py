"""Yearly tax exports as CSV (roadmap 20.5): realized gains per lot and
dividends with withholding, in the trade currency and the base currency.

Base amounts: the cost at the acquired day's FX rate, the proceeds at the
disposed day's rate, and dividends at the ex-date's rate. An amount whose
currency has no stored rate is left empty (never guessed)."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from stonks.fx import FxRates
from stonks.tax.lots import Disposal

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
