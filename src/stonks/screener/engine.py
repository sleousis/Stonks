"""Run a screen on the lake as it was on a date.

1. The candidates are what the universe rule keeps on the date
   (:func:`stonks.lab.universe.resolve`), narrowed to a stored universe's
   members when the spec names one. More than ``max_candidates`` stops the
   screen with :class:`TooManyCandidates` before any metric is read.
2. Each metric the spec uses is computed once
   (:class:`~stonks.screener.data.ScreenData`), with a progress step each.
3. Filters drop tickers outside their bounds or without a value, then the
   rows are sorted and cut to ``limit``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING

from pydantic import BaseModel

from stonks.lab.universe import resolve
from stonks.screener.data import ScreenData
from stonks.screener.spec import ScreenSpec

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake


#: ``progress(fraction, message)``: 0 to 1 as a screen runs. It may raise to
#: stop the screen (a cancelled job).
Progress = Callable[[float, str], None]


class TooManyCandidates(ValueError):
    """The rule kept more candidates than the cap allows."""

    def __init__(self, count: int, cap: int) -> None:
        self.count = count
        self.cap = cap
        super().__init__(
            f"the screen has {count:,} candidates, more than the cap of {cap:,}. "
            "Narrow it with sectors, exchanges, asset_classes, min_price, min_adv "
            "or a universe_id, or raise [screener] max_candidates"
        )


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
    #: Served from the short result cache (same spec and date).
    cached: bool = False


def candidates(lake: DuckDBLake, spec: ScreenSpec, as_of: date) -> list[str]:
    """What the rule (and the stored universe) keep on ``as_of``, before
    the metric filters."""
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
    tickers = candidates(lake, spec, as_of)
    if not spec.filters and spec.sort_by is None and spec.limit is None:
        return tickers
    kept = _matches(spec, ScreenData(lake, tickers, as_of), tickers)
    return kept[: spec.limit] if spec.limit is not None else kept


def run_screen(
    lake: DuckDBLake,
    spec: ScreenSpec,
    as_of: date,
    *,
    max_candidates: int | None = None,
    progress: Progress | None = None,
) -> ScreenResult:
    """The screen's rows on ``as_of`` with the value of each shown metric.
    ``KeyError`` when ``spec.universe_id`` names no stored universe,
    :class:`TooManyCandidates` when the rule keeps more than
    ``max_candidates``."""
    report = progress or _silent
    tickers = candidates(lake, spec, as_of)
    if max_candidates is not None and len(tickers) > max_candidates:
        raise TooManyCandidates(len(tickers), max_candidates)
    report(0.1, f"{len(tickers):,} candidates")
    data = ScreenData(lake, tickers, as_of)
    shown = spec.shown_metrics()
    values: dict[str, dict[str, float]] = {}
    for i, metric in enumerate(shown, start=1):
        values[metric] = data.metric(metric)
        report(0.1 + 0.8 * i / len(shown), f"metric {metric} ({i} of {len(shown)})")
    kept = _matches(spec, data, tickers)
    rows = kept[: spec.limit] if spec.limit is not None else kept
    info = _instrument_info(lake, rows)
    result = ScreenResult(
        as_of=as_of,
        candidates=len(tickers),
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
    report(1.0, f"{len(kept):,} matched")
    return result


def _silent(fraction: float, message: str) -> None:
    return None


def _instrument_info(lake: DuckDBLake, tickers: list[str]) -> dict[str, dict[str, str | None]]:
    if not tickers:
        return {}
    rows = lake.con.execute(
        "SELECT id, name, sector, exchange FROM instruments WHERE id = ANY(?)", [tickers]
    ).fetchall()
    return {r[0]: {"name": r[1], "sector": r[2], "exchange": r[3]} for r in rows}
