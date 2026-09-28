"""Minute bars on real NYSE sessions and a toy strategy, for the decision
step and intraday backtest tests (roadmap 21.2.2)."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Order, Portfolio
from stonks.scheduling.calendar import get_calendar
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import BaseStrategy

#: Thursday and Friday, two regular NYSE sessions (13:30 to 20:00 UTC).
DAYS = (date(2026, 9, 24), date(2026, 9, 25))
TICKERS = ("AAA.US", "BBB.US")


def session_minutes(day: date) -> list[datetime]:
    """Naive UTC bar stamps of every minute of ``day``'s NYSE session."""
    s = get_calendar("XNYS").session(day)
    assert s is not None
    n = int((s.close - s.open).total_seconds() // 60)
    start = s.open.replace(tzinfo=None)
    return [start + timedelta(minutes=i) for i in range(n)]


def wave(ticker: str) -> Callable[[int], float]:
    """A close path per ticker: drift plus waves, so momentum flips often."""
    phase = 0.0 if ticker == TICKERS[0] else 1.3
    return lambda i: 100.0 + 0.01 * i + 2.0 * math.sin(i / 17.0 + phase) + math.sin(i / 5.0)


def minute_frame(tickers: Sequence[str] = TICKERS, days: Sequence[date] = DAYS) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for ticker in tickers:
        path = wave(ticker)
        i = 0
        prev: float | None = None
        for day in days:
            for stamp in session_minutes(day):
                close = round(path(i), 4)
                open_ = close if prev is None else prev
                rows.append(
                    {
                        "ticker": ticker,
                        "timestamp": stamp,
                        "open": open_,
                        "high": max(open_, close) + 0.05,
                        "low": min(open_, close) - 0.05,
                        "close": close,
                        "adj_close": close,
                        "volume": 1_000.0 + (i % 7) * 100,
                    }
                )
                prev = close
                i += 1
    return pd.DataFrame(rows)


def minute_lake(frame: pd.DataFrame | None = None, *, path: Path | None = None) -> DuckDBLake:
    lake = DuckDBLake(path or Path(":memory:"))
    lake.migrate()
    frame = minute_frame() if frame is None else frame
    lake.upsert_bars(frame, interval=Interval.MIN_1)
    for ticker in sorted(set(frame["ticker"])):
        lake.con.execute("INSERT INTO instruments (id, asset_class) VALUES (?, 'equity')", [ticker])
    return lake


class MinuteMomentum(BaseStrategy):
    """Holds the ticker with the best ``lookback``-minute return, if positive.

    It records every decision's view of the data in ``seen``: ``(as_of,
    last visible bar stamp)`` per ticker, for the look-ahead tests."""

    id = "minute_momentum"
    applicable_asset_classes = ("equity",)

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="lookback",
                kind="int",
                default=10,
                bounds=(1, 100),
                tunable=False,
                description="Minutes of return.",
            )
        ]

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(dict(params or {}))
        self.seen: list[tuple[datetime, str, datetime | None]] = []

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        bars = lake.get_bars(ticker, Interval.MIN_1, None, None)
        last = None if bars.empty else pd.Timestamp(bars["timestamp"].iloc[-1]).to_pydatetime()
        self.seen.append((as_of, ticker, last))
        n = int(self.params["lookback"])
        if len(bars) <= n:
            return None
        closes = bars["close"].astype(float).to_numpy()
        r = closes[-1] / closes[-1 - n] - 1.0
        return float(r) if r > 0 else None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        stamp = as_of.isoformat()
        top = my_picks[0][1] if my_picks else None
        out: list[Order] = []
        cash = portfolio.cash
        for ticker, held in sorted(portfolio.positions.items()):
            if held > 0 and ticker != top:
                out.append(Order(f"x:{stamp}:{ticker}", ticker, "sell", held))
                cash += held * prices.get(ticker, 0.0)
        if top is not None and portfolio.positions.get(top, 0.0) <= 0:
            price = prices.get(top)
            if price and cash > 0:
                out.append(Order(f"e:{stamp}:{top}", top, "buy", round(0.99 * cash / price, 6)))
        return out
