"""ScreenAlertService: a saved screen that runs on a schedule and tells you
about names that newly match (roadmap 23.17).

The ``screen_alerts`` scheduler job runs every due alert after the prices
are ingested (:mod:`stonks.screener.alerts`) and sends what it finds
through the notification router, in the ``screen_alert`` category. Your
channels, quiet hours and the category switch in Settings apply. It only
notifies: nothing is traded.

- Every call is scoped to the caller: another person's screen reads as
  missing (404), admins included.
- Reading needs ``data.read``. Turning an alert on, changing it and
  turning it off need ``notifications.manage`` (like price alerts).
- Running every alert now (``evaluate``) is an operator action
  (``operations.run``), used by the scheduler's API backend.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.accounts.audit import iso_now
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError
from stonks.app.pagination import Page
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.notify.events import Event
from stonks.screener import ScreenSpec, run_screen
from stonks.screener.alerts import ScreenAlertRunSummary, run_screen_alerts
from stonks.screener.settings import ScreenerSettings
from stonks.store.state import SqliteState

Cadence = Literal["daily", "weekly"]


class ScreenAlertView(BaseModel):
    screen_id: str
    screen_name: str
    enabled: bool
    cadence: Cadence
    #: Weekly alerts: the day they run, 0 = Monday.
    weekday: int | None
    #: The last day the alert ran on (``None``: not yet, the first run sets
    #: the baseline and sends nothing).
    last_as_of: date | None
    #: Why the last run failed, if it did.
    last_error: str | None
    #: Names the screen matched on its last run.
    matched: int
    created_at: datetime
    updated_at: datetime


class ScreenAlertSet(BaseModel):
    """Turn an alert on for one of your saved screens, or change it."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    cadence: Cadence = "daily"
    weekday: int | None = Field(
        default=None, ge=0, le=6, description="Weekly: the day it runs, 0 = Monday."
    )

    @model_validator(mode="after")
    def _weekday(self) -> Self:
        if self.cadence == "weekly" and self.weekday is None:
            raise ValueError("a weekly alert needs a weekday (0 = Monday)")
        if self.cadence == "daily" and self.weekday is not None:
            raise ValueError("a daily alert takes no weekday")
        return self


class ScreenAlertEventView(BaseModel):
    id: int
    screen_id: str
    screen_name: str | None
    as_of: date
    #: The names that newly matched.
    tickers: list[str]
    #: Names the screen matched in all that day.
    matched: int
    created_at: datetime


class ScreenAlertRunView(BaseModel):
    as_of: date
    alerts: int
    ran: int
    baselines: int
    fired: int
    published: int
    failed: int


def _view(row: sqlite3.Row) -> ScreenAlertView:
    r = dict(row)
    return ScreenAlertView(
        screen_id=r["screen_id"],
        screen_name=r["screen_name"],
        enabled=bool(r["enabled"]),
        cadence=r["cadence"],
        weekday=r["weekday"],
        last_as_of=date.fromisoformat(r["last_as_of"]) if r["last_as_of"] else None,
        last_error=r["last_error"],
        matched=int(r["matched"]),
        created_at=datetime.fromisoformat(r["created_at"]),
        updated_at=datetime.fromisoformat(r["updated_at"]),
    )


_SELECT = (
    "SELECT a.*, s.name AS screen_name,"
    " (SELECT COUNT(*) FROM screen_alert_matches m WHERE m.screen_id = a.screen_id) AS matched"
    " FROM screen_alerts a JOIN screens s ON s.id = a.screen_id"
)


