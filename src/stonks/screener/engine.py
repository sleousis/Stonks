"""Run a screen on the lake as it was on a date.

1. The candidates are what the universe rule keeps on the date
   (:func:`stonks.lab.universe.resolve`), narrowed to a stored universe's
   members when the spec names one.
2. Each metric the spec uses is computed once
   (:class:`~stonks.screener.data.ScreenData`).
3. Filters drop tickers outside their bounds or without a value, then the
   rows are sorted and cut to ``limit``.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from pydantic import BaseModel

from stonks.lab.universe import resolve
from stonks.screener.data import ScreenData
from stonks.screener.spec import ScreenSpec

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake


class ScreenRow(BaseModel):
    ticker: str
    name: str | None = None
    sector: str | None = None
    exchange: str | None = None
    #: The value of each shown metric (``None``: no value on the date).
    values: dict[str, float | None]


class ScreenResult(BaseModel):
    as_of: date
    #: Tickers the rule (and the universe) kept before the metric filters.
    candidates: int
    #: Tickers that passed every filter, before ``limit``.
    matched: int
    #: ``limit`` cut the matches.
    truncated: bool
    metrics: list[str]
    rows: list[ScreenRow]


def _candidates(lake: DuckDBLake, spec: ScreenSpec, as_of: date) -> list[str]:
    tickers = resolve(lake, spec.rule(), as_of)
    if spec.universe_id is not None:
        members = set(resolve(lake, spec.universe_id, as_of))
        tickers = [t for t in tickers if t in members]
    return tickers


def _matches(spec: ScreenSpec, data: ScreenData, tickers: list[str]) -> list[str]:
    kept = tickers
    for f in spec.filters:
        values = data.metric(f.metric)
        kept = [
            t
            for t in kept
            if t in values
            and (f.min is None or values[t] >= f.min)
            and (f.max is None or values[t] <= f.max)
        ]
    if spec.sort_by is not None:
        values = data.metric(spec.sort_by)
        sign = -1.0 if spec.descending else 1.0
        # tickers without a value last, whatever the direction
        kept = sorted(kept, key=lambda t: (t not in values, sign * values.get(t, 0.0), t))
    return kept


def screen_tickers(lake: DuckDBLake, spec: ScreenSpec, as_of: date) -> list[str]:
    """The tickers a screen keeps on ``as_of``, in its order."""
    candidates = _candidates(lake, spec, as_of)
    if not spec.filters and spec.sort_by is None and spec.limit is None:
        return candidates
    kept = _matches(spec, ScreenData(lake, candidates, as_of), candidates)
    return kept[: spec.limit] if spec.limit is not None else kept


def run_screen(lake: DuckDBLake, spec: ScreenSpec, as_of: date) -> ScreenResult:
    """The screen's rows on ``as_of`` with the value of each shown metric.
    ``KeyError`` when ``spec.universe_id`` names no stored universe."""
    candidates = _candidates(lake, spec, as_of)
    data = ScreenData(lake, candidates, as_of)
    kept = _matches(spec, data, candidates)
    rows = kept[: spec.limit] if spec.limit is not None else kept
    shown = spec.shown_metrics()
    values = {m: data.metric(m) for m in shown}
    info = _instrument_info(lake, rows)
    return ScreenResult(
        as_of=as_of,
        candidates=len(candidates),
        matched=len(kept),
        truncated=len(rows) < len(kept),
        metrics=shown,
        rows=[
            ScreenRow(
                ticker=t,
                **info.get(t, {}),
                values={m: values[m].get(t) for m in shown},
            )
            for t in rows
        ],
    )


def _instrument_info(lake: DuckDBLake, tickers: list[str]) -> dict[str, dict[str, str | None]]:
    if not tickers:
        return {}
    rows = lake.con.execute(
        "SELECT id, name, sector, exchange FROM instruments WHERE id = ANY(?)", [tickers]
    ).fetchall()
    return {r[0]: {"name": r[1], "sector": r[2], "exchange": r[3]} for r in rows}
