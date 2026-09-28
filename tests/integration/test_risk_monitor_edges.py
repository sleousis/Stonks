"""The live risk monitor at its edges: yesterday's book held names with no
prices, a forecast that had no VaR, the date filter of the snapshot list,
and an alert that cannot be sent."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.production.monitor_settings import RiskMonitorSettings
from stonks.production.risk_metrics import (
    PORTFOLIO_BOOK,
    BookState,
    RiskAlert,
    _publish,
    list_snapshots,
    score_book,
    write_snapshot,
)

PF = "pf_default"
DAY1, DAY2 = date(2026, 3, 16), date(2026, 3, 17)
SETTINGS = RiskMonitorSettings(window=20, min_window=1)


def closes(start: str, n: int, last: float) -> pd.Series:
    idx = pd.bdate_range(end=start, periods=n)
    values = np.linspace(100.0, last, n)
    return pd.Series(values, index=idx)


def book(day, exposures, value=10_000.0) -> BookState:
    return BookState(portfolio_id=PF, strategy_id=PORTFOLIO_BOOK, as_of=day, value=value,
                     exposures=exposures)  # fmt: skip


def test_names_without_prices_add_nothing_to_the_day_return(state):
    history = {"A.US": closes("2026-03-17", 40, 110.0), "LATE.US": closes("2026-03-17", 1, 50.0)}
    first = score_book(state, book(DAY1, {"A.US": 5000.0, "NOPX.US": 1000.0, "LATE.US": 500.0}),
                       history, SETTINGS)  # fmt: skip
    write_snapshot(state, first)
    second = score_book(state, book(DAY2, {"A.US": 5000.0}), history, SETTINGS)
    a = history["A.US"]
    expected = 5000.0 * (a.iloc[-1] / a.iloc[-2] - 1.0)  # only A.US moved the book
    assert second.pnl == pytest.approx(expected)
    assert second.realized_return == pytest.approx(expected / 10_000.0)


def test_a_day_after_a_forecast_without_var_is_not_scored(state):
    history = {"A.US": closes("2026-03-17", 40, 110.0)}
    first = score_book(state, book(DAY1, {"A.US": 5000.0}), history, SETTINGS)
    write_snapshot(state, replace(first, var_95=None, var_99=None))
    second = score_book(state, book(DAY2, {"A.US": 5000.0}), history, SETTINGS)
    assert second.realized_return is not None
    assert (second.violation_95, second.violation_99, second.window_days) == (None, None, 0)


def test_the_snapshot_list_filters_by_date(state):
    history = {"A.US": closes("2026-03-17", 40, 110.0)}
    for day in (DAY1, DAY2):
        write_snapshot(state, score_book(state, book(day, {"A.US": 5000.0}), history, SETTINGS))
    rows, total = list_snapshots(state, PF, since=DAY2)
    assert total == 1 and [r.as_of for r in rows] == [DAY2]


def test_an_alert_that_cannot_be_sent_is_logged(state):
    def broken(_event):
        raise RuntimeError("push down")

    _publish(state, RiskAlert("var_violations", PF, None, "too many"), DAY2, broken)
