"""Shared helpers for the trade- and cost-robustness survival tests
(BL-17, BL-18, BL-19): trend lakes and module-level strategies/objectives
that worker processes can import."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from stonks.core.params import ParameterSpec
from stonks.core.types import Order, Portfolio
from stonks.lab.dataset import LabDataset
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import BaseStrategy


def trend_lake(
    path: Path | str,
    trends: Mapping[str, tuple[float, float]],
    *,
    periods: int = 260,
    start: str = "2024-01-02",
    seed: int = 7,
    asset_classes: Mapping[str, str] | None = None,
) -> DuckDBLake:
    """A migrated lake with daily bars per ticker: ``trends[ticker] =
    (daily drift, daily vol)`` of a log random walk from 50. Every ticker
    gets an instrument row (``asset_classes`` overrides ``equity``)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=periods)
    lake = DuckDBLake(Path(path))
    lake.migrate()
    frames = []
    for ticker, (drift, vol) in trends.items():
        close = 50.0 * np.exp(np.cumsum(drift + vol * rng.standard_normal(len(dates))))
        frames.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": [d.date() for d in dates],
                    "open": close,
                    "high": close * 1.005,
                    "low": close * 0.995,
                    "close": close,
                    "adj_close": close,
                    "volume": 1_000_000,
                }
            )
        )
    lake.upsert_prices(pd.concat(frames, ignore_index=True))
    classes = dict(asset_classes or {})
    for ticker in trends:
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


class LongAll(BaseStrategy):
    """Buys every pick once, equal weight of the cash, and holds."""

    id = "long_all_fake"

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(name="allocation", kind="float", default=1.0, bounds=(0.1, 1.0)),
            ParameterSpec(name="ticker", kind="categorical", default="", tunable=False),
        ]

    def estimate_return(self, ticker, as_of, lake):
        wanted = self.params.get("ticker", "")
        return 1.0 if not wanted or ticker == wanted else None

    def decide(self, my_picks, portfolio: Portfolio, prices, as_of) -> list[Order]:
        todo = [t for _, t in my_picks if portfolio.positions.get(t, 0.0) <= 0 and prices.get(t)]
        if not todo or portfolio.cash <= 0:
            return []
        if any(portfolio.positions.get(t, 0.0) > 0 for _, t in my_picks):
            return []
        budget = portfolio.cash * float(self.params.get("allocation", 1.0)) / len(todo)
        return [
            Order(
                client_id=f"{self.id}:{t}:{as_of.isoformat()}",
                ticker=t,
                side="buy",
                quantity=budget / prices[t],
                strategy_id=self.id,
            )
            for t in todo
        ]


class FlipFlop(BaseStrategy):
    """Holds every pick for ``hold_bars`` bars, then flat for as long, and
    so on: turnover rises as ``hold_bars`` falls."""

    id = "flip_flop_fake"

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(name="hold_bars", kind="int", default=1, bounds=(1, 40)),
            ParameterSpec(name="ticker", kind="categorical", default="", tunable=False),
        ]

    def __init__(self, params) -> None:
        super().__init__(params)
        self._bar = 0

    def estimate_return(self, ticker, as_of, lake):
        wanted = self.params["ticker"]
        return 1.0 if not wanted or ticker == wanted else None

    def decide(self, my_picks, portfolio: Portfolio, prices, as_of) -> list[Order]:
        hold = int(self.params["hold_bars"])
        phase_long = (self._bar // hold) % 2 == 0
        self._bar += 1
        held = {t: q for t, q in portfolio.positions.items() if q > 0}
        if not phase_long:
            return [
                Order(
                    client_id=f"{self.id}:{t}:sell:{as_of.isoformat()}",
                    ticker=t,
                    side="sell",
                    quantity=q,
                    strategy_id=self.id,
                )
                for t, q in sorted(held.items())
            ]
        todo = [t for _, t in my_picks if t not in held and prices.get(t)]
        if held or not todo or portfolio.cash <= 0:
            return []
        budget = portfolio.cash * 0.99 / len(todo)
        return [
            Order(
                client_id=f"{self.id}:{t}:buy:{as_of.isoformat()}",
                ticker=t,
                side="buy",
                quantity=budget / prices[t],
                strategy_id=self.id,
            )
            for t in todo
        ]

    def fit(self, dataset) -> None:
        self._bar = 0


class SurfaceStrategy(LongAll):
    """Buys and holds whatever its params (so its OOS Sharpe is the same
    for every neighbour); the params define a synthetic train-score
    surface (:class:`SurfaceObjective`). ``trade=False`` never trades;
    ``fragile=True`` refuses any params but a=10, b=0.5."""

    id = "surface_fake"

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(name="a", kind="int", default=10, bounds=(0, 20)),
            ParameterSpec(name="b", kind="float", default=0.5, bounds=(0.0, 1.0)),
            ParameterSpec(name="mode", kind="categorical", default="x", bounds=["x", "y"]),
            ParameterSpec(name="flag", kind="bool", default=False),
            ParameterSpec(name="shape", kind="categorical", default="smooth", tunable=False),
            ParameterSpec(name="trade", kind="bool", default=True, tunable=False),
            ParameterSpec(name="fragile", kind="bool", default=False, tunable=False),
        ]

    def __init__(self, params) -> None:
        super().__init__(params)
        p = self.params
        if p["fragile"] and not (p["a"] == 10 and abs(p["b"] - 0.5) < 1e-9):
            raise ValueError("fragile surface: only a=10, b=0.5 builds")

    def estimate_return(self, ticker, as_of, lake):
        return 1.0 if self.params["trade"] else None


class SurfaceObjective:
    """``smooth``: a wide hill peaking at a=10, b=0.5. ``spike``: 1.0 at
    exactly that point, 0.05 anywhere else. ``negative``: always -1."""

    name = "surface"
    direction: Literal["maximize", "minimize"] = "maximize"

    def score(self, strategy, dataset) -> float:
        p = strategy.params
        a, b, shape = float(p["a"]), float(p["b"]), p["shape"]
        if shape == "negative":
            return -1.0
        if shape == "spike":
            return 1.0 if a == 10 and abs(b - 0.5) < 1e-9 else 0.05
        return 1.0 - ((a - 10.0) / 40.0) ** 2 - ((b - 0.5) / 2.0) ** 2


class PeakTrader(LongAll):
    """Buys and holds every pick only when ``a`` is exactly 10: a pure
    peak in parameter space, in and out of sample."""

    id = "peak_trader_fake"

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(name="a", kind="int", default=10, bounds=(0, 20)),
            *super().parameter_spec(),
        ]

    def estimate_return(self, ticker, as_of, lake):
        return 1.0 if int(self.params["a"]) == 10 else None


class FixedTuner:
    """A tuner stand-in: plateau/cross-instrument tests only read its seed."""

    seed = 5

    def tune(self, **kwargs):  # pragma: no cover - never called
        raise NotImplementedError
