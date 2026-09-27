"""Risk of a book: from its own value history, or from its holdings'
price returns (useful for a synced account with little history), plus
concentration and beta. Drawdown, VaR and ES reuse the backtest metrics."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np

from stonks.backtest.metrics import (
    bar_returns,
    drawdowns,
    expected_shortfall,
    max_drawdown,
    value_at_risk,
)
from stonks.insights.book import Book
from stonks.insights.models import Concentration, RiskStats

TRADING_DAYS = 252
#: Fewest aligned returns a beta is computed from.
MIN_BETA_OBSERVATIONS = 20


def _stats(returns: Sequence[float], curve: Sequence[float]) -> RiskStats:
    r = np.asarray(returns, dtype=float)
    dd = drawdowns(curve)
    return RiskStats(
        observations=int(r.size),
        volatility=float(np.std(r, ddof=1) * math.sqrt(TRADING_DAYS)),
        max_drawdown=max_drawdown(curve),
        current_drawdown=dd[-1] if dd else 0.0,
        var_95=value_at_risk(r),
        expected_shortfall_95=expected_shortfall(r),
    )


def realized_risk(values: Sequence[float]) -> RiskStats | None:
    """Risk of a value history (one value per day). ``None`` with fewer
    than two daily returns."""
    returns = bar_returns(values)
    if len(returns) < 2:
        return None
    return _stats(returns, values)


def returns_risk(returns: Sequence[float]) -> RiskStats | None:
    """Risk of a daily return series (e.g. today's weights applied to past
    returns). ``None`` with fewer than two returns."""
    if len(returns) < 2:
        return None
    curve = [1.0]
    for r in returns:
        curve.append(curve[-1] * (1.0 + r))
    return _stats(returns, curve)


def weighted_returns(
    weights: Mapping[str, float], returns: Mapping[str, Sequence[float]]
) -> list[float]:
    """Daily returns of a book held at constant ``weights``. Every series in
    ``returns`` covers the same days (the caller aligns them); symbols
    missing from ``returns`` are left out."""
    series = [(w, returns[s]) for s, w in weights.items() if s in returns]
    if not series:
        return []
    n = min(len(r) for _, r in series)
    if n == 0:
        return []
    out = np.zeros(n)
    for w, r in series:
        out += w * np.asarray(r[-n:], dtype=float)
    return [float(x) for x in out]


def beta(asset: Sequence[float], benchmark: Sequence[float]) -> float | None:
    """Slope of ``asset`` on ``benchmark`` daily returns (same days).
    ``None`` with too few observations or a flat benchmark."""
    if len(asset) != len(benchmark):
        raise ValueError("asset and benchmark returns must cover the same days")
    if len(asset) < MIN_BETA_OBSERVATIONS:
        return None
    a = np.asarray(asset, dtype=float)
    b = np.asarray(benchmark, dtype=float)
    var = float(np.var(b, ddof=1))
    if var == 0.0:
        return None
    return float(np.cov(a, b, ddof=1)[0, 1] / var)


def concentration(book: Book) -> Concentration:
    """How much rides on the largest holdings, by share of gross holdings."""
    gross = book.gross_invested
    weighted = sorted(
        ((abs(h.market_value or 0.0) / gross, h.symbol) for h in book.priced if gross),
        key=lambda x: (-x[0], x[1]),
    )
    if not weighted:
        return Concentration(
            holdings=0,
            largest=None,
            top_weight=None,
            top5_weight=None,
            hhi=None,
            effective_holdings=None,
        )
    hhi = sum(w * w for w, _ in weighted)
    return Concentration(
        holdings=len(weighted),
        largest=weighted[0][1],
        top_weight=weighted[0][0],
        top5_weight=sum(w for w, _ in weighted[:5]),
        hhi=hhi,
        effective_holdings=1.0 / hhi if hhi else None,
    )
