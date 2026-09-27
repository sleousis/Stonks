"""The screener (roadmap 20.8): filter instruments on price and fundamental
metrics, point in time, on top of the universe rule.

- :mod:`.spec`: :class:`ScreenSpec`, the rule's filters plus metric bounds,
  an order and a top N;
- :mod:`.metrics`: the :class:`ScreenMetric` seam, one class per metric,
  found by :mod:`.registry`;
- :mod:`.data`: the lake reads, once per screen and date (P12);
- :mod:`.engine`: :func:`run_screen` and :func:`screen_tickers`.

A ``rule`` universe takes the same fields, so a saved screen becomes a
point-in-time universe for the lab (:mod:`stonks.universes.providers.rule`).
"""

from stonks.screener.engine import ScreenResult, ScreenRow, run_screen, screen_tickers
from stonks.screener.metrics.base import ScreenMetric
from stonks.screener.registry import all_metrics, metric_for, metric_ids
from stonks.screener.spec import MetricFilter, ScreenSpec

__all__ = [
    "MetricFilter",
    "ScreenMetric",
    "ScreenResult",
    "ScreenRow",
    "ScreenSpec",
    "all_metrics",
    "metric_for",
    "metric_ids",
    "run_screen",
    "screen_tickers",
]
