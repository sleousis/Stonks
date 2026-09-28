"""Factors computed from annual statements, point in time (roadmap 23.13).

A :class:`StatementFactor` reads each ticker's annual balance sheet and
income statement through a :class:`~stonks.store.pit.PointInTimeLake` view,
so a filing counts from the day after it and each period reads the version
known then (P12). Each date is scored on its own, like the fundamentals
scores, so tear sheets pass their sampled dates.

The metrics here are the published definitions the library ports:

- :func:`asset_growth`: total assets over last year's total assets, minus one
  (Cooper, Gulen and Schill 2008);
- :func:`gross_profitability`: gross profit over total assets (Novy-Marx 2013);
- :func:`net_share_issuance`: the log change in shares outstanding over the
  fiscal year, net of splits (Pontiff and Woodgate 2008).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any

import pandas as pd

from stonks.factors.base import Provenance
from stonks.factors.library.fundamentals import FundamentalFactor

__all__ = [
    "StatementFactor",
    "asset_growth",
    "gross_profitability",
    "net_share_issuance",
]

#: An annual report older than this many days at the decision is stale.
MAX_REPORT_AGE_DAYS = 550
#: Days between two fiscal year ends that count as consecutive years.
_YEAR_GAP = (300, 430)

Metric = Callable[[Any, str, datetime], float | None]


def _annual(frame: pd.DataFrame | None, as_of: datetime) -> pd.DataFrame:
    """Annual rows, newest period first, the newest not stale."""
    if frame is None or frame.empty or "frequency" not in frame:
        return pd.DataFrame()
    rows = pd.DataFrame(frame.loc[frame["frequency"] == "A"]).copy()
    if rows.empty:
        return rows
    rows["period_end"] = pd.to_datetime(rows["period_end"])
    rows = pd.DataFrame(rows.sort_values(by="period_end", ascending=False)).reset_index(drop=True)
    newest = pd.Timestamp(rows["period_end"].iloc[0])
    age = (pd.Timestamp(as_of) - newest).days
    return rows if age <= MAX_REPORT_AGE_DAYS else pd.DataFrame()


def _number(row: pd.Series, column: str) -> float | None:
    if column not in row:
        return None
    raw: Any = row[column]
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _two_years(rows: pd.DataFrame) -> tuple[pd.Series, pd.Series] | None:
    if len(rows) < 2:
        return None
    gap = (rows["period_end"].iloc[0] - rows["period_end"].iloc[1]).days
    if not _YEAR_GAP[0] <= gap <= _YEAR_GAP[1]:
        return None
    return rows.iloc[0], rows.iloc[1]


def asset_growth(view: Any, ticker: str, as_of: datetime) -> float | None:
    pair = _two_years(_annual(view.get_balance_sheet(ticker), as_of))
    if pair is None:
        return None
    now, before = (_number(r, "total_assets") for r in pair)
    if now is None or before is None or before <= 0:
        return None
    return now / before - 1.0


def gross_profitability(view: Any, ticker: str, as_of: datetime) -> float | None:
    income = _annual(view.get_income_statement(ticker), as_of)
    balance = _annual(view.get_balance_sheet(ticker), as_of)
    if income.empty or balance.empty:
        return None
    latest = income.iloc[0]
    same = balance[balance["period_end"] == latest["period_end"]]
    if same.empty:
        return None
    gross = _number(latest, "gross_profit")
    if gross is None:
        revenue, cost = _number(latest, "revenue"), _number(latest, "cost_of_revenue")
        gross = revenue - cost if revenue is not None and cost is not None else None
    assets = _number(same.iloc[0], "total_assets")
    if gross is None or assets is None or assets <= 0:
        return None
    return gross / assets


def _split_factor(view: Any, ticker: str, start: date, end: date) -> float:
    """New shares per old share from splits with an ex date in ``(start, end]``."""
    reader = getattr(view, "get_corporate_actions", None)
    if not callable(reader):
        return 1.0
    actions: Any = reader([ticker])
    if not isinstance(actions, pd.DataFrame) or actions.empty:
        return 1.0
    factor = 1.0
    for kind, ex_date, value in zip(
        actions["kind"], actions["ex_date"], actions["value"], strict=True
    ):
        ex = _as_day(ex_date)
        ratio = float(value)
        if kind == "split" and ex is not None and start < ex <= end and ratio > 0:
            factor *= ratio
    return factor


def _as_day(value: Any) -> date | None:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        return None
    return date.fromisoformat(str(stamp)[:10])


def net_share_issuance(view: Any, ticker: str, as_of: datetime) -> float | None:
    pair = _two_years(_annual(view.get_balance_sheet(ticker), as_of))
    if pair is None:
        return None
    column = "common_stock_shares_outstanding"
    now, before = (_number(r, column) for r in pair)
    if now is None or before is None or now <= 0 or before <= 0:
        return None
    start, end = _as_day(pair[1]["period_end"]), _as_day(pair[0]["period_end"])
    if start is None or end is None:
        return None
    splits = _split_factor(view, ticker, start, end)
    return math.log(now / (before * splits))


class StatementFactor(FundamentalFactor):
    """A factor computed by ``metric(view, ticker, as_of)`` from annual
    statements read point in time (module doc)."""

    def __init__(
        self,
        id: str,
        metric: Metric,
        *,
        description: str,
        family: str,
        hypothesis: str,
        direction: int = 1,
        provenance: Provenance | None = None,
        tables: Sequence[str] = ("income_statement", "balance_sheet"),
    ) -> None:
        super().__init__(
            id,
            id,
            description=description,
            family=family,
            direction=direction,
            hypothesis=hypothesis,
            scorer=lambda: None,
            provenance=provenance,
        )
        self._metric = metric
        self.tables = tuple(tables)

    def _values(self, scorer: Any, view: Any, tickers: Sequence[str], as_of: datetime) -> dict:
        out: dict[str, float] = {}
        for ticker in tickers:
            value = self._metric(view, ticker, as_of)
            if value is not None and math.isfinite(value):
                out[ticker] = float(value)
        return out
