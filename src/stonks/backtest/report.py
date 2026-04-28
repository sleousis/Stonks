"""Backtest performance report + metric computation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class BacktestReport:
    strategy_id: str
    equity_dates: list[date]
    equity_curve: list[float]
    final_return: float
    sharpe: float
    max_drawdown: float
    cagr: float
    #: Profit factor — sum of positive per-bar returns divided by the absolute
    #: sum of negative per-bar returns. ``inf`` when there are positive
    #: returns but no negative ones ("all upside") and ``0.0`` when there are
    #: no positive returns at all (either all-flat or all-loss).
    profit_factor: float = 0.0


def compute_report(
    strategy_id: str,
    equity_dates: Sequence[date],
    equity_curve: Sequence[float],
) -> BacktestReport:
    dates = list(equity_dates)
    curve = list(equity_curve)
    if not curve:
        return BacktestReport(
            strategy_id=strategy_id,
            equity_dates=dates,
            equity_curve=curve,
            final_return=0.0,
            sharpe=0.0,
            max_drawdown=0.0,
            cagr=0.0,
        )

    start = curve[0]
    end = curve[-1]
    final_return = end / start - 1.0 if start > 0 else 0.0

    returns = [curve[i] / curve[i - 1] - 1.0 for i in range(1, len(curve)) if curve[i - 1] > 0]
    mean = sum(returns) / len(returns) if returns else 0.0
    var = sum((r - mean) ** 2 for r in returns) / len(returns) if returns else 0.0
    std = math.sqrt(var)
    sharpe = (mean / std) * math.sqrt(252) if std > 0 else 0.0

    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            dd = (v - peak) / peak
            max_dd = min(max_dd, dd)

    # Use seconds so CAGR makes sense for intraday windows too. Below one
    # full year we just report the scaled annual equivalent.
    _SECONDS_PER_YEAR = 365.25 * 24 * 3600
    if len(dates) > 1:
        span = dates[-1] - dates[0]
        seconds = getattr(span, "total_seconds", lambda: span.days * 86400)()
        years = max(seconds / _SECONDS_PER_YEAR, 1 / _SECONDS_PER_YEAR)
    else:
        years = 1 / _SECONDS_PER_YEAR
    cagr = (end / start) ** (1 / years) - 1.0 if start > 0 and end > 0 else 0.0

    pos = sum(r for r in returns if r > 0)
    neg = abs(sum(r for r in returns if r < 0))
    if pos == 0:
        profit_factor = 0.0  # no gains at all — either flat or all-loss
    elif neg == 0:
        profit_factor = math.inf  # gains but zero losses: infinite PF
    else:
        profit_factor = pos / neg

    return BacktestReport(
        strategy_id=strategy_id,
        equity_dates=dates,
        equity_curve=curve,
        final_return=final_return,
        sharpe=sharpe,
        max_drawdown=max_dd,
        cagr=cagr,
        profit_factor=profit_factor,
    )
