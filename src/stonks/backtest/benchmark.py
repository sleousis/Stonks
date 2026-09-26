"""The one benchmark source for backtests (BL-22) and beta attribution (BL-23).

A benchmark is a buy-and-hold curve marked on **the strategy's own equity
timestamps**, so every statistic compares like with like.

Specs (:func:`normalize_spec`)
------------------------------
- ``"auto"`` (default): :data:`AUTO_BENCHMARK_TICKER` when the lake prices
  it at the first date, else ``"EW"``.
- ``"EW"``: equal-weight buy-and-hold of the universe, bought on the first
  date and never rebalanced.
- any other string: that ticker, e.g. ``"QQQ.US"``.
- ``"none"`` / ``""`` / ``None``: no benchmark.

No look-ahead
-------------
The value at date ``d`` uses only the latest bar stamped at or before
``d``; no bar after the last curve date is even loaded. A name's entry price
is the last one known at the first date (at most :data:`MAX_STALENESS`
old). A ticker that lists later sits in cash until its first bar.

No survivorship
---------------
``"EW"`` holds only the names priced at the first date. A name that enters
the universe mid-window is left out (listed in ``excluded``): allocating to
it on day one would need to know that it will list. A name that stops
trading keeps its last close, like the engine's marks.

Prices are total-return: back-adjusted for splits and dividends through
``stonks.features.price_adjustment`` (corporate-action events, else the
vendor's ``adj_close``), adjusted as of the last loaded bar.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from stonks.backtest import metrics
from stonks.backtest.report import BacktestReport
from stonks.core.interval import Interval
from stonks.core.timeutil import as_datetime
from stonks.features.price_adjustment import SeriesAdjustment
from stonks.stats.hac import default_lags
from stonks.store.corporate_actions import LakeCorporateActions

__all__ = [
    "AUTO_BENCHMARK_TICKER",
    "DEFAULT_BENCHMARK",
    "EQUAL_WEIGHT",
    "MAX_STALENESS",
    "Attribution",
    "BenchmarkCurve",
    "BenchmarkResult",
    "BenchmarkStats",
    "BenchmarkedReport",
    "attribute",
    "benchmark_curve",
    "benchmark_stats",
    "compare",
    "normalize_spec",
    "with_benchmark",
]

#: What ``"auto"`` tries first.
AUTO_BENCHMARK_TICKER = "SPY.US"
DEFAULT_BENCHMARK = "auto"
#: Display name of the equal-weight universe benchmark.
EQUAL_WEIGHT = "EW"
#: Oldest price that still counts as "known at the first date" (weekends,
#: holidays, a crypto window starting on a Saturday).
MAX_STALENESS = timedelta(days=7)

_NONE_SPECS = frozenset({"", "none", "off", "null"})


def normalize_spec(spec: str | None) -> str | None:
    """``"auto"``, ``"ew"``, a ticker (stripped, case kept) or ``None``."""
    if spec is None:
        return None
    text = str(spec).strip()
    low = text.lower()
    if low in _NONE_SPECS:
        return None
    if low in ("auto", "ew"):
        return low
    return text


# ---- curve -----------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkCurve:
    """Benchmark value on each of the strategy's timestamps."""

    #: ``"EW"`` or the ticker.
    name: str
    #: The normalized spec that produced it (``"auto"`` resolves to a name).
    spec: str
    dates: tuple[date, ...]
    values: tuple[float, ...]
    #: Tickers held (the ticker itself for a named benchmark).
    members: tuple[str, ...] = ()
    #: Universe tickers ``"EW"`` left out (not priced at the first date).
    excluded: tuple[str, ...] = ()


