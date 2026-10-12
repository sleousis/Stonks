"""Seasonality: hold the names that did best in the coming month a year ago.

A named :class:`~stonks.strategies.examples.factor_strategy.FactorStrategy`
that always ranks on the ``seasonality_12m`` library factor (the return of
the month one year before the coming month). Scoring, the top slice and the
monthly rebalance are the factor strategy's own. Source: Heston and Sadka
(2008), see ``stonks factors show seasonality_12m``.
"""

from __future__ import annotations

from stonks.strategies.examples.factor_strategy import FactorStrategy


class Seasonality(FactorStrategy):
    id = "seasonality"
    title = "Same month last year"
    summary = (
        "Holds the names that rose most in the coming calendar month a year ago, "
        "rebalanced each month."
    )
    hypothesis = (
        "Some stocks do well in the same calendar month year after year, so the "
        "return of the coming month a year ago predicts it (Heston and Sadka). "
        "Fails when the seasonal driver, such as a fiscal calendar or a flow, goes away."
    )
    alpha_family = "data_driven"
    premise = "none"
    fixed_factor = "seasonality_12m"
    required_history_bars = 253
