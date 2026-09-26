"""Shared helpers for the parallel-lab tests (BL-07): a random-walk lake and
module-level strategies / objectives that worker processes can import."""

from __future__ import annotations

import random
from datetime import date
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from stonks.core.params import ParameterSpec
from stonks.store.lake import DuckDBLake
from stonks.strategies.base import BaseStrategy


def random_walk_lake(
    path: Path | str,
    tickers: list[str],
    *,
    periods: int = 260,
    start: str = "2024-01-02",
    seed: int = 7,
) -> DuckDBLake:
    """A migrated lake (``path`` may be ``":memory:"``) with daily
    random-walk bars and an equity instrument row per ticker."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=periods)
    lake = DuckDBLake(Path(path))
    lake.migrate()
    frames = []
    for ticker in tickers:
        close = 50.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, len(dates))))
        frames.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": [d.date() for d in dates],
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "adj_close": close,
                    "volume": 1_000_000,
                }
            )
        )
    lake.upsert_prices(pd.concat(frames, ignore_index=True))
    for ticker in tickers:
        lake.con.execute("INSERT INTO instruments (id, asset_class) VALUES (?, 'equity')", [ticker])
    return lake


def lake_dates(lake: DuckDBLake) -> tuple[date, date]:
    row = lake.sql("SELECT MIN(date) AS lo, MAX(date) AS hi FROM prices").iloc[0]
    return pd.Timestamp(row["lo"]).date(), pd.Timestamp(row["hi"]).date()


class NoisyParamStrategy(BaseStrategy):
    """No trading; exists so objectives can score params without a lake.
    ``fail_on`` makes chosen ``a`` values raise in ``fit``."""

    id = "noisy_param_fake"

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(name="a", kind="int", default=0, bounds=(0, 9)),
            ParameterSpec(name="b", kind="float", default=0.0, bounds=(0.0, 1.0)),
            ParameterSpec(name="fail_on", kind="int", default=-1, bounds=(-1, 9), tunable=False),
        ]

    def fit(self, dataset) -> None:
        if self.params["a"] == self.params["fail_on"]:
            raise RuntimeError(f"a={self.params['a']} fails")

    def estimate_return(self, ticker, as_of, lake):  # pragma: no cover - unused
        return None

    def decide(self, my_picks, portfolio, prices, as_of):  # pragma: no cover - unused
        return []


class GlobalRngObjective:
    """Score = params plus a draw from the *global* RNGs: reproducible only
    if every trial's RNGs are seeded per task, whatever worker runs it."""

    name = "global_rng"
    direction: Literal["maximize", "minimize"] = "maximize"

    def score(self, strategy, dataset) -> float:
        p = strategy.params
        return p["a"] + p["b"] + random.random() + float(np.random.random())
