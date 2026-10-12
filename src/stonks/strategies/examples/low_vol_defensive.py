"""LowVolDefensive: hold the calmest names (the low-volatility anomaly).

A named :class:`~stonks.strategies.examples.factor_strategy.FactorStrategy`
that always ranks on the ``low_vol_60`` library factor (60-bar volatility
of daily returns, direction -1, so the lowest values are held). Scoring,
the top slice and the monthly rebalance are the factor strategy's own.
Source: Ang, Hodrick, Xing and Zhang (2006), see ``stonks factors show
low_vol_60``.
"""

from __future__ import annotations

from stonks.strategies.examples.factor_strategy import FactorStrategy


class LowVolDefensive(FactorStrategy):
    id = "low_vol_defensive"
    title = "Calm stocks"
    summary = "Holds the calmest fifth of the universe by 60-day volatility, rebalanced each month."
    hypothesis = (
        "Leverage-constrained investors bid up risky stocks, so calmer stocks earn "
        "more per unit of risk (the low-volatility anomaly, Ang, Hodrick, Xing and "
        "Zhang). Holding the calmest names monthly earns that premium with smaller "
        "drawdowns. Fails in speculative rallies led by high-beta names."
    )
    alpha_family = "quality"
    premise = "none"
    fixed_factor = "low_vol_60"
    required_history_bars = 61
