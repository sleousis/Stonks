"""Backtest performance report.

Every metric is a pure function in ``stonks.backtest.metrics``; the trade
ledger and trade statistics come from ``stonks.backtest.trades`` and are
attached by ``with_trades`` (the lab does this for every backtest).

Annualization conventions
-------------------------
- Sharpe uses the sample standard deviation (ddof=1; before BL-03 it
  used the population one, overstating it by ``sqrt(n / (n - 1))``) and
  is annualized with ``sqrt(periods_per_year)``. The default is 252
  (daily bars). ``periods_per_year(interval, asset_classes)`` derives the
  factor from a bar ``Interval`` and the universe's trading calendar: 252
  sessions of 6.5 hours for exchange-traded classes, 365 days of 24 hours
  for crypto (``stonks.backtest.calendar``).
- CAGR is ``(end / start) ** (1 / years) - 1`` with ``years`` measured in
  wall-clock seconds, so it works for intraday windows. A total wipeout
  (``end <= 0``) reports ``-1.0``. When the annualized figure is too large
  to represent as a float (tiny intraday spans with any gain) it reports
  ``math.inf`` instead of raising ``OverflowError``.

Long/short books (roadmap 16.4)
-------------------------------
A backtest that allows shorts attaches a :class:`ShortBookReport` as
``short_book``: every financing charge (borrow fees, debit interest), the
orders the engine forced (margin calls, borrow recalls), the long and short
exposure after every bar as fractions of equity, and, once the trade
ledger is attached, the P&L of the long and the short round trips. A
long-only backtest leaves ``short_book`` as ``None``, so its report is
unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from stonks.backtest import metrics
from stonks.backtest.calendar import calendar_for_universe
from stonks.backtest.corporate_actions import CorporateActionRecord
from stonks.backtest.trades import RoundTrip, TradeStats
from stonks.core.interval import Interval
from stonks.core.types import AssetClass, Order

if TYPE_CHECKING:
    from stonks.backtest.simulated_broker import FinancingEvent

_TRADING_DAYS_PER_YEAR = 252


def periods_per_year(
    interval: Interval, asset_classes: Iterable[AssetClass] = ("equity",)
) -> float:
    """Number of bars of ``interval`` in one year, for Sharpe annualization,
    on the trading calendar of ``asset_classes`` (see
    ``stonks.backtest.calendar``: crypto trades 24/7/365, every other class
    252 sessions of 6.5h; a mixed universe uses the densest calendar)."""
    return calendar_for_universe(asset_classes).periods_per_year(interval)


@dataclass(frozen=True)
class ExposurePoint:
    """Long and short market value after one bar, as fractions of equity
    (``short`` is a positive magnitude)."""

    timestamp: date
    long: float
    short: float

    @property
    def gross(self) -> float:
        return self.long + self.short

    @property
    def net(self) -> float:
        return self.long - self.short


@dataclass(frozen=True)
class ShortBookReport:
    """What a long/short backtest adds to its report (see the module doc)."""

    financing: tuple[FinancingEvent, ...] = ()
    forced_orders: tuple[Order, ...] = ()
    exposure: tuple[ExposurePoint, ...] = ()
    #: P&L of the long and short round trips (open lots marked); set by
    #: ``stonks.backtest.trades.with_trades``.
    long_pnl: float = 0.0
    short_pnl: float = 0.0
    n_long_trades: int = 0
    n_short_trades: int = 0

    @property
    def borrow_fees(self) -> float:
        """Borrow fees paid (a positive cost)."""
        return -sum(e.amount for e in self.financing if e.kind == "borrow_fee")

    @property
    def debit_interest(self) -> float:
        """Interest paid on negative cash (a positive cost)."""
        return -sum(e.amount for e in self.financing if e.kind == "debit_interest")

    @property
    def financing_total(self) -> float:
        return self.borrow_fees + self.debit_interest

    @property
    def n_margin_calls(self) -> int:
        return sum(1 for o in self.forced_orders if o.strategy_id == "margin")

    @property
    def n_recalls(self) -> int:
        return sum(1 for o in self.forced_orders if o.strategy_id == "recall")

    @property
    def max_gross(self) -> float:
        return max((p.gross for p in self.exposure), default=0.0)

    @property
    def max_short(self) -> float:
        return max((p.short for p in self.exposure), default=0.0)

    @property
    def mean_net(self) -> float:
        return sum(p.net for p in self.exposure) / len(self.exposure) if self.exposure else 0.0


@dataclass(frozen=True)
class BacktestReport:
    strategy_id: str
    equity_dates: list[date]
    equity_curve: list[float]
    final_return: float
    #: Annualized Sharpe of the per-bar returns, sample std (ddof=1), net of
    #: ``risk_free_rate``.
    sharpe: float
    max_drawdown: float
    cagr: float
    #: Per-bar profit factor — sum of positive per-bar returns divided by the
    #: absolute sum of negative ones. ``inf`` with gains and no losses,
    #: ``0.0`` with no gains. Not a trade-level figure: see
    #: ``trade_stats.trade_profit_factor``.
    bar_profit_factor: float = 0.0
    #: Splits and dividends applied to held positions, in order; they
    #: explain equity-curve jumps (a dividend credits cash, a split changes
    #: the share count at unchanged value).
    corporate_actions: tuple[CorporateActionRecord, ...] = ()
    # ---- return-series metrics (``stonks.backtest.metrics``) ----
    #: Bars per year the annualized figures use.
    periods_per_year: float = _TRADING_DAYS_PER_YEAR
    #: Trading days (sessions) per year of the curve's calendar: 252 for
    #: exchange sessions, 365 for crypto. Turns annual into daily turnover.
    sessions_per_year: float = _TRADING_DAYS_PER_YEAR
    n_bars: int = 0
    sortino: float = 0.0
    calmar: float = 0.0
    ulcer_index: float = 0.0
    upi: float = 0.0
    max_dd_duration_bars: int = 0
    #: Historical 5th-percentile per-bar return (a loss is negative).
    var_95: float = 0.0
    #: Mean per-bar return at or below ``var_95``.
    es_95: float = 0.0
    skew: float = 0.0
    #: Non-excess kurtosis (normal = 3).
    kurtosis: float = 3.0
    autocorr_1: float = 0.0
    lower_tail_ratio: float = 1.0
    # ---- trade ledger (``stonks.backtest.trades.with_trades``) ----
    #: Round trips; empty until a caller attaches the ledger.
    trades: tuple[RoundTrip, ...] = ()
    trade_stats: TradeStats = TradeStats()  # noqa: RUF009 - frozen, safe to share
    #: Tulchinsky fitness; needs turnover, so ``None`` until trades attach.
    fitness: float | None = None
    #: Financing, forced orders, exposure and long/short attribution of a
    #: backtest that allows shorts; ``None`` for a long-only one.
    short_book: ShortBookReport | None = None

    @property
    def profit_factor(self) -> float:
        """Deprecated alias of ``bar_profit_factor``; kept for one release."""
        return self.bar_profit_factor

    @property
    def returns(self) -> list[float]:
        """Simple per-bar returns of ``equity_curve``."""
        return metrics.bar_returns(self.equity_curve)


def compute_report(
    strategy_id: str,
    equity_dates: Sequence[date],
    equity_curve: Sequence[float],
    periods_per_year: float = _TRADING_DAYS_PER_YEAR,
    corporate_actions: Iterable[CorporateActionRecord] = (),
    risk_free_rate: float = 0.0,
    sessions_per_year: float = _TRADING_DAYS_PER_YEAR,
) -> BacktestReport:
    """Report of an equity curve; ``risk_free_rate`` is annual and only
    affects Sharpe and Sortino. ``sessions_per_year`` is the calendar's
    trading days a year (``BacktestReport.sessions_per_year``)."""
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
            periods_per_year=periods_per_year,
            sessions_per_year=sessions_per_year,
        )

    start = curve[0]
    end = curve[-1]
    returns = metrics.bar_returns(curve)
    max_dd = metrics.max_drawdown(curve)
    ulcer = metrics.ulcer_index(curve)
    cagr = metrics.cagr(start, end, metrics.years_spanned(dates))
    return BacktestReport(
        strategy_id=strategy_id,
        equity_dates=dates,
        equity_curve=curve,
        final_return=end / start - 1.0 if start > 0 else 0.0,
        sharpe=metrics.sharpe(returns, periods_per_year, risk_free_rate),
        max_drawdown=max_dd,
        cagr=cagr,
        bar_profit_factor=metrics.profit_factor(returns),
        corporate_actions=events,
        periods_per_year=periods_per_year,
        sessions_per_year=sessions_per_year,
        n_bars=len(curve),
        sortino=metrics.sortino(returns, periods_per_year, risk_free_rate),
        calmar=metrics.calmar(cagr, max_dd),
        ulcer_index=ulcer,
        upi=metrics.upi(cagr, ulcer),
        max_dd_duration_bars=metrics.max_drawdown_duration_bars(curve),
        var_95=metrics.value_at_risk(returns),
        es_95=metrics.expected_shortfall(returns),
        skew=metrics.skew(returns),
        kurtosis=metrics.kurtosis(returns),
        autocorr_1=metrics.autocorr_1(returns),
        lower_tail_ratio=metrics.lower_tail_ratio(returns),
    )
