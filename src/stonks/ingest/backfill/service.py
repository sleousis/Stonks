"""Builds a backfill from settings and reports its progress: what the CLI
(``stonks ingest backfill``) calls."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from stonks.ingest.backfill.base import BackfillContext, BackfillPhase
from stonks.ingest.backfill.budget import CallBudget
from stonks.ingest.backfill.registry import select_phases
from stonks.ingest.backfill.runner import BackfillRunner, RunReport
from stonks.ingest.backfill.settings import BackfillSettings
from stonks.ingest.backfill.state import BackfillState, DayCalls
from stonks.ingest.pipeline import IngestPipeline
from stonks.ingest.quality import BarQualityChecker
from stonks.ingest.quality_config import DataQualityConfig
from stonks.ingest.sources.base import DataSource, DataSourceError
from stonks.store.lake import DuckDBLake

#: Plans that serve what the backfill asks for, per source id.
FULL_PLANS: dict[str, frozenset[str]] = {"eodhd": frozenset({"all_in_one"})}

_MEMORY = re.compile(r"^\d+(\.\d+)?\s*(KB|MB|GB|TB|KiB|MiB|GiB|TiB)$", re.IGNORECASE)


class BackfillSetupError(ValueError):
    """The backfill cannot start (plan, schema)."""


def check_plan(source_id: str, plans: dict[str, str]) -> None:
    """Refuse to start on a plan that lacks full history, bulk or intraday:
    the shipped default is the free plan, so a paid install says so."""
    allowed = FULL_PLANS.get(source_id)
    if allowed is None:
        return
    plan = plans.get(source_id, "")
    if plan not in allowed:
        raise BackfillSetupError(
            f"the {source_id} plan is {plan or 'not set'!r}; the backfill needs "
            f"{' or '.join(sorted(allowed))}. Set [ensure.plans] {source_id} in your own "
            "config or STONKS_EODHD_PLAN=all_in_one"
        )


def limit_duckdb(lake: DuckDBLake, settings: BackfillSettings) -> None:
    """Cap DuckDB's memory and threads while the backfill holds the lake."""
    memory = settings.duckdb_memory_limit.strip()
    if not _MEMORY.match(memory):
        raise BackfillSetupError(f"duckdb_memory_limit {memory!r} is not a size like 1GB")
    lake.con.execute(f"SET memory_limit = '{memory}'")
    lake.con.execute(f"SET threads = {int(settings.duckdb_threads)}")


