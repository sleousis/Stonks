"""Synthetic minute bars on real exchange sessions, for the intraday
strategy and lab tests (roadmap 21.3.1).

Stamps are naive UTC, one per minute from the regular open to the last
minute before the close, taken from the ticker's exchange calendar. A path
function maps the minute of the session (0 at the open) to a close.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.features.sessions import RegularSession, regular_session
from stonks.store.lake import DuckDBLake

PricePath = Callable[[int], float]


def session_of(ticker: str, day: date) -> RegularSession:
    """The regular session of ``ticker`` on ``day`` (it must be one)."""
    for hour in range(24):
        s = regular_session(ticker, datetime(day.year, day.month, day.day, hour, 30))
        if s is not None and s.day == day:
            return s
    raise ValueError(f"{ticker} has no session on {day}")


def session_minutes(ticker: str, day: date) -> list[datetime]:
    s = session_of(ticker, day)
    n = int((s.close - s.open).total_seconds() // 60)
    return [s.open + timedelta(minutes=i) for i in range(n)]


def minute_bar(ticker: str, day: date, minute: int) -> datetime:
    """The stamp of the bar ``minute`` minutes after ``day``'s open."""
    return session_of(ticker, day).open + timedelta(minutes=minute)


def bars_frame(
    ticker: str,
    stamps: list[datetime],
    closes: Iterable[float],
    *,
    volume: float | np.ndarray = 1_000.0,
    spread: float = 0.0002,
) -> pd.DataFrame:
    """OHLCV rows: open at the previous close, high and low ``spread``
    (a fraction) around the bar's range."""
    close = np.asarray(list(closes), dtype=float)
    opens = np.concatenate([[close[0]], close[:-1]])
    vol = np.broadcast_to(np.asarray(volume, dtype=float), close.shape)
    return pd.DataFrame(
        {
            "ticker": ticker,
            "timestamp": stamps,
            "open": opens,
            "high": np.maximum(opens, close) * (1 + spread),
            "low": np.minimum(opens, close) * (1 - spread),
            "close": close,
            "adj_close": close,
            "volume": vol,
        }
    )


def session_frame(
    ticker: str, day: date, path: PricePath, *, volume: float | np.ndarray = 1_000.0
) -> pd.DataFrame:
    """One whole session of ``ticker`` on ``day`` following ``path``."""
    stamps = session_minutes(ticker, day)
    return bars_frame(ticker, stamps, [path(i) for i in range(len(stamps))], volume=volume)


def minute_lake(
    frames: Iterable[pd.DataFrame],
    *,
    asset_classes: Mapping[str, str] | None = None,
    path: Path | None = None,
) -> DuckDBLake:
    """A lake (in memory unless ``path``) holding ``frames`` as 1m bars."""
    lake = DuckDBLake(path or Path(":memory:"))
    lake.migrate()
    frame = pd.concat(list(frames), ignore_index=True)
    lake.upsert_bars(frame, interval=Interval.MIN_1)
    for ticker in sorted(set(frame["ticker"])):
        cls = (asset_classes or {}).get(ticker, "equity")
        lake.con.execute("INSERT INTO instruments (id, asset_class) VALUES (?, ?)", [ticker, cls])
    return lake
