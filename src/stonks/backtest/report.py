"""Backtest performance report + metric computation.

Annualization conventions
-------------------------
- Sharpe is annualized with ``sqrt(periods_per_year)``. The default is 252
  (daily bars). ``periods_per_year(interval, asset_classes)`` derives the
  factor from a bar ``Interval`` and the universe's trading calendar: 252
  sessions of 6.5 hours for exchange-traded classes, 365 days of 24 hours
  for crypto (``stonks.backtest.calendar``).
- CAGR is ``(end / start) ** (1 / years) - 1`` with ``years`` measured in
  wall-clock seconds, so it works for intraday windows. A total wipeout
  (``end <= 0``) reports ``-1.0``. When the annualized figure is too large
  to represent as a float (tiny intraday spans with any gain) it reports
  ``math.inf`` instead of raising ``OverflowError``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from stonks.backtest.calendar import calendar_for_universe
from stonks.backtest.corporate_actions import CorporateActionRecord
from stonks.core.interval import Interval
from stonks.core.types import AssetClass

_TRADING_DAYS_PER_YEAR = 252
# ``math.exp`` overflows just above 709.78.
_MAX_EXP = 709.0


def periods_per_year(
    interval: Interval, asset_classes: Iterable[AssetClass] = ("equity",)
) -> float:
    """Number of bars of ``interval`` in one year, for Sharpe annualization,
    on the trading calendar of ``asset_classes`` (see
    ``stonks.backtest.calendar``: crypto trades 24/7/365, every other class
    252 sessions of 6.5h; a mixed universe uses the densest calendar)."""
    return calendar_for_universe(asset_classes).periods_per_year(interval)


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
    #: Splits and dividends applied to held positions, in order; they
    #: explain equity-curve jumps (a dividend credits cash, a split changes
    #: the share count at unchanged value).
    corporate_actions: tuple[CorporateActionRecord, ...] = ()


def compute_report(
    strategy_id: str,
    equity_dates: Sequence[date],
    equity_curve: Sequence[float],
    periods_per_year: float = _TRADING_DAYS_PER_YEAR,
    corporate_actions: Iterable[CorporateActionRecord] = (),
) -> BacktestReport:
    dates = list(equity_dates)
    curve = list(equity_curve)
    events = tuple(corporate_actions)
    if not curve:
        return BacktestReport(
            strategy_id=strategy_id,
            equity_dates=dates,
            equity_curve=curve,
            final_return=0.0,
            sharpe=0.0,
            max_drawdown=0.0,
            cagr=0.0,
            corporate_actions=events,
        )

    start = curve[0]
    end = curve[-1]
    final_return = end / start - 1.0 if start > 0 else 0.0

    returns = [curve[i] / curve[i - 1] - 1.0 for i in range(1, len(curve)) if curve[i - 1] > 0]
    mean = sum(returns) / len(returns) if returns else 0.0
    var = sum((r - mean) ** 2 for r in returns) / len(returns) if returns else 0.0
    std = math.sqrt(var)
    sharpe = (mean / std) * math.sqrt(periods_per_year) if std > 0 else 0.0

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
    cagr = _cagr(start, end, years)

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
        corporate_actions=events,
    )


def _cagr(start: float, end: float, years: float) -> float:
    if start <= 0:
        return 0.0
    if end <= 0:
        return -1.0  # total wipeout
    exponent = math.log(end / start) / years
    if exponent > _MAX_EXP:
        return math.inf
    return math.exp(exponent) - 1.0
