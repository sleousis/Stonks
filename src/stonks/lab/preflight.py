"""Lab preflight (BL-37): check the data before a lab run spends compute.

:func:`run_preflight` looks at a :class:`~stonks.lab.dataset.LabDataset`
(and optionally the strategy) and returns a :class:`PreflightReport`.
Two issues are always errors because no run can succeed:

* ``empty_universe``: the dataset names no tickers;
* ``no_data``: no universe ticker has a bar of the dataset interval in
  the window.

Everything else is a warning, and an error with ``strict=True``:

* ``missing_data``: some tickers have no bars in the window;
* ``late_start``: a ticker's first bar is well after the window start
  (the EODHD free tier keeps one year);
* ``insufficient_history``: fewer bars up to the window end than the
  strategy's ``required_history_bars``, so it can never act;
* ``short_warmup``: fewer bars before the window start than
  ``required_history_bars``, so the start of the window is spent warming up;
* ``quarantined_bars``: ingest rejected bars inside the window;
* ``statement_flags``: the statement audit (BL-36) flagged error rows;
* ``static_universe``: a plain ticker list, which carries survivorship
  bias (P14);
* ``membership_gap``: members of the named universe during the window
  are missing from the dataset (often the delisted ones);
* ``unknown_universe`` (always an error): the named universe has no rows;
* ``no_delisted``: a window of five years or more with no delisted name;
* ``zero_costs``: the dataset charges no costs (BL-13);
* ``missing_benchmark``: the benchmark ticker has no bars in the window.

With no lake the preflight is skipped. The checks only read.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

import pandas as pd

from stonks.backtest.benchmark import normalize_spec
from stonks.logging import get_logger
from stonks.strategies.base import strategy_data_tickers

Severity = Literal["error", "warning"]

_log = get_logger("stonks.lab.preflight")

#: A first bar more than this many days after the window start is late.
LATE_START_DAYS = 14
#: Windows at least this long should hold a delisted name.
LONG_WINDOW_YEARS = 5
#: Tickers listed in an issue message before it says "and N more".
_SHOWN = 5


@dataclass(frozen=True)
class PreflightIssue:
    code: str
    severity: Severity
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class PreflightError(RuntimeError):
    """A preflight with errors. ``report`` holds every issue."""

    def __init__(self, report: PreflightReport) -> None:
        self.report = report
        lines = "\n".join(f"- [{i.code}] {i.message}" for i in report.errors)
        super().__init__(f"lab preflight failed:\n{lines}")


@dataclass
class PreflightReport:
    issues: list[PreflightIssue] = field(default_factory=list)
    #: True when the preflight could not look at data (no lake).
    skipped: bool = False

    @property
    def errors(self) -> list[PreflightIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[PreflightIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            raise PreflightError(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "issues": [asdict(i) for i in self.issues],
        }


def _names(tickers: list[str]) -> str:
    shown = ", ".join(tickers[:_SHOWN])
    extra = len(tickers) - _SHOWN
    return f"{shown} and {extra} more" if extra > 0 else shown


def _required_history(strategy: Any) -> int:
    if strategy is None:
        return 0
    value = getattr(strategy, "required_history_bars", 0) or 0
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def run_preflight(
    dataset: Any,
    strategy: Any = None,
    *,
    strict: bool = False,
    universe_id: str | None = None,
) -> PreflightReport:
    """Check ``dataset`` (and ``strategy``'s history needs) before a run.
    ``universe_id`` (default: the dataset's ``universe_id`` attribute, if
    any) names the point-in-time universe the dataset was built from."""
    lake = getattr(dataset, "lake", None)
    if lake is None:
        return PreflightReport(skipped=True)
    if universe_id is None:
        universe_id = getattr(dataset, "universe_id", None)
    issues: list[PreflightIssue] = []

    def add(code: str, message: str, *, fatal: bool = False, **details: Any) -> None:
        severity: Severity = "error" if fatal or strict else "warning"
        issues.append(PreflightIssue(code, severity, message, details))

    universe = list(dict.fromkeys(dataset.universe))
    start, end = dataset.start, dataset.end
    interval = dataset.interval
    if not universe:
        add("empty_universe", "the dataset universe is empty: name at least one ticker", fatal=True)
        return PreflightReport(issues)

    _coverage(lake, universe, start, end, interval, _required_history(strategy), add)
    _quality(lake, universe, start, end, interval, add)
    _membership(lake, universe, start, end, universe_id, add)
    _costs_and_benchmark(lake, dataset, universe, add)
    _references(lake, dataset, strategy, universe, add)

    report = PreflightReport(issues)
    for issue in report.issues:
        _log.info("lab.preflight.issue", code=issue.code, severity=issue.severity)
    return report


def _references(lake: Any, dataset: Any, strategy: Any, universe: list[str], add: Any) -> None:
    """Warn when a ticker the strategy reads but does not trade (RS-01) has
    no bars in the window: the strategy then runs blind or never acts."""
    refs = [
        t
        for t in dict.fromkeys(
            [*getattr(dataset, "reference_tickers", ()), *strategy_data_tickers(strategy)]
        )
        if t not in universe
    ]
    if not refs:
        return
    start, end, interval = dataset.start, dataset.end, dataset.interval
    cov = lake.bar_coverage(refs, interval, start, end)
    covered = set(cov[cov["n_window"] > 0]["ticker"]) if not cov.empty else set()
    missing = [t for t in refs if t not in covered]
    if missing:
        add(
            "missing_reference_data",
            f"the strategy reads {_names(missing)} but they have no {interval} bars between "
            f"{start} and {end}: ingest them, or the strategy runs without its reference",
            tickers=missing,
        )


def _coverage(
    lake: Any,
    universe: list[str],
    start: date,
    end: date,
    interval: Any,
    required: int,
    add: Any,
) -> None:
    cov = lake.bar_coverage(universe, interval, start, end)
    cov = cov[cov["n_window"] > 0] if not cov.empty else cov
    covered = set(cov["ticker"])
    missing = [t for t in universe if t not in covered]
    if not covered:
        add(
            "no_data",
            f"no {interval} bars for any of {_names(universe)} between {start} and {end}: "
            "ingest prices first (stonks ingest prices)",
            fatal=True,
            tickers=missing,
        )
        return
    if missing:
        add(
            "missing_data",
            f"{len(missing)} of {len(universe)} tickers have no {interval} bars between "
            f"{start} and {end}: {_names(missing)}",
            tickers=missing,
        )
    late_cutoff = start + timedelta(days=LATE_START_DAYS)
    late = sorted(
        r.ticker
        for r in cov.itertuples(index=False)
        if pd.Timestamp(r.first_bar).date() > late_cutoff
    )
    if late:
        firsts = {
            r.ticker: str(pd.Timestamp(r.first_bar).date())
            for r in cov.itertuples(index=False)
            if r.ticker in late
        }
        add(
            "late_start",
            f"data for {_names(late)} starts well after the window start {start} "
            "(the EODHD free tier keeps one year of prices)",
            tickers=late,
            first_bars=firsts,
        )
    if required <= 0:
        return
    total = {r.ticker: int(r.n_window) + int(r.n_before) for r in cov.itertuples(index=False)}
    never = sorted(t for t, n in total.items() if n <= required)
    if never:
        add(
            "insufficient_history",
            f"the strategy needs {required} bars of history but {_names(never)} "
            f"have at most {max(total[t] for t in never)} up to {end}, so it can never act",
            tickers=never,
            required_history_bars=required,
        )
    short = sorted(
        r.ticker
        for r in cov.itertuples(index=False)
        if int(r.n_before) < required and r.ticker not in never
    )
    if short:
        add(
            "short_warmup",
            f"{_names(short)} have fewer than {required} bars before {start}, so the "
            "start of the window is spent warming up: start the lake earlier",
            tickers=short,
            required_history_bars=required,
        )


def _quality(
    lake: Any, universe: list[str], start: date, end: date, interval: Any, add: Any
) -> None:
    quarantined = lake.quarantine_counts(universe, interval, start, end)
    if quarantined:
        add(
            "quarantined_bars",
            f"{sum(quarantined.values())} bars in the window were quarantined at ingest "
            f"for {_names(sorted(quarantined))}: those days are missing from the data",
            counts=quarantined,
        )
    flags = lake.get_statement_flags(severity="error")
    if not flags.empty:
        flags = flags[
            flags["ticker"].isin(universe)
            & flags["period_end"].map(lambda d: d is not None and d <= end)
        ]
    if not flags.empty:
        counts = {str(k): int(v) for k, v in flags.groupby("ticker").size().items()}
        add(
            "statement_flags",
            f"the statement audit flagged {len(flags)} periods for {_names(sorted(counts))}: "
            "fundamental strategies should skip them (exclude_flagged=True)",
            counts=counts,
        )


def _membership(
    lake: Any,
    universe: list[str],
    start: date,
    end: date,
    universe_id: str | None,
    add: Any,
) -> None:
    if universe_id is None:
        add(
            "static_universe",
            "the universe is a static ticker list, so results may carry survivorship "
            "bias (P14): load universe_membership and run on a universe id",
        )
    elif universe_id not in lake.universe_ids():
        add(
            "unknown_universe",
            f"unknown universe {universe_id!r}: it has no universe_membership rows",
            fatal=True,
        )
    else:
        members = lake.members_between(universe_id, start, end)
        gap = [t for t in members if t not in set(universe)]
        if gap:
            add(
                "membership_gap",
                f"{len(gap)} members of {universe_id!r} during the window are not in the "
                f"dataset: {_names(gap)}",
                tickers=gap,
            )
    years = (end - start).days / 365.25
    if years >= LONG_WINDOW_YEARS and not lake.delisted_tickers(universe):
        add(
            "no_delisted",
            f"a {years:.1f} year window with no delisted name in the universe is "
            "probably survivors only (P14)",
        )


def _costs_and_benchmark(lake: Any, dataset: Any, universe: list[str], add: Any) -> None:
    from stonks.lab.runner import costs_are_zero  # the one cost test (BL-13)

    if costs_are_zero(getattr(dataset, "costs", None)):
        add(
            "zero_costs",
            "every backtest of this run ignores fees, spread and impact: configure "
            "[backtest.costs] or pass a cost model",
        )
    spec = normalize_spec(getattr(dataset, "benchmark", None))
    if spec is None or spec in ("auto", "ew"):
        return
    cov = lake.bar_coverage([spec], dataset.interval, dataset.start, dataset.end)
    if cov.empty or int(cov["n_window"].sum()) == 0:
        add(
            "missing_benchmark",
            f"benchmark {spec!r} has no {dataset.interval} bars in the window: ingest it "
            "or pick another benchmark",
            benchmark=spec,
        )
