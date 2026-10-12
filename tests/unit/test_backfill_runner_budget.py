"""How the runner picks units against the budget: the next batch leaves room
for the batch still in flight, and a budget that turns out to have room
does not end a phase with work left. No network, no lake."""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from stonks.ingest.backfill.base import BackfillContext, BackfillPhase, Outcome
from stonks.ingest.backfill.budget import CallBudget
from stonks.ingest.backfill.runner import BackfillRunner
from stonks.ingest.backfill.settings import BackfillSettings
from stonks.ingest.backfill.state import BackfillState, Unit, UnitSpec

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


class _ToyPhase(BackfillPhase):
    """Units of two requests each, one call a request."""

    phase_id = "toy"
    order = 1
    title = "Toy"
    batch_size = 2

    def __init__(self, budget: CallBudget, units: int, *, delay: float = 0.0) -> None:
        self._budget = budget
        self._units = units
        self._delay = delay

    def list_units(self, ctx: BackfillContext) -> Iterable[UnitSpec]:
        return [UnitSpec(f"U{i:02d}") for i in range(self._units)]

    def estimate(self, unit: Unit) -> int:
        return 2

    def fetch(self, ctx: BackfillContext, unit: Unit) -> Any:
        time.sleep(self._delay)
        self._budget.before_request("/a", 1)
        time.sleep(self._delay / 4)
        self._budget.before_request("/b", 1)
        return unit.key

    def write(
        self, ctx: BackfillContext, fetched: Sequence[tuple[Unit, Any]]
    ) -> dict[str, Outcome]:
        return {unit.key: Outcome(rows=1) for unit, _ in fetched}


def _runner(state, budget: CallBudget, phase: BackfillPhase) -> tuple[BackfillRunner, Any]:
    bstate = BackfillState(state, clock=lambda: NOW)
    ctx = BackfillContext(
        source=None,  # type: ignore[arg-type]
        lake=None,  # type: ignore[arg-type]
        state=bstate,
        settings=BackfillSettings(network_threads=2),
        make_pipeline=lambda source: None,  # type: ignore[arg-type,return-value]
        clock=lambda: NOW,
    )
    runner = BackfillRunner(
        ctx, [phase], budget, until_budget=True, clock=lambda: NOW, sleep=lambda s: None
    )
    return runner, bstate


def _budget(cls: type[CallBudget] = CallBudget, **kwargs: Any) -> CallBudget:
    return cls(
        daily_budget=kwargs.pop("daily_budget", 1000),
        requests_per_minute=60_000,
        clock=lambda: NOW,
        **kwargs,
    )


def test_the_next_batch_leaves_room_for_the_batch_in_flight(state):
    budget = _budget(run_cap=6)
    runner, bstate = _runner(state, budget, _ToyPhase(budget, 6, delay=0.2))
    report = runner.run()
    assert report.stopped == "cap"
    assert budget.run_spent == 6
    # no unit was cut off half way, so no call went to waste
    cut = state.sql("SELECT unit_key FROM backfill_units WHERE calls > 0 AND status != 'done'")
    assert cut == []
    assert bstate.summary()["toy"].done == 3


class _LateRoom(CallBudget):
    """Says there is no room the first time it is asked, then finds some
    (a vendor reading that came in lower than the local count)."""

    asked = 0

    @property
    def remaining(self) -> int:
        self.asked += 1
        if self.asked == 1:
            return 0
        return CallBudget.remaining.fget(self)  # type: ignore[attr-defined]


def test_room_found_on_a_second_look_does_not_end_the_phase(state):
    budget = _budget(_LateRoom)
    runner, bstate = _runner(state, budget, _ToyPhase(budget, 1))
    report = runner.run()
    assert report.stopped == "finished"
    assert bstate.summary()["toy"].done == 1


def test_every_unit_runs_when_the_budget_is_ample(state):
    units = 5
    budget = _budget()
    runner, bstate = _runner(state, budget, _ToyPhase(budget, units))
    report = runner.run()
    assert report.stopped == "finished"
    assert bstate.summary()["toy"].done == units
    assert budget.run_spent == 2 * units
