"""Benchmark-relative survival test (BL-22, BL-23): we pay for alpha, not beta.

Backtests the strategy on the validation window (or the full window) and
compares it with its benchmark on the same bars and timestamps
(``stonks.backtest.benchmark``). It passes when the information ratio is at
least ``min_ir`` and the excess CAGR at least ``min_excess_cagr``, and,
when ``require_alpha_tstat`` is set, the HAC t-stat of the alpha clears it
(2.0 is Chan's alpha test). Beta, alpha and its t-stat are always reported.

The benchmark is the ``benchmark`` option, else the dataset's
``benchmark`` attribute, else ``"auto"``. A disabled dataset benchmark
(``"none"``) falls back to ``"auto"``: this test has nothing to measure
without one. An unpriced benchmark fails the test with a note.

No evidence is never a pass (RS-09): the test fails with "insufficient
data" when the strategy made fewer than ``min_trades`` round trips (open lots count), when fewer
than ``min_bars`` paired returns exist, or when the tracking error is zero
(no active risk, so the IR says nothing). A strategy that sits in cash
while the benchmark falls would otherwise pass on a positive excess CAGR.
"""

from __future__ import annotations

from typing import Any, Literal

from stonks.backtest.benchmark import DEFAULT_BENCHMARK, normalize_spec
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest

_WINDOWS = ("val", "full")


class BenchmarkRelativeTest:
    id = "benchmark_relative"

    def __init__(
        self,
        min_ir: float = 0.0,
        min_excess_cagr: float = 0.0,
        require_alpha_tstat: float | None = None,
        window: Literal["val", "full"] = "val",
        benchmark: str | None = None,
        min_trades: int = 1,
        min_bars: int = 20,
    ) -> None:
        if window not in _WINDOWS:
            raise ValueError(f"window must be one of {_WINDOWS}, got {window!r}")
        self._min_ir = float(min_ir)
        self._min_excess = float(min_excess_cagr)
        self._min_t = None if require_alpha_tstat is None else float(require_alpha_tstat)
        self._window = window
        self._benchmark = benchmark
        self._min_trades = int(min_trades)
        self._min_bars = int(min_bars)

    def _spec(self, context: Any) -> str:
        for candidate in (self._benchmark, getattr(context, "benchmark", DEFAULT_BENCHMARK)):
            if normalize_spec(candidate) is not None:
                return str(candidate)
        return DEFAULT_BENCHMARK

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        window = context.val_window if self._window == "val" else context.full_window
        spec = self._spec(context)
        report = run_backtest(strategy, context, window, benchmark=spec)
        bench = getattr(report, "benchmark", None)
        if bench is None:
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics={"cagr": float(report.cagr)},
                notes=f"benchmark {spec!r} has no bars on the {self._window} window",
            )
        stats = bench.stats
        metrics = {"cagr": float(report.cagr), **bench.metrics()}
        n_trades = len(report.trades)  # round trips, open lots included
        metrics["n_trades"] = float(n_trades)
        lacking = []
        if n_trades < self._min_trades:
            lacking.append(f"{n_trades} trades (min {self._min_trades})")
        if stats.n_obs < self._min_bars:
            lacking.append(f"{stats.n_obs} bars (min {self._min_bars})")
        if not stats.tracking_error > 0:
            lacking.append("zero tracking error")
        if lacking:
            return SurvivalReport(
                test_id=self.id,
                passed=False,
                metrics=metrics,
                notes=f"insufficient data vs {bench.curve.name} ({self._window}): "
                + ", ".join(lacking),
            )
        checks = [
            stats.information_ratio >= self._min_ir,
            stats.excess_cagr >= self._min_excess,
        ]
        if self._min_t is not None:
            checks.append(stats.alpha_tstat >= self._min_t)
        notes = (
            f"vs {bench.curve.name} ({self._window}): IR {stats.information_ratio:.2f} "
            f"(min {self._min_ir:g}), excess CAGR {stats.excess_cagr:+.2%} "
            f"(min {self._min_excess:+.2%}), beta {stats.beta:.2f}, "
            f"alpha {stats.alpha_annual:+.2%} (t {stats.alpha_tstat:.2f}"
            + (f", min {self._min_t:g}" if self._min_t is not None else "")
            + ")"
        )
        return SurvivalReport(test_id=self.id, passed=all(checks), metrics=metrics, notes=notes)