def benchmark_curve(
    lake: Any,
    spec: str | None,
    dates: Sequence[date],
    *,
    universe: Sequence[str] = (),
    interval: Interval = Interval.DAY_1,
    initial_value: float = 1.0,
) -> BenchmarkCurve | None:
    """Buy-and-hold curve of ``spec`` on ``dates`` (ascending), starting at
    ``initial_value``; ``None`` for no spec, no dates, or no usable bars."""
    norm = normalize_spec(spec)
    if norm is None or not dates:
        return None
    stamps = [as_datetime(d) for d in dates]
    universe = list(dict.fromkeys(universe))
    if norm == "auto":
        tickers = list(dict.fromkeys([AUTO_BENCHMARK_TICKER, *universe]))
    elif norm == "ew":
        tickers = universe
    else:
        tickers = [norm]
    prices = _aligned_prices(lake, tickers, stamps, interval)

    if norm == "auto" and AUTO_BENCHMARK_TICKER in prices:
        series = prices[AUTO_BENCHMARK_TICKER]
        if not np.isnan(series[0]):
            return _curve(
                AUTO_BENCHMARK_TICKER,
                norm,
                dates,
                [series],
                [AUTO_BENCHMARK_TICKER],
                [],
                initial_value,
            )
    if norm in ("auto", "ew"):
        members = [t for t in universe if t in prices and not np.isnan(prices[t][0])]
        if not members:
            return None
        excluded = [t for t in universe if t not in members]
        return _curve(
            EQUAL_WEIGHT,
            norm,
            dates,
            [prices[t] for t in members],
            members,
            excluded,
            initial_value,
        )
    if norm not in prices:
        return None
    return _curve(norm, norm, dates, [prices[norm]], [norm], [], initial_value)


def _curve(
    name: str,
    spec: str,
    dates: Sequence[date],
    series: list[np.ndarray],
    members: list[str],
    excluded: list[str],
    initial_value: float,
) -> BenchmarkCurve:
    """Equal slices of ``initial_value``; a slice is cash until its series
    has a price, then grows with it."""
    slice_value = initial_value / len(series)
    total = np.zeros(len(dates))
    for s in series:
        known = ~np.isnan(s)
        entry = s[known][0]
        total += np.where(known, slice_value * s / entry, slice_value)
    return BenchmarkCurve(
        name=name,
        spec=spec,
        dates=tuple(dates),
        values=tuple(float(v) for v in total),
        members=tuple(members),
        excluded=tuple(excluded),
    )


def _aligned_prices(
    lake: Any, tickers: list[str], stamps: list[datetime], interval: Interval
) -> dict[str, np.ndarray]:
    """Per ticker with a usable bar: adjusted closes on ``stamps`` (the
    latest bar at or before each stamp; NaN before the first one). A ticker
    whose only pre-window bar is older than ``MAX_STALENESS`` starts NaN."""
    if not tickers:
        return {}
    first, last = stamps[0], stamps[-1]
    frame = lake.sql(
        """
        SELECT ticker, timestamp, close, adj_close
          FROM bars
         WHERE ticker = ANY(?) AND interval = ? AND timestamp <= ? AND close > 0
        QUALIFY timestamp >= COALESCE(
                    MAX(CASE WHEN timestamp <= ? THEN timestamp END)
                        OVER (PARTITION BY ticker),
                    ?)
         ORDER BY ticker, timestamp
        """,
        [tickers, interval.code, last, first, first],
    )
    if frame is None or frame.empty:
        return {}
    actions = LakeCorporateActions(lake).load(tickers)
    wanted = np.array(stamps, dtype="datetime64[us]")
    out: dict[str, np.ndarray] = {}
    for ticker, bars in frame.groupby("ticker", sort=False):
        bars = bars.reset_index(drop=True)
        ts = pd.to_datetime(bars["timestamp"]).to_numpy(dtype="datetime64[us]")
        if ts[0] < wanted[0] and ts[0] < np.datetime64(first - MAX_STALENESS, "us"):
            bars, ts = bars.iloc[1:].reset_index(drop=True), ts[1:]
            if bars.empty:
                continue
        raw = bars["close"].to_numpy(dtype=float)
        adjustment = SeriesAdjustment.build(bars, actions.for_ticker(str(ticker)))
        adjusted = adjustment.closes(raw, 0, len(raw))
        idx = np.searchsorted(ts, wanted, side="right") - 1
        out[str(ticker)] = np.where(idx >= 0, adjusted[np.clip(idx, 0, None)], np.nan)
    return out


# ---- attribution -----------------------------------------------------------------


@dataclass(frozen=True)
class Attribution:
    """OLS of ``r = alpha + beta * r_b + e`` (per-bar simple returns)."""

    #: Per-bar intercept times bars per year.
    alpha_annual: float = 0.0
    beta: float = 0.0
    #: Intercept over its Newey-West (HAC) standard error.
    alpha_tstat: float = 0.0
    r2: float = 0.0
    #: Appraisal ratio: ``alpha / std(e) * sqrt(ppy)``.
    residual_sharpe: float = 0.0


