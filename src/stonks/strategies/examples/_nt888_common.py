"""Helpers shared by the neurotrader888 ML strategy ports (PIP miner,
trendline meta-label): the train-window bar fetch and a long-only,
single-ticker ``decide``."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

import pandas as pd

from stonks.core.interval import Interval
from stonks.strategies._common import (  # noqa: F401 - re-exported
    BarCache,
    LakeBarCaches,
    as_datetime,
    long_only_decide,
)


def train_bars(
    dataset: Any,
    ticker: str,
    interval: Interval,
    *,
    caches: LakeBarCaches | None = None,
) -> pd.DataFrame:
    """Bars of ``ticker`` inside ``dataset.train_window`` only, oldest first,
    back-adjusted for splits and dividends as of the last training bar
    (the ``adjusted`` basis every signal read uses), so a split inside the
    window is not a fake crash.

    A plain-date ``train_end`` covers that whole day (the validation window
    starts the next day), so intraday bars of the last training day count.
    Nothing after ``train_end`` is ever read and no event after it adjusts
    the result: fitting cannot leak. Reads go through the strategy's
    ``caches`` when given (one history fetch shared with its predictions),
    else a one-off :class:`BarCache`."""
    start, end = dataset.train_window
    end_dt = (
        datetime.combine(end, time.max)
        if isinstance(end, date) and not isinstance(end, datetime)
        else as_datetime(end)
    )
    lake = dataset.lake
    cache = caches.for_lake(lake) if caches is not None else BarCache(lake)
    bars = cache.bars_between(ticker, interval, as_datetime(start), end_dt, basis="adjusted")
    if bars is None or bars.empty:
        raise ValueError(f"no bars for {ticker!r} in the training window")
    return bars.reset_index(drop=True)
