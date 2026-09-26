"""Moving-average crossover: long while the fast SMA is above the slow SMA.

Source: neurotrader888/mcpt, ``moving_average.py`` (MIT License, (c) 2023
neurotrader888), the baseline strategy of the Monte Carlo permutation test
videos. Stonks' own implementation; no code is copied.

Rule at bar ``t``: ``SMA(close, fast)`` and ``SMA(close, slow)`` over the
bars up to and including ``t``; long when fast > slow, flat otherwise.

Deliberate deviations from the original:

- ``fast < slow`` is enforced at construction (the original hard-codes
  10/30 and never checks).
- ``estimate_return`` is ``fast / slow - 1``, the size of the gap, as the
  magnitude proxy the Ranker orders picks by.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from stonks.core.params import ParameterSpec
from stonks.strategies._common import BarCache
from stonks.strategies.examples._nt888_base import SingleTickerLongFlat, common_specs


class MACrossoverStrategy(SingleTickerLongFlat):
    id = "ma_crossover"
    hypothesis = (
        "A fast average above a slow one marks a trend that tends to "
        "persist, since investors adjust slowly to news. Fails in "
        "sideways markets, where crossovers whipsaw."
    )
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = 20

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        fast, slow = int(self.params["fast"]), int(self.params["slow"])
        if fast >= slow:
            raise ValueError(f"fast ({fast}) must be smaller than slow ({slow})")

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="fast",
                kind="int",
                default=10,
                bounds=(2, 50),
                description="Fast SMA length in bars; must be smaller than slow.",
            ),
            ParameterSpec(
                name="slow",
                kind="int",
                default=30,
                bounds=(10, 200),
                description="Slow SMA length in bars.",
            ),
            *common_specs("BTC-USD.CC"),
        ]

    def _evaluate(self, cache: BarCache, ticker: str, as_of: Any) -> dict[str, float] | None:
        fast, slow = int(self.params["fast"]), int(self.params["slow"])
        closes = cache.last_n_closes(ticker, self.interval, as_of, slow)
        if len(closes) < slow or not np.isfinite(closes).all():
            return None
        fast_ma = float(closes[-fast:].mean())
        slow_ma = float(closes.mean())
        long = slow_ma > 0 and fast_ma > slow_ma
        return {
            "fast_ma": fast_ma,
            "slow_ma": slow_ma,
            "close": float(closes[-1]),
            "signal": 1.0 if long else 0.0,
            "score": fast_ma / slow_ma - 1.0 if slow_ma > 0 else 0.0,
        }
