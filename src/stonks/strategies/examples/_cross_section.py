"""Shared plumbing for cross-sectional (universe-ranking) strategies.

The engine and the ranker ask ``estimate_return`` one ticker at a time,
but a cross-sectional rule (top decile, top 20%) needs the whole universe
at once. :class:`CrossSectionMemo` computes the cross section on the first
ask of a ``(lake, as_of)`` and answers the rest from memory, so every
ticker gets the same, order-independent answer and ``decide`` (which may
run on a fresh instance in production) only needs the picks.

The universe is the strategy's ``universe`` param (comma-separated
tickers) or, when that is empty, every ticker with daily bars in the lake
whose asset class (when known) the strategy applies to.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from typing import Any

from stonks.core.interval import Interval
from stonks.core.types import Order
from stonks.logging import get_logger
from stonks.portfolio.base import ConstructionInput, PortfolioConstructor
from stonks.portfolio.orders import orders_from_targets
from stonks.strategies._common import BarCache, as_datetime

_log = get_logger("stonks.strategies.cross_section")

#: A last bar older than this (calendar days) means the name stopped
#: trading (delisted, halted): its frozen history must not rank.
MAX_STALENESS_DAYS = 10


def session_cutoff(as_of: Any) -> tuple[date, datetime]:
    """``as_of``'s calendar day and the decision time to hand the bar
    cache. The cache's "as of" readers show only daily bars complete at that
    decision (``strategies._common.visible_cutoff``, RS-03): the day's own
    bar at a daily decision, the previous session's mid-session."""
    at = as_datetime(as_of)
    return at.date(), at


def parse_universe(spec: str) -> list[str]:
    return list(dict.fromkeys(t.strip() for t in spec.split(",") if t.strip()))


def lake_universe(lake: Any, asset_classes: Iterable[str]) -> list[str]:
    """Tickers with daily bars in ``lake``, minus those whose known asset
    class is outside ``asset_classes`` (unknown classes are kept)."""
    reader: Any = getattr(lake, "bar_tickers", None)
    if callable(reader):  # a typed read, so a point-in-time lake allows it (BL-49)
        names: Any = reader(Interval.DAY_1)
        tickers = [str(t) for t in names]
    else:
        rows = lake.sql(
            "SELECT DISTINCT ticker FROM bars WHERE interval = ? ORDER BY ticker",
            [Interval.DAY_1.code],
        )
        tickers = [str(t) for t in rows["ticker"]]
    known = lake.get_asset_classes(tickers) if tickers else {}
    allowed = set(asset_classes)
    return [t for t in tickers if t not in known or known[t] in allowed]


def is_fresh(cache: BarCache, ticker: str, cutoff: datetime) -> bool:
    """True when ``ticker``'s latest bar as of ``cutoff`` is recent."""
    last = cache.last_close(ticker, Interval.DAY_1, cutoff)
    return last is not None and cutoff - last[0] <= timedelta(days=MAX_STALENESS_DAYS + 1)


class CrossSectionMemo:
    """Per-instance memo of one cross section per ``(lake, as_of day)``.

    Keeps only the latest day per lake (backtests move forward) and holds
    lakes weakly, so the lab's permuted lakes never share results."""

    def __init__(self) -> None:
        self._by_lake: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()

    def get(self, lake: Any, day: date, compute: Callable[[], Any]) -> Any:
        try:
            slot = self._by_lake.setdefault(lake, {})
        except TypeError:  # a lake that can't be weakly referenced
            return compute()
        if slot.get("day") != day:
            slot["value"], slot["day"] = compute(), day
        return slot["value"]

    def universe(self, lake: Any, compute: Callable[[], list[str]]) -> list[str]:
        try:
            slot = self._by_lake.setdefault(lake, {})
        except TypeError:
            return compute()
        if "universe" not in slot:
            slot["universe"] = compute()
        return slot["universe"]


def orders_from_constructor(
    constructor: PortfolioConstructor,
    inp: ConstructionInput,
    *,
    strategy_id: str,
    buffer_fraction: float = 0.0,
    allow_short: bool = False,
) -> list[Order]:
    """Size ``inp`` with ``constructor`` and diff the target book into
    orders (sells first; buys never spend more than cash plus proceeds).
    ``allow_short`` diffs signed targets (roadmap 16.3)."""
    book = constructor.target_weights(inp)
    if book.meta.get("unfunded"):
        _log.info("strategy.unfunded", strategy_id=strategy_id, tickers=book.meta["unfunded"])
    return orders_from_targets(
        book.weights,
        inp.portfolio,
        inp.prices,
        buffer_fraction=buffer_fraction,
        as_of=inp.as_of,
        strategy_id=strategy_id,
        allow_short=allow_short,
    )
