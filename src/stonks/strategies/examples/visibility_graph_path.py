"""Visibility-graph path-length strategy.

Source: neurotrader888/TimeSeriesVisibilityGraphs, ``network_indicators.py``
(MIT License, (c) 2023 neurotrader888). Stonks' own implementation; no code
is copied. Graph helpers live in :mod:`stonks.features.visibility_graph`.

Rule at bar ``t``: build the natural visibility graph of the last
``lookback`` closes (bars ``t-lookback+1 .. t``) and of their negation, and
measure the average shortest path length of each (``path_pos`` /
``path_neg``). Long when ``path_pos > path_neg``, flat otherwise. The
original goes short when ``path_pos < path_neg``; the broker is long-only,
so that becomes flat.

Deliberate deviations from the original:

- No ``ts2vg`` / ``networkx``: the graphs and BFS path lengths are our own.
- Short -> flat (long-only broker).
- ``estimate_return`` is ``path_pos - path_neg`` (> 0 while long), the
  signal's own magnitude, used by the Ranker to order picks. It is a graph
  statistic, not a forecast return.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from stonks.core.params import ParameterSpec
from stonks.features.visibility_graph import (
    average_shortest_path_length,
    natural_visibility_graph,
)
from stonks.strategies._common import BarCache
from stonks.strategies.examples._nt888_base import SingleTickerLongFlat, common_specs


class VisibilityGraphPathStrategy(SingleTickerLongFlat):
    id = "visibility_graph_path"
    hypothesis = (
        "The visibility graph of recent closes tells a smooth rise from a "
        "choppy one. A rise that is easier to see through tends to go on. "
        "Weak evidence, it fails in noisy markets."
    )
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = 12
    required_history_bars = 12

    def param_metadata(self) -> dict[str, int]:
        p = self.params
        return {
            "required_history_bars": int(p["lookback"]),
        }

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="lookback",
                kind="int",
                default=12,
                bounds=(6, 48),
                description="Closes per visibility graph (bars, current one included).",
            ),
            *common_specs("BTC-USD.CC"),
        ]

    def _evaluate(self, cache: BarCache, ticker: str, as_of: Any) -> dict[str, float] | None:
        lookback = int(self.params["lookback"])
        closes = cache.last_n_closes(ticker, self.interval, as_of, lookback)
        if len(closes) < lookback or not np.isfinite(closes).all():
            return None
        path_pos = average_shortest_path_length(natural_visibility_graph(closes))
        path_neg = average_shortest_path_length(natural_visibility_graph(-closes))
        return {
            "path_pos": path_pos,
            "path_neg": path_neg,
            "close": float(closes[-1]),
            "signal": 1.0 if path_pos > path_neg else 0.0,
            "score": path_pos - path_neg,
        }