def make_pipeline_factory(
    lake: DuckDBLake,
    quality: DataQualityConfig,
    adjustment_tolerance: float,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Callable[[DataSource], IngestPipeline]:
    """Pipelines over replayed rows: the usual quality checks, no fallback
    source (it would refetch from another vendor) and no alerts (a run of
    days would send one per batch)."""
    from stonks.scheduling.calendar import TickerSessionCalendar

    checker = BarQualityChecker(quality, calendar=TickerSessionCalendar())

    def make(source: DataSource) -> IngestPipeline:
        return IngestPipeline(
            source,
            lake,
            quality=checker,
            fallback=None,
            notifier=None,
            clock=clock,
            adjustment_tolerance=adjustment_tolerance,
        )

    return make


def free_source_factory(cfg: Any) -> Callable[[str], DataSource]:
    """Builds the free sources the unmetered phases read from
    ``[sources.*]`` (``SourcesConfig``): ``edgar`` (raises until
    ``[sources.edgar] user_agent`` is set), ``ibkr_borrow`` and
    ``defillama``. None of them is metered against the EODHD budget."""

    def build(source_id: str) -> DataSource:
        if source_id == "edgar":
            from stonks.ingest.sources.edgar import EdgarDataSource

            return EdgarDataSource.from_config(cfg.edgar)
        if source_id == "ibkr_borrow":
            from stonks.ingest.sources.ibkr_borrow import IbkrBorrowDataSource

            return IbkrBorrowDataSource.from_config(cfg.ibkr_borrow)
        if source_id == "defillama":
            from stonks.ingest.sources.registry import build_source

            return build_source("defillama", cfg)
        raise DataSourceError(f"no free source {source_id!r} for the backfill")

    return build


def build_runner(
    *,
    source: DataSource,
    lake: DuckDBLake,
    state: BackfillState,
    settings: BackfillSettings,
    make_pipeline: Callable[[DataSource], IngestPipeline],
    phases: Collection[str] | None = None,
    exchanges: Collection[str] | None = None,
    max_calls: int | None = None,
    until_budget: bool = False,
    daily_budget: int | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    sources: Callable[[str], DataSource] | None = None,
) -> tuple[BackfillRunner, CallBudget]:
    """A runner and its budget, with the budget metering ``source``."""
    clock = clock or (lambda: datetime.now(UTC))
    sleep = sleep or time.sleep
    budget = CallBudget(
        daily_budget=daily_budget or settings.daily_budget,
        usage=source.fetch_usage,
        spent_today=lambda day: state.calls_on(day).calls,
        run_cap=max_calls,
        requests_per_minute=settings.requests_per_minute,
        usage_check_seconds=settings.usage_check_seconds,
        reset_margin=timedelta(minutes=settings.reset_margin_minutes),
        clock=clock,
        sleep=sleep,
    )
    source.set_request_meter(budget)
    ctx = BackfillContext(
        source=source,
        lake=lake,
        state=state,
        settings=settings,
        make_pipeline=make_pipeline,
        exchanges=frozenset(e.upper() for e in exchanges) if exchanges else None,
        clock=clock,
        sources=sources,
    )
    runner = BackfillRunner(
        ctx, select_phases(phases), budget, until_budget=until_budget, clock=clock, sleep=sleep
    )
    return runner, budget


# ---- status --------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseStatus:
    phase: str
    title: str
    listed: bool
    done: int
    failed: int
    retry: int
    pending: int
    calls: int
    rows: int
    #: Calls the remaining units should take (``None``: not listed yet).
    calls_left: int | None

    @property
    def remaining(self) -> int:
        return self.pending + self.retry


@dataclass(frozen=True)
class BackfillStatus:
    phases: list[PhaseStatus]
    today: DayCalls
    daily_budget: int

    @property
    def calls_left(self) -> int:
        return sum(p.calls_left or 0 for p in self.phases)

    @property
    def eta_days(self) -> float:
        return self.calls_left / self.daily_budget if self.daily_budget else float("inf")


def _average_cost(
    phase: BackfillPhase, done: int, calls: int, settings: BackfillSettings, now: datetime
) -> float:
    if done > 0 and calls > 0:
        return calls / done
    return phase.eta_cost(settings, now.date())


def backfill_status(
    state: BackfillState,
    settings: BackfillSettings,
    *,
    phases: Collection[str] | None = None,
    today: datetime | None = None,
) -> BackfillStatus:
    """Per phase: units done, failed, waiting to retry and left, calls spent
    and the calls left at the average cost so far; today's calls; and the
    days left at the daily budget. Reads the state only (no network)."""
    summary = state.summary()
    now = today or datetime.now(UTC)
    rows: list[PhaseStatus] = []
    for phase in select_phases(phases):
        s = summary.get(phase.phase_id)
        if s is None or s.listed_at is None:
            rows.append(PhaseStatus(phase.phase_id, phase.title, False, 0, 0, 0, 0, 0, 0, None))
            continue
        per_unit = _average_cost(phase, s.done, s.done_calls, settings, now)
        rows.append(
            PhaseStatus(
                phase=phase.phase_id,
                title=phase.title,
                listed=True,
                done=s.done,
                failed=s.failed,
                retry=s.retry,
                pending=s.pending,
                calls=s.calls,
                rows=s.rows,
                calls_left=round(per_unit * s.remaining),
            )
        )
    return BackfillStatus(
        phases=rows, today=state.calls_on(now.date()), daily_budget=settings.daily_budget
    )


__all__ = [
    "BackfillSetupError",
    "BackfillStatus",
    "PhaseStatus",
    "RunReport",
    "backfill_status",
    "build_runner",
    "check_plan",
    "free_source_factory",
    "limit_duckdb",
    "make_pipeline_factory",
]