class ScreenAlertService:
    def __init__(
        self,
        context: AppContext,
        *,
        publisher: Callable[[SqliteState], Callable[[Event], Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ctx = context
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(UTC))

    # ---- alerts ------------------------------------------------------------------------

    def list(self, principal: Principal) -> list[ScreenAlertView]:
        """Your screen alerts, oldest first."""
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            rows = state.sql(
                f"{_SELECT} WHERE a.owner_id = ? ORDER BY a.created_at, a.screen_id",
                [principal.user_id],
            )
        return [_view(r) for r in rows]

    def get(self, principal: Principal, screen_id: str) -> ScreenAlertView:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            return self._alert(state, principal, screen_id)

    def set(self, principal: Principal, screen_id: str, body: ScreenAlertSet) -> ScreenAlertView:
        """Turn the alert on (or change it). The matches it has seen stay, so
        changing the cadence does not alert about every name again."""
        require(principal, Permission.NOTIFICATIONS_MANAGE)
        now = iso_now()
        with self._ctx.state() as state, state.transaction():
            self._screen(state, principal, screen_id)
            state.execute(
                "INSERT INTO screen_alerts (screen_id, owner_id, enabled, cadence, weekday,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (screen_id) DO UPDATE SET enabled = excluded.enabled,"
                " cadence = excluded.cadence, weekday = excluded.weekday,"
                " updated_at = excluded.updated_at",
                [
                    screen_id,
                    principal.user_id,
                    int(body.enabled),
                    body.cadence,
                    body.weekday,
                    now,
                    now,
                ],
            )
            return self._alert(state, principal, screen_id)

    def delete(self, principal: Principal, screen_id: str) -> None:
        """Turn the alert off for good: its matches go too, so a new alert
        starts from a fresh baseline. Past events stay in the history."""
        require(principal, Permission.NOTIFICATIONS_MANAGE)
        with self._ctx.state() as state, state.transaction():
            self._alert(state, principal, screen_id)
            state.execute("DELETE FROM screen_alerts WHERE screen_id = ?", [screen_id])

    def events(
        self, principal: Principal, *, screen_id: str | None, limit: int, offset: int
    ) -> Page[ScreenAlertEventView]:
        """When your screens found new names, newest first."""
        require(principal, Permission.READ)
        where = "e.owner_id = ?"
        params: list[Any] = [principal.user_id]
        if screen_id is not None:
            where += " AND e.screen_id = ?"
            params.append(screen_id)
        with self._ctx.state() as state:
            total = int(
                state.sql(f"SELECT COUNT(*) FROM screen_alert_events e WHERE {where}", params)[0][0]
            )
            rows = state.sql(
                "SELECT e.*, s.name AS screen_name FROM screen_alert_events e"
                f" LEFT JOIN screens s ON s.id = e.screen_id WHERE {where}"
                " ORDER BY e.id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        items = [
            ScreenAlertEventView(
                id=r["id"],
                screen_id=r["screen_id"],
                screen_name=r["screen_name"],
                as_of=date.fromisoformat(r["as_of"]),
                tickers=list(json.loads(r["tickers_json"])),
                matched=int(r["matched"]),
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]
        return Page[ScreenAlertEventView](items=items, total=total, limit=limit, offset=offset)

    # ---- the run -----------------------------------------------------------------------

    def evaluate(
        self, principal: Principal | None = None, as_of: date | None = None
    ) -> ScreenAlertRunSummary:
        """Run every due alert of every person now (the scheduler job, or an
        operator). ``principal=None`` is in-process."""
        if principal is not None:
            require(principal, Permission.OPERATIONS_RUN)
        now = self._clock()
        day = as_of or now.date()
        cap = self._settings().max_candidates
        with self._ctx.state() as state, self._ctx.lake() as lake:

            def run(spec: ScreenSpec, on: date) -> Sequence[str]:
                result = run_screen(lake, spec, on, max_candidates=cap)
                return [row.ticker for row in result.rows]

            return run_screen_alerts(
                state, as_of=day, run=run, publish=self._publish(state), now=now
            )

    def evaluate_view(self, principal: Principal, as_of: date | None = None) -> ScreenAlertRunView:
        day = as_of or self._clock().date()
        out = self.evaluate(principal, day)
        return ScreenAlertRunView(as_of=day, **out.as_dict())

    def _publish(self, state: SqliteState) -> Callable[[Event], Any]:
        if self._publisher is not None:
            return self._publisher(state)
        from stonks.notify.router import configured_router

        return configured_router(state).publish

    def _settings(self) -> ScreenerSettings:
        return getattr(self._ctx.settings, "screener", None) or ScreenerSettings()

    # ---- helpers -----------------------------------------------------------------------

    @staticmethod
    def _screen(state: SqliteState, principal: Principal, screen_id: str) -> None:
        rows = state.sql(
            "SELECT 1 FROM screens WHERE id = ? AND owner_id = ?", [screen_id, principal.user_id]
        )
        if not rows:
            raise NotFoundError(f"no screen with id {screen_id!r}")

    @staticmethod
    def _alert(state: SqliteState, principal: Principal, screen_id: str) -> ScreenAlertView:
        rows = state.sql(
            f"{_SELECT} WHERE a.screen_id = ? AND a.owner_id = ?", [screen_id, principal.user_id]
        )
        if not rows:
            raise NotFoundError(f"no alert on screen {screen_id!r}")
        return _view(rows[0])
