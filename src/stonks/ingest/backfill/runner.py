"""Runs backfill phases in order until the work or the budget runs out.

For each phase the runner lists its units (when the listing is stale),
then takes batches of runnable units. A batch is fetched on at most
``network_threads`` threads while the previous batch is written on the
runner's thread, so at most two batches sit in memory.

Each unit ends its turn in one of four ways:

- done (or a step done, with a cursor to resume at);
- a soft failure: the attempt is counted and the unit waits, doubling the
  wait each time, until ``max_attempts`` marks it failed;
- not found (HTTP 404): done with nothing stored, never asked again;
- stopped by the budget or the vendor's quota: left as it was, so the next
  run picks it up. The run then sleeps until the budget refills, or
  returns when ``until_budget`` is set so a scheduler can rerun it.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import requests

from stonks.ingest.backfill.base import BackfillContext, BackfillPhase, Outcome
from stonks.ingest.backfill.budget import BudgetExhausted, CallBudget
from stonks.ingest.backfill.state import Unit
from stonks.ingest.pipeline import _SOFT_FAIL_EXCEPTIONS
from stonks.ingest.redact import format_exception
from stonks.ingest.sources.base import QuotaExceededError
from stonks.logging import get_logger

_log = get_logger("stonks.ingest.backfill.runner")

StopReason = Literal["finished", "budget", "cap", "waiting"]

#: Longest single sleep, so a stop request is noticed between naps.
_NAP_SECONDS = 300.0


@dataclass
class RunReport:
    stopped: StopReason = "finished"
    detail: str = ""
    units_done: int = 0
    units_failed: int = 0
    units_retry: int = 0
    calls: int = 0
    phases: dict[str, int] = field(default_factory=dict)


@dataclass
class _Fetched:
    unit: Unit
    payload: Any = None
    error: BaseException | None = None
    calls: int = 0


class BackfillRunner:
    def __init__(
        self,
        ctx: BackfillContext,
        phases: Sequence[BackfillPhase],
        budget: CallBudget,
        *,
        until_budget: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ctx = ctx
        self._phases = list(phases)
        self._budget = budget
        self._until_budget = until_budget
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(UTC))
        self._report = RunReport()
        self._exchanges = sorted(ctx.exchanges) if ctx.exchanges is not None else None

    # ---- the loop -----------------------------------------------------------------

    def run(self) -> RunReport:
        while True:
            try:
                for phase in self._phases:
                    self._run_phase(phase)
            except BudgetExhausted as exc:
                self._flush_calls()
                if exc.resets_at is None:
                    return self._stop("cap", exc.reason)
                if self._until_budget:
                    return self._stop("budget", exc.reason)
                _log.info("backfill.sleeping", reason=exc.reason, until=exc.resets_at.isoformat())
                self._sleep_until(exc.resets_at)
                self._budget.refresh()
                continue
            self._flush_calls()
            retry_at = self._ctx.state.next_retry_at(
                [p.phase_id for p in self._phases], self._exchanges
            )
            if retry_at is None:
                return self._stop("finished", "no work left")
            if self._until_budget:
                return self._stop("waiting", f"retries wait until {retry_at.isoformat()}")
            _log.info("backfill.waiting_for_retries", until=retry_at.isoformat())
            self._sleep_until(retry_at)

    def _stop(self, reason: StopReason, detail: str) -> RunReport:
        self._report.stopped = reason
        self._report.detail = detail
        self._report.calls = self._budget.run_spent
        _log.info(
            "backfill.stopped",
            reason=reason,
            detail=detail,
            calls=self._report.calls,
            done=self._report.units_done,
            failed=self._report.units_failed,
        )
        return self._report

    def _sleep_until(self, moment: datetime) -> None:
        while True:
            left = (moment - self._clock()).total_seconds()
            if left <= 0:
                return
            self._sleep(min(left, _NAP_SECONDS))

    # ---- one phase ----------------------------------------------------------------

    def _run_phase(self, phase: BackfillPhase) -> None:
        ctx = self._ctx
        why_not = phase.ready(ctx)
        if why_not is not None:
            _log.info("backfill.phase_skipped", phase=phase.phase_id, reason=why_not)
            return
        listed = ctx.state.listed_at(phase.phase_id)
        ttl = phase.listing_ttl(ctx)
        stale = listed is None or ttl is None or self._clock() - listed >= ttl
        if stale:
            added = ctx.state.add_units(phase.phase_id, phase.list_units(ctx))
            self._flush_calls()
            _log.info("backfill.phase_listed", phase=phase.phase_id, new_units=added)
        workers = ctx.settings.network_threads
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="backfill") as pool:
            current = self._submit(pool, phase, exclude=())
            while current:
                stop: BudgetExhausted | None = None
                try:
                    following = self._submit(pool, phase, exclude=[f.unit.key for f, _ in current])
                except BudgetExhausted as exc:
                    following, stop = [], exc
                try:
                    self._finish(phase, self._gather(current))
                except BudgetExhausted:
                    # the batch already in flight is still stored before stopping
                    if following:
                        with contextlib.suppress(BudgetExhausted):
                            self._finish(phase, self._gather(following))
                    raise
                if stop is not None:
                    raise stop
                # units that ran a step are runnable again once stored
                current = following or self._submit(pool, phase, exclude=())

    def _submit(
        self, pool: ThreadPoolExecutor, phase: BackfillPhase, exclude: Sequence[str]
    ) -> list[tuple[_Fetched, Future[_Fetched]]]:
        units = self._ctx.state.next_units(
            phase.phase_id, phase.batch_size, exchanges=self._exchanges, exclude=exclude
        )
        if not units:
            return []
        # take as many units as fit the budget; none fitting stops the run
        room = self._budget.remaining
        chosen: list[Unit] = []
        spend = 0
        for unit in units:
            cost = phase.estimate(unit)
            if spend + cost > room:
                break
            chosen.append(unit)
            spend += cost
        if not chosen:
            self._budget.ensure_room(phase.estimate(units[0]))
            return []
        return [(_Fetched(u), pool.submit(self._fetch_one, phase, u)) for u in chosen]

    def _fetch_one(self, phase: BackfillPhase, unit: Unit) -> _Fetched:
        out = _Fetched(unit)
        with self._budget.track() as tracker:
            try:
                out.payload = phase.fetch(self._ctx, unit)
            except BaseException as exc:  # sorted out on the runner's thread
                out.error = exc
        out.calls = tracker.calls
        return out

    def _gather(self, batch: list[tuple[_Fetched, Future[_Fetched]]]) -> list[_Fetched]:
        return [future.result() for _, future in batch]

    def _finish(self, phase: BackfillPhase, fetched: list[_Fetched]) -> None:
        state = self._ctx.state
        stop: BaseException | None = None
        ok: list[_Fetched] = []
        for item in fetched:
            exc = item.error
            if exc is None:
                ok.append(item)
                continue
            if isinstance(exc, BudgetExhausted | QuotaExceededError):
                if isinstance(exc, QuotaExceededError):
                    self._budget.exhaust()
                state.add_unit_calls(item.unit, item.calls)
                stop = stop or exc
            elif _not_found(exc):
                state.mark_done(item.unit, calls=item.calls, rows=0, note="not found")
                self._count(phase, "done")
            elif isinstance(exc, _SOFT_FAIL_EXCEPTIONS):
                self._fail(phase, item.unit, format_exception(exc), item.calls)
            elif isinstance(exc, Exception):
                _log.error(
                    "backfill.unit_crashed",
                    phase=phase.phase_id,
                    unit=item.unit.key,
                    error=format_exception(exc),
                )
                self._fail(phase, item.unit, format_exception(exc), item.calls)
            else:
                raise exc  # KeyboardInterrupt, SystemExit
        if ok:
            outcomes = phase.write(self._ctx, [(i.unit, i.payload) for i in ok])
            for item in ok:
                outcome = outcomes.get(item.unit.key, Outcome())
                self._record(phase, item, outcome)
        self._flush_calls()
        if stop is not None:
            if isinstance(stop, BudgetExhausted):
                raise stop
            raise BudgetExhausted(
                f"the vendor refused a request: {stop}", self._budget.resets_at
            ) from stop

    def _record(self, phase: BackfillPhase, item: _Fetched, outcome: Outcome) -> None:
        state = self._ctx.state
        if outcome.error is not None:
            self._fail(phase, item.unit, outcome.error, item.calls)
        elif not outcome.finished and outcome.cursor is not None:
            state.mark_progress(
                item.unit, cursor=outcome.cursor, calls=item.calls, rows=outcome.rows
            )
        else:
            state.mark_done(item.unit, calls=item.calls, rows=outcome.rows, note=outcome.note)
            self._count(phase, "done")

    def _fail(self, phase: BackfillPhase, unit: Unit, error: str, calls: int) -> None:
        settings = self._ctx.settings
        status = self._ctx.state.mark_failure(
            unit,
            error=error,
            calls=calls,
            max_attempts=settings.max_attempts,
            backoff=timedelta(minutes=settings.retry_backoff_minutes),
        )
        _log.warning(
            "backfill.unit_failed", phase=phase.phase_id, unit=unit.key, status=status, error=error
        )
        self._count(phase, "failed" if status == "failed" else "retry")

    def _count(self, phase: BackfillPhase, status: str) -> None:
        report = self._report
        if status == "done":
            report.units_done += 1
            report.phases[phase.phase_id] = report.phases.get(phase.phase_id, 0) + 1
        elif status == "failed":
            report.units_failed += 1
        else:
            report.units_retry += 1

    def _flush_calls(self) -> None:
        state = self._ctx.state
        for item in self._budget.flush():
            state.add_calls(item.day, item.calls)
            for used, limit, read_at in item.readings:
                state.record_vendor_usage(item.day, used, limit, read_at)


def _not_found(exc: BaseException) -> bool:
    return (
        isinstance(exc, requests.HTTPError)
        and exc.response is not None
        and exc.response.status_code == 404
    )
