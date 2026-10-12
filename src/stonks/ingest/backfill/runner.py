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
  returns when ``until_budget`` is set so a scheduler can rerun it;
- refused for going too fast (HTTP 429 after the source's own retries):
  left as it was, no attempt counted, and the run pauses a minute (twice
  that after each refusal in a row, up to 15 minutes) before going on.

A phase whose listing fails is skipped for this pass and listed again on
the next run. A write that raises is split per unit, so only the unit
that cannot be stored fails. The calls spent are stored however the run
ends, an interrupt included.
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
#: First pause after the vendor refuses requests for going too fast, and the
#: longest one (the pause doubles with each refusal in a row).
_RATE_PAUSE_SECONDS = 60.0
_RATE_PAUSE_MAX_SECONDS = 900.0


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
    #: Counts the calls while the fetch runs (``None``: not started yet).
    tracker: Any = None


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
        self._rate_pauses = 0
        #: Phases whose listing failed in the current pass.
        self._unlisted: list[str] = []
        self._exchanges = sorted(ctx.exchanges) if ctx.exchanges is not None else None

    # ---- the loop -----------------------------------------------------------------

    def run(self) -> RunReport:
        try:
            return self._loop()
        finally:
            # an interrupt or a crash still stores the calls already spent
            self._flush_calls()

    def _loop(self) -> RunReport:
        while True:
            self._unlisted = []
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
                detail = "no work left"
                if self._unlisted:
                    detail += f", listing failed for {', '.join(self._unlisted)} (next run)"
                return self._stop("finished", detail)
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
        if stale and not self._list(phase):
            return
        workers = ctx.settings.network_threads
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="backfill") as pool:
            current = self._submit(pool, phase, exclude=())
            while current:
                stop: BudgetExhausted | None = None
                try:
                    following = self._submit(
                        pool, phase, exclude=[f.unit.key for f, _ in current], in_flight=current
                    )
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

    def _list(self, phase: BackfillPhase) -> bool:
        """List the phase's units. A listing the vendor refuses for the day
        stops the run. One that fails otherwise skips the phase this pass."""
        ctx = self._ctx
        try:
            added = ctx.state.add_units(phase.phase_id, phase.list_units(ctx))
        except QuotaExceededError as exc:
            self._budget.exhaust()
            self._flush_calls()
            raise BudgetExhausted(
                f"the vendor refused a request: {exc}", self._budget.resets_at
            ) from exc
        except _SOFT_FAIL_EXCEPTIONS as exc:
            self._flush_calls()
            _log.warning(
                "backfill.listing_failed", phase=phase.phase_id, error=format_exception(exc)
            )
            self._unlisted.append(phase.phase_id)
            return False
        self._flush_calls()
        _log.info("backfill.phase_listed", phase=phase.phase_id, new_units=added)
        return True

    def _submit(
        self,
        pool: ThreadPoolExecutor,
        phase: BackfillPhase,
        exclude: Sequence[str],
        in_flight: Sequence[tuple[_Fetched, Future[_Fetched]]] = (),
    ) -> list[tuple[_Fetched, Future[_Fetched]]]:
        units = self._ctx.state.next_units(
            phase.phase_id, phase.batch_size, exchanges=self._exchanges, exclude=exclude
        )
        if not units:
            return []
        # what the batch in flight may still spend is not charged yet
        reserved = 0
        for item, future in in_flight:
            if not future.done():
                spent = item.tracker.calls if item.tracker is not None else 0
                reserved += max(0, phase.estimate(item.unit) - spent)
        # take as many units as fit the budget
        room = self._budget.remaining - reserved
        chosen: list[Unit] = []
        spend = 0
        for unit in units:
            cost = phase.estimate(unit)
            if spend + cost > room:
                break
            chosen.append(unit)
            spend += cost
        if not chosen:
            if reserved:
                return []  # decided again once the batch in flight is stored
            # no room stops the run, room found on a second look goes on
            self._budget.ensure_room(phase.estimate(units[0]))
            chosen = units[:1]
        return [(_Fetched(u), pool.submit(self._fetch_one, phase, u)) for u in chosen]

    def _fetch_one(self, phase: BackfillPhase, unit: Unit) -> _Fetched:
        out = _Fetched(unit)
        with self._budget.track() as tracker:
            out.tracker = tracker
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
        rate_limited = False
        ok: list[_Fetched] = []
        for item in fetched:
            exc = item.error
            if exc is None:
                ok.append(item)
                continue
            if _rate_limited(exc):
                # going too fast is not the unit's fault: no attempt counted
                state.add_unit_calls(item.unit, item.calls)
                rate_limited = True
            elif isinstance(exc, BudgetExhausted | QuotaExceededError):
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
            outcomes = self._write(phase, ok)
            for item in ok:
                outcome = outcomes.get(item.unit.key, Outcome())
                self._record(phase, item, outcome)
        self._flush_calls()
        if stop is None:
            self._pace(rate_limited)
        if stop is not None:
            if isinstance(stop, BudgetExhausted):
                raise stop
            raise BudgetExhausted(
                f"the vendor refused a request: {stop}", self._budget.resets_at
            ) from stop

    def _write(self, phase: BackfillPhase, ok: list[_Fetched]) -> dict[str, Outcome]:
        """Store the fetched units. A write that raises is tried again one
        unit at a time, so only the unit that cannot be stored fails (and
        a poison unit cannot stop every later run)."""
        try:
            return phase.write(self._ctx, [(i.unit, i.payload) for i in ok])
        except Exception as exc:
            if len(ok) == 1:
                return {ok[0].unit.key: self._write_failed(phase, ok[0], exc)}
        outcomes: dict[str, Outcome] = {}
        for item in ok:
            try:
                outcomes.update(phase.write(self._ctx, [(item.unit, item.payload)]))
            except Exception as exc:
                outcomes[item.unit.key] = self._write_failed(phase, item, exc)
        return outcomes

    def _write_failed(self, phase: BackfillPhase, item: _Fetched, exc: Exception) -> Outcome:
        error = format_exception(exc)
        _log.error("backfill.write_failed", phase=phase.phase_id, unit=item.unit.key, error=error)
        return Outcome(error=error)

    def _pace(self, rate_limited: bool) -> None:
        """Pause after the vendor refused requests for going too fast."""
        if not rate_limited:
            self._rate_pauses = 0
            return
        self._rate_pauses += 1
        pause = min(_RATE_PAUSE_SECONDS * 2 ** (self._rate_pauses - 1), _RATE_PAUSE_MAX_SECONDS)
        _log.warning("backfill.rate_limited", pause_seconds=pause)
        self._sleep(pause)

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


def _rate_limited(exc: BaseException) -> bool:
    return (
        isinstance(exc, requests.HTTPError)
        and exc.response is not None
        and exc.response.status_code == 429
    )


def _not_found(exc: BaseException) -> bool:
    return (
        isinstance(exc, requests.HTTPError)
        and exc.response is not None
        and exc.response.status_code == 404
    )
