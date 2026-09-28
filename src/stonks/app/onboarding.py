"""OnboardingService: the first-run guide (roadmap 13.2).

A new install or a new trader is walked through a few plain steps:

1. ``account``: sign in and set up the second factor,
2. ``portfolio``: pick or open a portfolio,
3. ``data``: choose the data to watch (a watchlist, or a universe),
4. ``follow``: follow a strategy for signals or paper trading,
5. ``alerts``: turn on push alerts on a device.

Each step is **done** when the data already shows it (a second factor, a
portfolio, a watchlist, a subscription, a push device) or when the person
marked it done, **skipped** when they skipped it, else **todo**. Progress is
stored per user (``onboarding_steps``, migration 026), so it follows the
person across devices. Closing the guide stores ``dismissed_at``.

Admins also get a system checklist: a data source key, a first ingest,
strategies to follow (the starter set, ``POST /api/starter/install``), a
backup on disk and a running scheduler. It is read only: each item says
what to do, and the console links to the page that does it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, get_args

from pydantic import BaseModel, ConfigDict

from stonks.accounts.audit import iso_now
from stonks.app.context import AppContext
from stonks.app.errors import ValidationError
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.logging import get_logger
from stonks.store.state import SqliteState

if TYPE_CHECKING:
    from stonks.app.ingest import DataSourceInfo

_log = get_logger("stonks.app.onboarding")

StepId = Literal["account", "portfolio", "data", "follow", "alerts"]
StepState = Literal["done", "skipped", "todo"]
SystemCheckId = Literal["data_source", "first_ingest", "strategies", "backup", "scheduler"]

STEPS: tuple[StepId, ...] = get_args(StepId)


class OnboardingStepView(BaseModel):
    id: StepId
    state: StepState
    #: True when the data shows the step done (it cannot be undone here).
    derived: bool


class OnboardingView(BaseModel):
    steps: list[OnboardingStepView]
    #: Every step is done or skipped.
    complete: bool
    #: The person closed the guide.
    dismissed: bool
    #: The console should offer the guide: not complete and not dismissed.
    show: bool


class StepUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: ``todo`` clears a stored skip or done mark.
    state: StepState


class OnboardingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dismissed: bool


class SystemCheckView(BaseModel):
    id: SystemCheckId
    done: bool
    #: One plain line: what was found, or what is missing.
    detail: str


class SystemChecklistView(BaseModel):
    checks: list[SystemCheckView]
    complete: bool


def _person(principal: Principal) -> str:
    if principal.scope.is_service:
        raise ValidationError("the first-run guide is for people, not services")
    return principal.user_id


class OnboardingService:
    def __init__(self, context: AppContext, *, sources: Callable[[], list[DataSourceInfo]]) -> None:
        self._ctx = context
        self._sources = sources

    # ---- the trader's steps --------------------------------------------------

    def get(self, principal: Principal) -> OnboardingView:
        require(principal, Permission.READ)
        user_id = _person(principal)
        with self._ctx.state() as state:
            return self._view(state, user_id)

    def set_step(self, principal: Principal, step: str, body: StepUpdate) -> OnboardingView:
        require(principal, Permission.READ)
        user_id = _person(principal)
        if step not in STEPS:
            raise ValidationError(f"step must be one of {list(STEPS)}, got {step!r}")
        with self._ctx.state() as state:
            with state.transaction():
                if body.state == "todo":
                    state.execute(
                        "DELETE FROM onboarding_steps WHERE user_id = ? AND step = ?",
                        [user_id, step],
                    )
                else:
                    state.execute(
                        "INSERT INTO onboarding_steps (user_id, step, state, updated_at)"
                        " VALUES (?, ?, ?, ?) ON CONFLICT (user_id, step) DO UPDATE SET"
                        " state = excluded.state, updated_at = excluded.updated_at",
                        [user_id, step, body.state, iso_now()],
                    )
            _log.info("onboarding.step", user_id=user_id, step=step, state=body.state)
            return self._view(state, user_id)

    def update(self, principal: Principal, body: OnboardingUpdate) -> OnboardingView:
        require(principal, Permission.READ)
        user_id = _person(principal)
        with self._ctx.state() as state:
            with state.transaction():
                state.execute(
                    "INSERT INTO onboarding_status (user_id, dismissed_at) VALUES (?, ?)"
                    " ON CONFLICT (user_id) DO UPDATE SET dismissed_at = excluded.dismissed_at",
                    [user_id, iso_now() if body.dismissed else None],
                )
            return self._view(state, user_id)

    def _view(self, state: SqliteState, user_id: str) -> OnboardingView:
        derived = _derived(state, user_id)
        stored = {
            r["step"]: r["state"]
            for r in state.sql(
                "SELECT step, state FROM onboarding_steps WHERE user_id = ?", [user_id]
            )
        }
        steps = [
            OnboardingStepView(
                id=s,
                state="done" if derived[s] else stored.get(s, "todo"),
                derived=derived[s],
            )
            for s in STEPS
        ]
        dismissed_rows = state.sql(
            "SELECT dismissed_at FROM onboarding_status WHERE user_id = ?", [user_id]
        )
        dismissed = bool(dismissed_rows and dismissed_rows[0]["dismissed_at"])
        complete = all(s.state != "todo" for s in steps)
        return OnboardingView(
            steps=steps, complete=complete, dismissed=dismissed, show=not (complete or dismissed)
        )

    # ---- the admin's system checklist -----------------------------------------

    def system(self, principal: Principal, *, now: datetime | None = None) -> SystemChecklistView:
        """Read-only install checks for admins (``operations.run``)."""
        require(principal, Permission.OPERATIONS_RUN)
        checks = [
            self._data_source(),
            self._first_ingest(),
            self._strategies(),
            self._backup(),
            self._scheduler(now),
        ]
        return SystemChecklistView(checks=checks, complete=all(c.done for c in checks))

    def _data_source(self) -> SystemCheckView:
        sources = self._sources()
        default = next((s for s in sources if s.default), None)
        ready = [s.id for s in sources if s.configured]
        if default is not None and default.configured:
            return SystemCheckView(
                id="data_source", done=True, detail=f"{default.id} is set up and is the default"
            )
        detail = default.detail if default is not None and default.detail else "no source is set up"
        if ready:
            detail += f". Ready: {', '.join(ready)}"
        return SystemCheckView(id="data_source", done=False, detail=detail)

    def _first_ingest(self) -> SystemCheckView:
        with self._ctx.lake() as lake:
            rows = lake.sql(
                "SELECT COUNT(*) AS n, MAX(finished_at) AS last FROM ingest_runs"
                " WHERE tickers_ok > 0"
            )
        n = int(rows.iloc[0]["n"]) if len(rows) else 0
        if n == 0:
            return SystemCheckView(id="first_ingest", done=False, detail="no prices loaded yet")
        return SystemCheckView(
            id="first_ingest", done=True, detail=f"{n} {_plural(n, 'data load')} with prices"
        )

    def _strategies(self) -> SystemCheckView:
        with self._ctx.state() as state:
            rows = state.sql(
                "SELECT status, COUNT(*) AS n FROM strategies"
                " WHERE status != 'retired' GROUP BY status"
            )
        counts = {r["status"]: int(r["n"]) for r in rows}
        if not counts:
            return SystemCheckView(
                id="strategies",
                done=False,
                detail="no strategy yet: install the starter set to put three simple ones on trial",
            )
        parts = [
            f"{counts[s]} {label}"
            for s, label in (("active", "approved"), ("shadow", "on trial"))
            if counts.get(s)
        ]
        return SystemCheckView(id="strategies", done=True, detail=", ".join(parts))

    def _backup(self) -> SystemCheckView:
        from stonks.ops.backup import configured_target

        try:
            found = configured_target(self._ctx.settings).list()
        except Exception as exc:  # a missing or unreadable folder is "not yet"
            _log.warning("onboarding.backup_list_failed", error_type=type(exc).__name__)
            found = []
        if not found:
            return SystemCheckView(id="backup", done=False, detail="no backup on disk yet")
        n = len(found)
        return SystemCheckView(id="backup", done=True, detail=f"{n} {_plural(n, 'backup')} on disk")

    def _scheduler(self, now: datetime | None) -> SystemCheckView:
        from stonks.scheduling.metrics import scheduler_liveness
        from stonks.scheduling.runs import RunStore

        store = RunStore(self._ctx.settings.state.path)
        probe = scheduler_liveness(
            store,
            now=now or datetime.now(UTC),
            max_silence=timedelta(minutes=15),
        )
        return SystemCheckView(
            id="scheduler", done=probe.ok, detail=probe.checks.get("scheduler", "")
        )


def _plural(n: int, word: str) -> str:
    return word if n == 1 else f"{word}s"


def _derived(state: SqliteState, user_id: str) -> dict[StepId, bool]:
    """Which steps the data already shows done."""

    def has(query: str) -> bool:
        return bool(state.sql(query, [user_id]))

    return {
        "account": has("SELECT 1 FROM users WHERE id = ? AND mfa_enrolled_at IS NOT NULL"),
        "portfolio": has(
            "SELECT 1 FROM portfolios WHERE owner_id = ? AND status != 'archived' LIMIT 1"
        ),
        "data": has("SELECT 1 FROM watchlists WHERE owner_id = ? LIMIT 1"),
        "follow": has("SELECT 1 FROM subscriptions WHERE user_id = ? AND enabled = 1 LIMIT 1"),
        "alerts": has(
            "SELECT 1 FROM push_subscriptions WHERE user_id = ? AND revoked_at IS NULL LIMIT 1"
        ),
    }
