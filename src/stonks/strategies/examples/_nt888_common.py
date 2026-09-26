"""Helpers shared by the neurotrader888 ML strategy ports (PIP miner,
trendline meta-label): the train-window bar fetch and a long-only,
single-ticker ``decide``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Any

import pandas as pd

from stonks.core.interval import Interval
from stonks.core.types import Order, Portfolio
from stonks.strategies._common import BarCache, LakeBarCaches, as_datetime, iso


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


def long_only_decide(
    strategy_id: str,
    target: str,
    allocation: float,
    my_picks: Sequence[tuple[float, str]],
    portfolio: Portfolio,
    prices: Mapping[str, float],
    as_of: Any,
) -> list[Order]:
    """Buy ``allocation`` of cash when picked and flat; sell everything when
    not picked and holding."""
    price = prices.get(target)
    holding = portfolio.positions.get(target, 0.0)
    picked = any(t == target for _, t in my_picks)
    if picked and price and price > 0 and holding <= 0 and portfolio.cash > 0:
        qty = portfolio.cash * float(allocation) / price
        if qty <= 0:
            return []
        side, quantity = "buy", qty
    elif not picked and holding > 0:
        side, quantity = "sell", holding
    else:
        return []
    return [
        Order(
            client_id=f"{strategy_id}:{side}:{target}:{iso(as_of)}",
            ticker=target,
            side=side,
            quantity=quantity,
            order_type="market",
            strategy_id=strategy_id,
        )
    ]
