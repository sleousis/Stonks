"""Factor panels: evaluate an expression over a universe and window
(roadmap 22.2).

A *panel* is a date-by-ticker frame: one row per bar timestamp in the
window, one column per universe ticker, NaN where there is no value.

How a panel is built:

1. **Read** the universe's bars from ``start`` minus the formula's warm-up
   to ``end`` (never later, so even the label cannot read past the window
   end). A plain lake is read with one query; a point-in-time view
   (:class:`~stonks.store.pit.PointInTimeLake`) through its typed
   ``get_bars``, so a strategy's panel sees only what was known.
2. **Adjust** for splits and dividends with the same factors the rest of the
   system uses (:class:`~stonks.features.price_adjustment.SeriesAdjustment`,
   from corporate actions or the vendor's ``adj_close``). Fields go in
   fully adjusted, and the SQL rescales each result to the units known on
   its date (:mod:`stonks.factors.sql`).
3. **Mark membership**: with a stored universe's point-in-time spans a
   ticker counts only on dates it was a member. History outside the spans
   still feeds rolling windows (it was known), but ``CSRank`` and the
   output skip it.
4. **Evaluate** the compiled SQL in a private in-memory DuckDB and pivot.

:class:`FactorEngine` adds the panel cache (:mod:`stonks.factors.cache`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.timeutil import day_end, day_start
from stonks.factors.expression import Node, lookback
from stonks.factors.sql import compile_sql
from stonks.features.price_adjustment import SeriesAdjustment
from stonks.logging import get_logger
from stonks.store.corporate_actions import LakeCorporateActions
from stonks.store.pit import PointInTimeLake

__all__ = [
    "PanelRequest",
    "evaluate",
    "membership_frame",
    "panel_from_lake",
    "prepare_bars",
    "read_bars",
    "warmup_days",
]

_log = get_logger("stonks.factors.engine")

_BAR_COLUMNS = ["ticker", "timestamp", "open", "high", "low", "close", "adj_close", "volume"]
_FIELDS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class PanelRequest:
    """What to evaluate over: tickers, a window and an interval.

    ``membership`` (``ticker, start_date, end_date`` spans, ``end_date``
    empty for open) limits each ticker to the dates it was a member; without
    it every ticker counts on every date. ``universe_id`` names the stored
    universe the spans came from (for cache keys and reports)."""

    universe: tuple[str, ...]
    start: date
    end: date
    interval: Interval = Interval.DAY_1
    universe_id: str | None = None
    membership: pd.DataFrame | None = field(default=None, compare=False, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "universe", tuple(dict.fromkeys(self.universe)))
        if not self.universe:
            raise ValueError("the universe is empty")
        if self.start > self.end:
            raise ValueError("start must not be after end")


def warmup_days(bars: int, interval: Interval) -> int:
    """Calendar days that cover ``bars`` bars of ``interval`` with room for
    weekends and holidays."""
    if bars <= 0:
        return 0
    step = interval.to_timedelta()
    if interval.is_intraday:
        sessions = math.ceil(bars * step / timedelta(hours=6))
        return int(sessions * 1.5) + 10
    return int(bars * step.days * 1.5) + 10


# ---- reading --------------------------------------------------------------------------


def read_bars(
    lake: Any, tickers: Sequence[str], interval: Interval, start: datetime, end: datetime
) -> pd.DataFrame:
    """Raw bars of ``tickers`` with ``start <= timestamp <= end``. A
    point-in-time view clamps ``end`` to what it has seen."""
    tickers = list(tickers)
    if isinstance(lake, PointInTimeLake) or not callable(getattr(lake, "sql", None)):
        frames = []
        for ticker in tickers:
            got = lake.get_bars(ticker, interval, start, end)
            if got is not None and not got.empty:
                got = got.copy()
                got["ticker"] = ticker
                frames.append(got)
        if not frames:
            return pd.DataFrame(columns=_BAR_COLUMNS)
        out = pd.concat(frames, ignore_index=True)
        for col in _BAR_COLUMNS:
            if col not in out.columns:
                out[col] = np.nan
        return pd.DataFrame(out[_BAR_COLUMNS])
    return lake.sql(
        f"SELECT {', '.join(_BAR_COLUMNS)} FROM bars "
        "WHERE ticker = ANY(?) AND interval = ? AND timestamp BETWEEN ? AND ? "
        "ORDER BY ticker, timestamp",
        [tickers, interval.code, start, end],
    )


def _membership_mask(
    bars: pd.DataFrame, membership: pd.DataFrame | None, tickers: Sequence[str]
) -> np.ndarray:
    """True where the row's ticker was a member on the row's date."""
    if membership is None:
        return np.ones(len(bars), dtype=bool)
    days = pd.to_datetime(bars["timestamp"]).dt.normalize()
    mask = np.zeros(len(bars), dtype=bool)
    wanted = set(tickers)
    for span in membership.itertuples(index=False):
        if span.ticker not in wanted:
            continue
        lo = pd.Timestamp(span.start_date)
        hi = pd.Timestamp(span.end_date) if pd.notna(span.end_date) else pd.Timestamp.max
        rows = (bars["ticker"] == span.ticker).to_numpy() & (days >= lo).to_numpy()
        rows &= (days <= hi).to_numpy()
        mask |= rows
    return mask


def prepare_bars(
    bars: pd.DataFrame,
    actions: Any = None,
    membership: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """The engine's input relation (see :mod:`stonks.factors.sql`) from raw
    bars: fields in fully adjusted units, ``pf``, ``vf`` and ``member``.
    ``actions`` is a :class:`~stonks.core.corporate_actions.CorporateActions`
    (or ``None`` for the ``adj_close`` fallback)."""
    if bars.empty:
        out = pd.DataFrame(columns=["ticker", "timestamp", *_FIELDS, "volume", "pf", "vf"])
        out["member"] = pd.Series(dtype=bool)
        return out
    frames = []
    for ticker, rows in bars.groupby("ticker", sort=False):
        rows = rows.sort_values("timestamp").reset_index(drop=True)
        events = actions.for_ticker(str(ticker)) if actions is not None else []
        adj = SeriesAdjustment.build(rows, events)
        n = len(rows)
        pf = adj.price if adj.price is not None else np.ones(n)
        vf = adj.volume if adj.volume is not None else np.ones(n)
        frame = pd.DataFrame(
            {"ticker": str(ticker), "timestamp": pd.to_datetime(rows["timestamp"])}
        )
        for col in _FIELDS:
            frame[col] = rows[col].to_numpy(dtype=float) * pf
        frame["volume"] = rows["volume"].to_numpy(dtype=float) * vf
        frame["pf"] = np.asarray(pf, dtype=float)
        frame["vf"] = np.asarray(vf, dtype=float)
        frames.append(frame)
    out = pd.concat(frames, ignore_index=True)
    out["member"] = _membership_mask(out, membership, list(out["ticker"].unique()))
    return out


# ---- evaluating -----------------------------------------------------------------------


def evaluate(
    node: Node,
    prepared: pd.DataFrame,
    columns: Sequence[str],
    *,
    start: Any = None,
) -> pd.DataFrame:
    """The panel of ``node`` over a :func:`prepare_bars` frame: rows are the
    timestamps from ``start`` on (all when ``None``), columns ``columns``."""
    empty = pd.DataFrame(index=pd.DatetimeIndex([], name="timestamp"), columns=list(columns))
    if prepared.empty:
        return empty.astype(float)
    con = duckdb.connect()
    try:
        con.register("factor_bars", prepared)
        long = con.execute(compile_sql(node, "factor_bars")).fetchdf()
    finally:
        con.close()
    long["timestamp"] = pd.to_datetime(long["timestamp"])
    if start is not None:
        long = long[long["timestamp"] >= pd.Timestamp(start)]
    wide = long.pivot_table(
        index="timestamp", columns="ticker", values="value", aggfunc="first", dropna=False
    )
    stamps = pd.DatetimeIndex(sorted(prepared["timestamp"].unique()))
    if start is not None:
        stamps = stamps[stamps >= pd.Timestamp(start)]
    wide = wide.reindex(index=stamps, columns=list(columns)).astype(float)
    wide.index.name = "timestamp"
    wide.columns.name = None
    return wide


def panel_from_lake(node: Node, lake: Any, request: PanelRequest) -> pd.DataFrame:
    """The panel of ``node`` for ``request``, read from ``lake``. Nothing
    after ``request.end`` is read, so a label's future reads stop there."""
    warm = warmup_days(lookback(node), request.interval)
    lo = day_start(request.start - timedelta(days=warm))
    hi = day_end(request.end)
    raw = read_bars(lake, request.universe, request.interval, lo, hi)
    actions = LakeCorporateActions(lake).load(list(request.universe))
    prepared = prepare_bars(raw, actions, request.membership)
    _log.debug(
        "factor.panel", expression=str(node), tickers=len(request.universe), rows=len(prepared)
    )
    return evaluate(node, prepared, request.universe, start=day_start(request.start))


def membership_frame(spans: Mapping[str, Sequence[tuple[date, date | None]]]) -> pd.DataFrame:
    """A ``membership`` frame from ``{ticker: [(start, end), ...]}``."""
    rows = [
        {"ticker": t, "start_date": lo, "end_date": hi}
        for t, ranges in spans.items()
        for lo, hi in ranges
    ]
    return pd.DataFrame(rows, columns=["ticker", "start_date", "end_date"])
