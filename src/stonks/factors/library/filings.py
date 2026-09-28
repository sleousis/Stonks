"""Factors from regulatory filings (roadmap 23.13), read point in time.

``insider_net_buying`` reads ``insider_transactions`` through a
:class:`~stonks.store.pit.PointInTimeLake` view: a trade counts from the
acceptance time of the Form 4 that reported it (``stonks ingest edgar``),
or, for a vendor row without one, from the day after its filing date.
Only open-market purchases (code ``P``) and sales (code ``S``) count:
grants, option exercises and tax withholding carry no view.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from stonks.factors.base import Factor, Provenance
from stonks.factors.statements import StatementFactor

#: Days of insider trades the net buying ratio looks back over.
INSIDER_WINDOW_DAYS = 180


def insider_net_buying(view: Any, ticker: str, as_of: datetime) -> float | None:
    """``(bought - sold) / (bought + sold)`` in dollars over the last
    :data:`INSIDER_WINDOW_DAYS` of trades, ``None`` without any."""
    reader = getattr(view, "get_insider_transactions", None)
    if not callable(reader):
        return None
    rows = reader(ticker)
    if rows is None or rows.empty:
        return None
    start = (as_of - timedelta(days=INSIDER_WINDOW_DAYS)).date()
    traded = pd.to_datetime(rows["transaction_date"]).dt.date
    recent = rows[(traded >= start) & rows["transaction_code"].isin(["P", "S"])]
    if recent.empty:
        return None
    value = pd.to_numeric(recent["value"], errors="coerce")
    fallback = pd.to_numeric(recent["shares"], errors="coerce") * pd.to_numeric(
        recent["price"], errors="coerce"
    )
    dollars = value.where(value.notna(), fallback).abs()
    bought = float(dollars[recent["transaction_code"] == "P"].sum())
    sold = float(dollars[recent["transaction_code"] == "S"].sum())
    total = bought + sold
    if not math.isfinite(total) or total <= 0:
        return None
    return (bought - sold) / total


def factors() -> list[Factor]:
    return [
        StatementFactor(
            "insider_net_buying",
            insider_net_buying,
            description="insider open-market buying minus selling over the last 180 days, "
            "as a share of both",
            family="insider",
            hypothesis=(
                "Insiders know their firm best and buy with their own money only when "
                "they expect gains, so net buying predicts returns. Selling says less: "
                "insiders sell to diversify or pay taxes. Fails in large firms, where "
                "insider trades carry little news."
            ),
            provenance=Provenance(
                "Lakonishok and Lee (2001), Are insider trades informative?, Review of "
                "Financial Studies 14(1)",
                published=2001,
                sample_start=1975,
                sample_end=1995,
                reported="firms with the most insider buying beat those with the most "
                "selling over the next year, mostly among small firms",
            ),
            tables=("insider_transactions",),
        )
    ]
