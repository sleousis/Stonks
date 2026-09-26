"""The ``risk_halts`` trade gate (BL-28 W3.2, roadmap 12.6).

Before a portfolio's orders reach its broker the gate:

1. evaluates the circuit breaker on the portfolio's snapshot curve when the
   book's policy switches it on (``GateContext.policy``), and opens a
   ``risk_halts`` row per new trip (a ``risk`` notification goes to the
   owner). A dry run halts the same way but writes nothing;
2. reads every halt in force for the portfolio: global, its owner's and
   its own (the kill switch, breaker trips, the operational halt).

The strictest one wins: ``all`` (the kill switch) places nothing, ``buys``
drops buys and lets sells and exits through. Without the table (an old
state file) there is nothing to enforce.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from stonks.logging import get_logger
from stonks.production.halts import (
    Halt,
    active_halts,
    halts_enabled,
    notify_trip,
    trip_halt,
)
from stonks.production.hooks import GateContext, GateVerdict, TradeGate, register_gate
from stonks.production.rules._common import settings_of
from stonks.production.rules.circuit_breaker import (
    BreakerTrip,
    CircuitBreakerSettings,
    breaker_trips,
)
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.hooks.risk_halts")

BREAKER_ACTOR = "system"


def portfolio_curve(state: SqliteState, portfolio_id: str, as_of: date) -> list[tuple[date, float]]:
    """The portfolio's value per day up to ``as_of`` (a same-day rerun's
    later snapshot wins), oldest first."""
    rows = state.sql(
        "SELECT as_of, taken_at, total_value FROM portfolio_snapshots"
        " WHERE portfolio_id = ? ORDER BY id",
        [portfolio_id],
    )
    by_day: dict[date, float] = {}
    for r in rows:
        day = date.fromisoformat(r["as_of"]) if r["as_of"] else _utc_day(r["taken_at"])
        if day <= as_of:
            by_day[day] = float(r["total_value"])
    return sorted(by_day.items())


def portfolio_owner(state: SqliteState, portfolio_id: str) -> str | None:
    """The owner of ``portfolio_id`` (the legacy ``pf_default`` book carries
    no owner id, but its row names the bootstrap admin), or ``None`` when
    the portfolio is missing. ``risk_halts`` (016) implies ``portfolios``."""
    rows = state.sql("SELECT owner_id FROM portfolios WHERE id = ?", [portfolio_id])
    return rows[0]["owner_id"] if rows else None


def _utc_day(value: str) -> date:
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC).date()


def last_clears(state: SqliteState, portfolio_id: str) -> dict[str, date]:
    """Per breaker kind, the day a person last cleared it on this portfolio."""
    rows = state.sql(
        "SELECT kind, MAX(cleared_at) AS cleared_at FROM risk_halts"
        " WHERE scope = 'portfolio' AND portfolio_id = ? AND cleared_at IS NOT NULL"
        " AND clear_reason <> 'expired' GROUP BY kind",
        [portfolio_id],
    )
    return {r["kind"]: _utc_day(r["cleared_at"]) for r in rows}


@register_gate
class RiskHaltGate(TradeGate):
    name = "risk_halts"
    order = 10

    def check(self, ctx: GateContext) -> GateVerdict | None:
        state = ctx.state
        if not halts_enabled(state):
            return None
        pending = self._breaker(ctx)
        owner = ctx.owner_id or portfolio_owner(state, ctx.portfolio_id)
        halts = active_halts(state, ctx.as_of, portfolio_id=ctx.portfolio_id, user_id=owner)
        reasons = [f"{h.kind} ({h.target}): {h.reason}" for h in halts]
        reasons += [f"{t.kind} (portfolio {ctx.portfolio_id}): {t.reason}" for t in pending]
        if not reasons:
            return None
        mode = "all" if any(h.halt == "all" for h in halts) else "buys"
        return GateVerdict(halt=mode, reason="; ".join(reasons), gate=self.name)

    def _breaker(self, ctx: GateContext) -> list[BreakerTrip]:
        """Trips not persisted yet: in a dry run all of them, otherwise
        none (each is written as a halt and read back by ``check``)."""
        settings: CircuitBreakerSettings | None = settings_of(ctx.policy, "circuit_breaker")
        if settings is None or not settings.active:
            return []
        state = ctx.state
        curve = portfolio_curve(state, ctx.portfolio_id, ctx.as_of)
        trips = breaker_trips(
            curve, ctx.as_of, settings, since=last_clears(state, ctx.portfolio_id)
        )
        if ctx.dry_run:
            return trips
        for trip in trips:
            halt, created = trip_halt(
                state,
                trip.kind,
                reason=trip.reason,
                actor=BREAKER_ACTOR,
                portfolio_id=ctx.portfolio_id,
                expires_on=trip.expires_on,
                on=ctx.as_of,
            )
            if created:
                self._notify(state, halt)
        return []

    def _notify(self, state: SqliteState, halt: Halt) -> None:
        _log.warning("breaker.tripped", halt_id=halt.id, kind=halt.kind, reason=halt.reason)
        notify_trip(state, halt)
