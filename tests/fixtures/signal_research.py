"""Shared helpers for the signal-research tests (BL-33, BL-34, BL-35): a lake
with distinct opens and closes, and module-level strategies that worker
processes can import.

Every strategy here reads bars straight from the lake it is handed, so the
same class works on real, permuted and noise lakes. Some peek at future
bars on purpose (planted signals): they exist to check that the evaluation
lines scores up with the right forward returns.
"""

from __future__ import annotations

import zlib
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.params import ParameterSpec
from stonks.core.types import Order, Portfolio
from stonks.lab.dataset import LabDataset
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import BaseStrategy

ALL_CLASSES = ("equity", "crypto", "commodity", "bond")


def signal_lake(
    path: Path | str,
    tickers: Sequence[str],
    *,
    periods: int = 260,
    start: str = "2023-01-02",
    seed: int = 7,
    drift: float = 0.0003,
    vol: float = 0.012,
    gap_vol: float = 0.004,
    asset_classes: Mapping[str, str] | None = None,
    weekday_edge: Mapping[str, tuple[int, float]] | None = None,
) -> DuckDBLake:
    """Daily bars per ticker: ``open = prev close * exp(gap)``, ``close =
    open * exp(body)`` with independent normal gaps and bodies.
    ``weekday_edge[ticker] = (weekday, boost)`` adds ``boost`` to the body
    of every bar on that weekday (0 = Monday)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=periods)
    weekdays = np.array([d.weekday() for d in dates])
    lake = DuckDBLake(Path(path))
    lake.migrate()
    frames = []
    for ticker in tickers:
        gap = rng.normal(0.0, gap_vol, periods)
        body = rng.normal(drift, vol, periods)
        if weekday_edge and ticker in weekday_edge:
            day, boost = weekday_edge[ticker]
            body = body + np.where(weekdays == day, boost, 0.0)
        gap[0] = 0.0
        steps = np.empty(2 * periods)
        steps[0::2] = gap
        steps[1::2] = body
        path_ = np.log(50.0) + np.cumsum(steps)
        opens, closes = np.exp(path_[0::2]), np.exp(path_[1::2])
        frames.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": [d.date() for d in dates],
                    "open": opens,
                    "high": np.maximum(opens, closes) * 1.004,
                    "low": np.minimum(opens, closes) * 0.996,
                    "close": closes,
                    "adj_close": closes,
                    "volume": 1_000_000,
                }
            )
        )
    lake.upsert_prices(pd.concat(frames, ignore_index=True))
    classes = dict(asset_classes or {})
    for ticker in tickers:
        lake.con.execute(
            "INSERT INTO instruments (id, asset_class) VALUES (?, ?)",
            [ticker, classes.get(ticker, "equity")],
        )
    return lake


def dataset_for(lake: DuckDBLake, universe: Sequence[str], **kw: Any) -> LabDataset:
    row = lake.sql("SELECT MIN(date) AS lo, MAX(date) AS hi FROM prices").iloc[0]
    lo: date = pd.Timestamp(row["lo"]).date()
    hi: date = pd.Timestamp(row["hi"]).date()
    return LabDataset(lake=lake, universe=list(universe), start=lo, end=hi, **kw)


def stable_uniform(*parts: Any) -> float:
    """A uniform in [0, 1) from a process-independent hash of ``parts``."""
    return zlib.crc32("|".join(str(p) for p in parts).encode()) / 2**32


def _day(as_of: date | datetime) -> date:
    return as_of.date() if isinstance(as_of, datetime) else as_of


class _LakeBars(BaseStrategy):
    """Reads each ticker's daily opens / closes once per lake; holds every
    pick equal-weight (sells what drops out, buys new picks)."""

    applicable_asset_classes = ALL_CLASSES

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._lake: Any = None
        self._bars: dict[str, pd.DataFrame] = {}

    @classmethod
    def parameter_spec(cls):
        return []

    def bars(self, ticker: str, lake: Any) -> pd.DataFrame:
        if lake is not self._lake:
            self._lake, self._bars = lake, {}
        if ticker not in self._bars:
            frame = lake.sql(
                "SELECT timestamp, open, close FROM bars WHERE ticker = ? AND interval = '1d'"
                " ORDER BY timestamp",
                [ticker],
            )
            frame.index = pd.to_datetime(frame["timestamp"]).dt.date
            self._bars[ticker] = frame
        return self._bars[ticker]

    def position(self, ticker: str, as_of: Any, lake: Any) -> tuple[pd.DataFrame, int | None]:
        frame = self.bars(ticker, lake)
        day = _day(as_of)
        if day not in frame.index:
            return frame, None
        return frame, int(frame.index.get_loc(day))

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: date,
    ) -> list[Order]:
        picked = {t for _, t in my_picks}
        orders: list[Order] = []
        for ticker, qty in portfolio.positions.items():
            if qty > 0 and ticker not in picked:
                orders.append(self._order(ticker, "sell", qty, as_of))
        new = [t for t in sorted(picked) if portfolio.positions.get(t, 0.0) <= 0]
        if new and portfolio.cash > 0:
            budget = portfolio.cash / len(new)
            for ticker in new:
                price = prices.get(ticker)
                if price and price > 0:
                    orders.append(self._order(ticker, "buy", budget / price, as_of))
        return orders

    def _order(self, ticker: str, side: str, qty: float, as_of: Any) -> Order:
        return Order(
            client_id=f"{self.id}:{side}:{ticker}:{as_of}",
            ticker=ticker,
            side=side,  # type: ignore[arg-type]
            quantity=qty,
            order_type="market",
            strategy_id=self.id,
        )


class FutureReturnSignal(_LakeBars):
    """Planted look-ahead: the score IS the forward return from the next
    open over ``horizon`` bars (``open[t+1+h] / open[t+1] - 1``), minus
    ``threshold`` (so it is a pick only above the threshold)."""

    id = "future_return_fake"

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(name="horizon", kind="int", default=1, bounds=(1, 60)),
            ParameterSpec(name="threshold", kind="float", default=0.0, bounds=(0.0, 0.2)),
        ]

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        frame, i = self.position(ticker, as_of, lake)
        h = int(self.params["horizon"])
        if i is None or i + 1 + h >= len(frame):
            return None
        opens = frame["open"].to_numpy()
        return float(opens[i + 1 + h] / opens[i + 1] - 1.0) - float(self.params["threshold"])


class NextGapSignal(_LakeBars):
    """Peeks at the gap into the next bar (``open[t+1] / close[t] - 1``).
    Forward returns measured from the next open must not see it."""

    id = "next_gap_fake"

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        frame, i = self.position(ticker, as_of, lake)
        if i is None or i + 1 >= len(frame):
            return None
        return float(frame["open"].iloc[i + 1] / frame["close"].iloc[i] - 1.0)


class NoiseSignal(_LakeBars):
    """A score drawn from a stable hash of (seed, ticker, day): no edge."""

    id = "noise_signal_fake"

    @classmethod
    def parameter_spec(cls):
        return [ParameterSpec(name="seed", kind="int", default=0, bounds=(0, 1000))]

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        _, i = self.position(ticker, as_of, lake)
        if i is None:
            return None
        return stable_uniform(self.params["seed"], ticker, _day(as_of)) * 2.0 - 1.0


class StaticSignal(_LakeBars):
    """A fixed score per ticker: persistent, so its IC series is strongly
    autocorrelated at overlapping horizons."""

    id = "static_signal_fake"

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        _, i = self.position(ticker, as_of, lake)
        return None if i is None else stable_uniform("static", ticker)


class ClassSplitSignal(_LakeBars):
    """Planted 20-bar look-ahead above 3% for tickers starting with ``EQ``
    (entries precede big moves), noise for every other ticker."""

    id = "class_split_fake"

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        frame, i = self.position(ticker, as_of, lake)
        if i is None:
            return None
        if not ticker.startswith("EQ"):
            return stable_uniform("split", ticker, _day(as_of)) * 2.0 - 1.0
        if i + 21 >= len(frame):
            return None
        opens = frame["open"].to_numpy()
        return float(opens[i + 21] / opens[i + 1] - 1.0) - 0.03


class WeekdaySignal(_LakeBars):
    """Holds the universe through the bar that falls on ``weekday`` (the
    calendar is known in advance): an edge only when that weekday's bars
    carry one."""

    id = "weekday_fake"

    @classmethod
    def parameter_spec(cls):
        return [ParameterSpec(name="weekday", kind="int", default=0, bounds=(0, 4))]

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        frame, i = self.position(ticker, as_of, lake)
        if i is None:
            return None
        nxt = pd.Timestamp(_day(as_of)) + pd.offsets.BDay(1)
        return 1.0 if nxt.weekday() == int(self.params["weekday"]) else None
