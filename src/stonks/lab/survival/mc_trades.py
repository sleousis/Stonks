"""Monte Carlo over the trade sequence (BL-17, after Davey and Vince).

A backtest is one ordering of its trades. This test resamples the
validation window's closed round trips into ``n_paths`` one-year trade
sequences and asks how bad that year could have been:

- Each trade contributes ``pnl / equity before entry`` (the equity mark of
  the last bar before the entry bar), a size-independent return.
- Every path draws ``n_per_year`` trades with replacement, where
  ``n_per_year`` is the observed trade rate (closed trades per year of the
  window), and compounds them from 1.0. A path whose drawdown reaches
  ``ruin_drawdown`` stops there: it is ruined.
- Metrics: ``risk_of_ruin`` (share of ruined paths), ``median_max_dd`` and
  ``p95_max_dd`` (positive drawdown fractions), ``median_return`` and
  ``p05_return`` (the year's compounded return), ``return_to_dd``
  (median return / median max drawdown) and ``prob_profit``.

It passes when there are at least ``min_trades`` closed trades,
``risk_of_ruin <= max_risk_of_ruin``, ``return_to_dd >= min_return_to_dd``
and ``prob_profit >= min_prob_profit`` (Davey's conventions, configurable).
With too few trades the test fails with an "insufficient data" note and
stores no drawdown band, so go-live checks that read ``p95_max_dd`` /
``median_max_dd`` (BL-25, BL-29) see missing data, never a band built on
noise.

Trade source: a ``stitched_oos_report`` attribute on the dataset (a
``BacktestReport`` of stitched walk-forward OOS trades, BL-20) when
present, else a backtest of the validation window.

Seed: ``seed`` when set, else the run's root seed (the tuner's seed, via
``bind_run``), else 17. The draw is a single vectorised numpy array of
``n_paths x n_per_year``; no processes are needed.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from stonks.backtest.report import BacktestReport
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest
from stonks.logging import get_logger

_log = get_logger("stonks.lab.survival.mc_trades")

_DEFAULT_SEED = 17


class MonteCarloTradesOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n_paths: int = Field(default=5000, ge=1000, le=20000)
    #: Drawdown (fraction of peak equity) at which a path is ruined.
    ruin_drawdown: float = Field(default=0.40, gt=0.0, lt=1.0)
    min_trades: int = Field(default=30, ge=1)
    max_risk_of_ruin: float = Field(default=0.10, ge=0.0, le=1.0)
    min_return_to_dd: float = Field(default=2.0, ge=0.5, le=4.0)
    min_prob_profit: float = Field(default=0.8, ge=0.0, le=1.0)
    #: ``None``: the run's root seed (see module doc).
    seed: int | None = None


@dataclass(frozen=True)
class TradePathsResult:
    risk_of_ruin: float
    median_max_dd: float
    p95_max_dd: float
    median_return: float
    p05_return: float
    return_to_dd: float
    prob_profit: float

    def metrics(self) -> dict[str, float]:
        return {
            "risk_of_ruin": self.risk_of_ruin,
            "median_max_dd": self.median_max_dd,
            "p95_max_dd": self.p95_max_dd,
            "median_return": self.median_return,
            "p05_return": self.p05_return,
            "return_to_dd": self.return_to_dd,
            "prob_profit": self.prob_profit,
        }


def simulate_trade_paths(
    contributions: np.ndarray,
    *,
    n_per_year: int,
    n_paths: int,
    ruin_drawdown: float = 0.40,
    seed: int | None = None,
) -> TradePathsResult:
    """Resample ``contributions`` (per-trade returns) into ``n_paths``
    paths of ``n_per_year`` trades each; see the module doc."""
    r = np.asarray(contributions, dtype=float)
    if r.size == 0 or n_per_year < 1 or n_paths < 1:
        raise ValueError("need at least one trade, one trade per path and one path")
    rng = np.random.default_rng(seed)
    draws = r[rng.integers(0, r.size, size=(n_paths, n_per_year))]
    growth = np.maximum(1.0 + draws, 0.0)
    equity = np.cumprod(growth, axis=1)
    peak = np.maximum.accumulate(np.maximum(equity, 1.0), axis=1)
    drawdown = 1.0 - equity / peak
    ruin_hit = drawdown >= ruin_drawdown
    ruined = ruin_hit.any(axis=1)
    # a ruined path stops at its first ruinous trade
    stop = np.where(ruined, ruin_hit.argmax(axis=1), n_per_year - 1)
    rows = np.arange(n_paths)
    final = equity[rows, stop]
    cols = np.arange(n_per_year)
    max_dd = np.where(cols[None, :] <= stop[:, None], drawdown, 0.0).max(axis=1)
    median_dd = float(np.median(max_dd))
    median_ret = float(np.median(final) - 1.0)
    no_dd_ratio = float("inf") if median_ret > 0 else 0.0
    ratio = median_ret / median_dd if median_dd > 0 else no_dd_ratio
    return TradePathsResult(
        risk_of_ruin=float(ruined.mean()),
        median_max_dd=median_dd,
        p95_max_dd=float(np.percentile(max_dd, 95)),
        median_return=median_ret,
        p05_return=float(np.percentile(final, 5) - 1.0),
        return_to_dd=float(ratio),
        prob_profit=float((final > 1.0).mean()),
    )


def trade_contributions(report: BacktestReport) -> np.ndarray:
    """``pnl / equity before entry`` for each closed round trip of
    ``report``, in trade order. The equity before entry is the mark of the
    last equity date before the entry bar's date (the first mark when the
    entry is on the first bar)."""
    dates = [_as_date(d) for d in report.equity_dates]
    curve = report.equity_curve
    out: list[float] = []
    for trade in report.trades:
        if trade.is_open:
            continue
        i = bisect.bisect_left(dates, _as_date(trade.entry_ts)) - 1
        equity = curve[max(i, 0)] if curve else 0.0
        if equity > 0:
            out.append(trade.pnl / equity)
    return np.asarray(out, dtype=float)


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


class MonteCarloTradesTest:
    id = "mc_trades"
    Options = MonteCarloTradesOptions

    def __init__(self, options: MonteCarloTradesOptions | None = None, **overrides: Any) -> None:
        base = options or MonteCarloTradesOptions()
        self.options = base.model_copy(update=overrides) if overrides else base
        if overrides:  # re-validate the overridden fields
            self.options = MonteCarloTradesOptions.model_validate(self.options.model_dump())
        self._run_seed: int | None = None

    @classmethod
    def build(cls, options: MonteCarloTradesOptions) -> MonteCarloTradesTest:
        return cls(options)

    @property
    def seed(self) -> int:
        if self.options.seed is not None:
            return self.options.seed
        return self._run_seed if self._run_seed is not None else _DEFAULT_SEED

    def bind_run(self, ctx: Any) -> None:
        """Take the run's root seed (the tuner's) when no seed is set."""
        tuner = getattr(getattr(ctx, "setup", None), "tuner", None)
        for name in ("seed", "_seed"):
            value = getattr(tuner, name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                self._run_seed = value
                return

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        stitched = getattr(context, "stitched_oos_report", None)
        if isinstance(stitched, BacktestReport):
            return self.evaluate(stitched, source="stitched walk-forward OOS")
        report = run_backtest(strategy, context, context.val_window)
        return self.evaluate(report, source="validation window")

    def evaluate(self, report: BacktestReport, *, source: str = "report") -> SurvivalReport:
        years = max(len(report.equity_curve) - 1, 0) / report.periods_per_year
        return self.evaluate_contributions(trade_contributions(report), years, source=source)

    def evaluate_contributions(
        self, contributions: np.ndarray, years: float, *, source: str = "trades"
    ) -> SurvivalReport:
        opts = self.options
        r = np.asarray(contributions, dtype=float)
        n = int(r.size)
        base = {"n_trades": float(n), "min_trades": float(opts.min_trades)}
        if n < opts.min_trades:
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics=base,
                notes=(
                    f"insufficient data: {n} closed trades in the {source}, "
                    f"{opts.min_trades} required; no Monte Carlo band stored"
                ),
            )
        n_per_year = max(1, round(n / years)) if years > 0 else n
        seed = self.seed
        _log.info("mc_trades.start", n_trades=n, n_per_year=n_per_year, seed=seed)
        result = simulate_trade_paths(
            r,
            n_per_year=n_per_year,
            n_paths=opts.n_paths,
            ruin_drawdown=opts.ruin_drawdown,
            seed=seed,
        )
        failures = []
        if result.risk_of_ruin > opts.max_risk_of_ruin:
            failures.append(f"risk_of_ruin {result.risk_of_ruin:.3f} > {opts.max_risk_of_ruin}")
        if result.return_to_dd < opts.min_return_to_dd:
            failures.append(f"return_to_dd {result.return_to_dd:.2f} < {opts.min_return_to_dd}")
        if result.prob_profit < opts.min_prob_profit:
            failures.append(f"prob_profit {result.prob_profit:.3f} < {opts.min_prob_profit}")
        verdict = "; ".join(failures) if failures else "all thresholds met"
        return SurvivalReport(
            test_id=self.id,
            passed=not failures,
            metrics={
                **base,
                "n_per_year": float(n_per_year),
                "n_paths": float(opts.n_paths),
                **result.metrics(),
            },
            notes=f"{source}; seed={seed}; {verdict}",
        )