def _ratio(num: float, den: float, tol: float = 0.0) -> float:
    if abs(den) > tol:
        return num / den
    if abs(num) <= tol:
        return 0.0
    return math.inf if num > 0 else -math.inf


def attribute(
    returns: Sequence[float] | np.ndarray,
    benchmark_returns: Sequence[float] | np.ndarray,
    periods_per_year: float = 252.0,
    lags: int | None = None,
) -> Attribution:
    """Alpha, beta, the alpha's HAC t-stat, R² and residual Sharpe. The
    t-stat uses a Newey-West sandwich for the intercept with ``lags``
    (default: Newey-West's rule of thumb). Fewer than 3 points give zeros;
    a flat benchmark gives ``beta = 0`` and the mean as alpha."""
    r = np.asarray(returns, dtype=float)
    b = np.asarray(benchmark_returns, dtype=float)
    if r.shape != b.shape:
        raise ValueError(f"returns and benchmark returns differ in length: {r.size} != {b.size}")
    t = r.size
    if t < 3:
        return Attribution()
    scale = max(1.0, float(np.abs(r).max()), float(np.abs(b).max()))
    tiny = 1e-12 * scale
    b_dev = b - b.mean()
    var_b = float(b_dev @ b_dev) / t
    if var_b <= tiny**2:
        beta = 0.0
        x = np.ones((t, 1))
    else:
        beta = float(b_dev @ (r - r.mean())) / t / var_b
        x = np.column_stack([np.ones(t), b])
    alpha = float(r.mean() - beta * b.mean())
    resid = r - alpha - beta * b

    n_lags = min(default_lags(t) if lags is None else int(lags), t - 1)
    xtx_inv = np.linalg.inv(x.T @ x)
    u = x * resid[:, None]
    s = u.T @ u
    for lag in range(1, n_lags + 1):
        g = u[lag:].T @ u[:-lag]
        s += (1 - lag / (n_lags + 1)) * (g + g.T)
    var_alpha = float((xtx_inv @ s @ xtx_inv)[0, 0])
    se_alpha = math.sqrt(max(var_alpha, 0.0))

    r_dev = r - r.mean()
    ss_tot = float(r_dev @ r_dev)
    ss_res = float(resid @ resid)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > tiny**2 else 0.0
    resid_std = float(resid.std(ddof=1))
    return Attribution(
        alpha_annual=alpha * periods_per_year,
        beta=beta,
        alpha_tstat=_ratio(alpha, se_alpha, tiny),
        r2=min(max(r2, 0.0), 1.0),
        residual_sharpe=_ratio(alpha, resid_std, tiny) * math.sqrt(periods_per_year),
    )


# ---- stats -----------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkStats:
    """Strategy versus benchmark on the same timestamps."""

    name: str = ""
    n_obs: int = 0
    benchmark_cagr: float = 0.0
    #: Strategy CAGR minus benchmark CAGR over the same span.
    excess_cagr: float = 0.0
    benchmark_sharpe: float = 0.0
    benchmark_max_dd: float = 0.0
    beta: float = 0.0
    alpha_annual: float = 0.0
    alpha_tstat: float = 0.0
    r2: float = 0.0
    residual_sharpe: float = 0.0
    #: Annualized standard deviation (ddof=1) of the active returns.
    tracking_error: float = 0.0
    #: ``mean(r - r_b) / std(r - r_b) * sqrt(ppy)``.
    information_ratio: float = 0.0
    #: Mean strategy return over mean benchmark return, on up bars.
    up_capture: float = 0.0
    #: The same on down bars (below 1 means it lost less).
    down_capture: float = 0.0
    correlation: float = 0.0


