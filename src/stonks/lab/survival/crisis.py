"""Crisis-window survival test (BL-48, principle P35; Kindleberger, Danielsson).

A backtest that skips a crash hides how a strategy behaves when it matters.
This test backtests the strategy over each named crisis window that the
dataset covers and compares its maximum drawdown there with the
benchmark's over the same bars:

====================  ======================  ========================
window                dates                   what happened
====================  ======================  ========================
``gfc``               2007-10-09 .. 2009-03-09  global financial crisis
``euro_2011``         2011-05-02 .. 2011-10-03  euro debt crisis
``q4_2018``           2018-10-01 .. 2018-12-24  Q4 2018 sell-off
``covid``             2020-02-19 .. 2020-03-23  COVID crash
``h1_2022``           2022-01-03 .. 2022-06-30  2022 rate shock
``crypto_2022``       2022-05-01 .. 2022-11-30  Terra and FTX collapses
====================  ======================  ========================

A window passes when the strategy's drawdown is no deeper than
``max_dd_ratio`` (1.5) times the benchmark's, or ``abs_floor`` (5%),
whichever is larger (the floor keeps a quiet benchmark from demanding a
zero drawdown). The test passes when every covered window passes.

Coverage: a window counts only where the dataset holds at least
``min_bars`` bars of it and the benchmark is priced there. Windows without
data are skipped and ``crisis_coverage`` (covered / all) is reported.
When no window is covered the result follows ``require_coverage``: by
default the test passes with a note, because P35 asks for a crisis only
"where data allows"; set it to make missing crisis data a failure.
The strategy is judged on the whole dataset window (crises are history,
not a tuning target), with bars before each window visible for look-backs.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.backtest.benchmark import DEFAULT_BENCHMARK, normalize_spec
from stonks.core.protocols import Strategy, SurvivalReport
from stonks.lab.backtesting import run_backtest


class CrisisWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    start: date
    end: date


CRISIS_WINDOWS: tuple[CrisisWindow, ...] = (
    CrisisWindow(name="gfc", start=date(2007, 10, 9), end=date(2009, 3, 9)),
    CrisisWindow(name="euro_2011", start=date(2011, 5, 2), end=date(2011, 10, 3)),
    CrisisWindow(name="q4_2018", start=date(2018, 10, 1), end=date(2018, 12, 24)),
    CrisisWindow(name="covid", start=date(2020, 2, 19), end=date(2020, 3, 23)),
    CrisisWindow(name="h1_2022", start=date(2022, 1, 3), end=date(2022, 6, 30)),
    CrisisWindow(name="crypto_2022", start=date(2022, 5, 1), end=date(2022, 11, 30)),
)


class CrisisTest:
    """The strategy's drawdown in each named crisis window it has data
    for must stay within 1.5x the benchmark's."""

    id = "crisis"

    class Options(BaseModel):
        model_config = ConfigDict(extra="forbid")

        max_dd_ratio: float = Field(
            default=1.5,
            gt=0.0,
            description="Deepest fall allowed in a crisis, as a multiple of the benchmark's fall.",
        )
        abs_floor: float = Field(
            default=0.05,
            ge=0.0,
            lt=1.0,
            description="A fall this small always passes, so a calm benchmark does not demand zero.",
        )
        min_bars: int = Field(
            default=10, ge=2, description="Fewest bars of a crisis the data must hold to judge it."
        )
        require_coverage: bool = Field(
            default=False, description="Fail when the data covers no crisis at all."
        )
        benchmark: str | None = Field(
            default=None, description="Ticker to compare with. Empty uses the run's benchmark."
        )
        #: Custom windows replacing :data:`CRISIS_WINDOWS`.
        windows: list[CrisisWindow] | None = Field(
            default=None, description="Your own crisis dates in place of the built-in list."
        )

    def __init__(self, options: CrisisTest.Options | None = None) -> None:
        self.options = options or CrisisTest.Options()

    @classmethod
    def build(cls, options: CrisisTest.Options) -> CrisisTest:
        return cls(options)

    def _spec(self, context: Any) -> str:
        for candidate in (self.options.benchmark, getattr(context, "benchmark", None)):
            if normalize_spec(candidate) is not None:
                return str(candidate)
        return DEFAULT_BENCHMARK

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        o = self.options
        windows = list(o.windows) if o.windows is not None else list(CRISIS_WINDOWS)
        start, end = context.full_window
        spec = self._spec(context)
        metrics: dict[str, float] = {"n_windows": float(len(windows))}
        covered: list[str] = []
        failures: list[str] = []
        for window in windows:
            lo, hi = max(window.start, start), min(window.end, end)
            if lo > hi:
                continue
            report = run_backtest(strategy, context, (lo, hi), benchmark=spec)
            bench = getattr(report, "benchmark", None)
            if len(report.equity_curve) < o.min_bars or bench is None:
                continue
            dd = abs(float(report.max_drawdown))
            bench_dd = abs(float(bench.stats.benchmark_max_dd))
            limit = max(o.max_dd_ratio * bench_dd, o.abs_floor)
            covered.append(window.name)
            metrics[f"dd_{window.name}"] = -dd
            metrics[f"bench_dd_{window.name}"] = -bench_dd
            if dd > limit:
                failures.append(
                    f"{window.name}: drawdown {dd:.1%} > {limit:.1%} "
                    f"({o.max_dd_ratio:g}x benchmark {bench_dd:.1%})"
                )
        metrics["n_covered"] = float(len(covered))
        metrics["crisis_coverage"] = len(covered) / len(windows) if windows else 0.0
        if not covered:
            return SurvivalReport(
                test_id=self.id,
                passed=not o.require_coverage,
                metrics=metrics,
                notes="no crisis window has data in the dataset window"
                + ("" if o.require_coverage else " (skipped; P35: where data allows)"),
            )
        return SurvivalReport(
            test_id=self.id,
            passed=not failures,
            metrics=metrics,
            notes="; ".join(failures) or f"within limits in {', '.join(covered)}",
        )
