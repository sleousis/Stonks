"""Corporate-action provider over the lake's ``stock_splits`` and
``dividends`` tables (one query per ``load``)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd

from stonks.core.corporate_actions import (
    CorporateAction,
    CorporateActions,
    Dividend,
    Split,
)


class LakeCorporateActions:
    """:class:`~stonks.core.corporate_actions.CorporateActionsProvider`
    reading the lake. A lake without ``get_corporate_actions`` (a test
    double, a bars-only store) yields no events."""

    def __init__(self, lake: Any) -> None:
        self._lake = lake

    def load(self, tickers: Sequence[str]) -> CorporateActions:
        reader = getattr(self._lake, "get_corporate_actions", None)
        if reader is None or not tickers:
            return CorporateActions()
        return actions_from_frame(reader(list(tickers)))


def actions_from_frame(df: pd.DataFrame | None) -> CorporateActions:
    """Typed events from a ``ticker, ex_date, kind, value`` frame, with an
    optional ``declaration_date`` column for dividends."""
    if df is None or df.empty:
        return CorporateActions()
    events: list[CorporateAction] = []
    for row in df.itertuples(index=False):
        ex_date = pd.Timestamp(row.ex_date).date()
        value = float(row.value)
        if row.kind == "split" and value > 0 and value != 1.0:
            events.append(Split(row.ticker, ex_date, value))
        elif row.kind == "dividend" and value > 0:
            declared = getattr(row, "declaration_date", None)
            declared_on = None if pd.isna(declared) else pd.Timestamp(declared).date()
            events.append(Dividend(row.ticker, ex_date, value, declared_on=declared_on))
    return CorporateActions.from_events(events)