def _aligned_returns(curve: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    prev = curve[:-1]
    valid = prev > 0
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(valid, curve[1:] / np.where(valid, prev, 1.0) - 1.0, 0.0)
    return out, valid


def _capture(r: np.ndarray, b: np.ndarray, mask: np.ndarray) -> float:
    if not mask.any():
        return 0.0
    return _ratio(float(r[mask].mean()), float(b[mask].mean()))


def benchmark_stats(
    dates: Sequence[date],
    equity_curve: Sequence[float],
    benchmark_values: Sequence[float],
    periods_per_year: float = 252.0,
    name: str = "",
) -> BenchmarkStats:
    """Relative statistics of ``equity_curve`` against ``benchmark_values``,
    both marked on ``dates``. Returns are paired bar by bar; a step where
    either curve starts non-positive is dropped from both."""
    eq = np.asarray(equity_curve, dtype=float)
    bm = np.asarray(benchmark_values, dtype=float)
    if not (len(dates) == eq.size == bm.size):
        raise ValueError(
            f"dates, equity curve and benchmark differ in length: "
            f"{len(dates)}, {eq.size}, {bm.size}"
        )
    if eq.size < 2:
        return BenchmarkStats(name=name)
    r_all, ok_r = _aligned_returns(eq)
    b_all, ok_b = _aligned_returns(bm)
    keep = ok_r & ok_b
    r, b = r_all[keep], b_all[keep]
    years = metrics.years_spanned(list(dates))
    strategy_cagr = metrics.cagr(float(eq[0]), float(eq[-1]), years)
    bench_cagr = metrics.cagr(float(bm[0]), float(bm[-1]), years)
    active = r - b
    if active.size >= 2:
        te_bar = float(active.std(ddof=1))
        tol = 1e-12 * max(1.0, float(np.abs(active).max()))
        tracking = te_bar * math.sqrt(periods_per_year) if te_bar > tol else 0.0
        info = float(active.mean()) / te_bar * math.sqrt(periods_per_year) if te_bar > tol else 0.0
    else:
        tracking = info = 0.0
    corr = 0.0
    if r.size >= 2 and r.std() > 0 and b.std() > 0:
        corr = float(np.corrcoef(r, b)[0, 1])
    att = attribute(r, b, periods_per_year)
    excess = strategy_cagr - bench_cagr
    if math.isnan(excess):  # inf - inf on a tiny intraday span
        excess = 0.0
    return BenchmarkStats(
        name=name,
        n_obs=int(r.size),
        benchmark_cagr=bench_cagr,
        excess_cagr=excess,
        benchmark_sharpe=metrics.sharpe(b, periods_per_year),
        benchmark_max_dd=metrics.max_drawdown(bm),
        beta=att.beta,
        alpha_annual=att.alpha_annual,
        alpha_tstat=att.alpha_tstat,
        r2=att.r2,
        residual_sharpe=att.residual_sharpe,
        tracking_error=tracking,
        information_ratio=info,
        up_capture=_capture(r, b, b > 0),
        down_capture=_capture(r, b, b < 0),
        correlation=corr,
    )


# ---- attaching to a report ----------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkResult:
    curve: BenchmarkCurve
    stats: BenchmarkStats

    def metrics(self) -> dict[str, float]:
        """Numeric stats as a flat ``{name: float}`` (survival reports)."""
        return {
            f.name: float(getattr(self.stats, f.name))
            for f in fields(self.stats)
            if f.name != "name"
        }


@dataclass(frozen=True)
class BenchmarkedReport(BacktestReport):
    """A :class:`BacktestReport` with its benchmark (``None`` when there is
    none). Every consumer reads ``getattr(report, "benchmark", None)``."""

    benchmark: BenchmarkResult | None = None


def compare(report: BacktestReport, curve: BenchmarkCurve) -> BenchmarkResult:
    """Stats of ``report`` against ``curve``; the curve must be marked on
    the report's own equity dates."""
    if list(curve.dates) != list(report.equity_dates):
        raise ValueError("benchmark curve dates must equal the report's equity dates")
    stats = benchmark_stats(
        report.equity_dates,
        report.equity_curve,
        curve.values,
        periods_per_year=report.periods_per_year,
        name=curve.name,
    )
    return BenchmarkResult(curve=curve, stats=stats)


def with_benchmark(report: BacktestReport, curve: BenchmarkCurve | None) -> BenchmarkedReport:
    """``report`` as a :class:`BenchmarkedReport` carrying ``curve``'s stats."""
    result = compare(report, curve) if curve is not None else None
    values = {f.name: getattr(report, f.name) for f in fields(BacktestReport)}
    return BenchmarkedReport(**values, benchmark=result)
