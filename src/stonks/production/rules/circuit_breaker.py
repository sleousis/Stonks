"""Circuit breaker (BL-28; Elder's 6% rule, Benedict, Ghosh & Donadio).

Three halts, each read from the portfolio's equity curve (points up to
``as_of`` plus today's value):

- ``month_loss``: the value fell ``max_month_loss`` (default 6%) below the
  first snapshot of the calendar month;
- ``week_loss``: the value fell ``max_week_loss`` (default 4%) over a
  rolling ``week_sessions`` (5) snapshots;
- ``drawdown``: the value fell ``max_drawdown_halt`` (default 20%) below its
  peak. This one **latches**: it stays on after a recovery until a person
  clears it (``production.halts.clear_halt``).

With ``cooldown="rest_of_month"`` a month or week trip holds until the
month ends, even when the loss is made back; with ``"none"`` it holds only
while the loss does. The tick is stateless, so the halts are rebuilt by
replaying the curve: the same curve always gives the same halts.

When tripped the rule drops every buy that opens or grows a long position.
Sells, exits and covers pass. Because the portfolio pipeline is shared,
the breaker also runs in backtests. In the live tick the halt is enforced
by the ``risk_halts`` trade gate, which persists trips so a person can
see and clear them. Off unless a limit is set.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.production.rules import RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import book_value, scale_opens, settings_of

BreakerKind = Literal["month_loss", "week_loss", "drawdown"]
Cooldown = Literal["rest_of_month", "none"]

#: Recommended limits (BL-28). The settings default to off; set these to
#: switch the breaker on.
RECOMMENDED = {"max_month_loss": 0.06, "max_week_loss": 0.04, "max_drawdown_halt": 0.20}


class CircuitBreakerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Loss from the month's first snapshot that halts buys; ``None`` is off.
    max_month_loss: float | None = Field(None, gt=0.0, lt=1.0)
    #: Loss over ``week_sessions`` snapshots that halts buys; ``None`` is off.
    max_week_loss: float | None = Field(None, gt=0.0, lt=1.0)
    #: Drawdown from the peak that halts buys until cleared; ``None`` is off.
    max_drawdown_halt: float | None = Field(None, gt=0.0, lt=1.0)
    week_sessions: int = Field(5, ge=1, le=60)
    cooldown: Cooldown = "rest_of_month"

    @property
    def active(self) -> bool:
        return any(
            v is not None for v in (self.max_month_loss, self.max_week_loss, self.max_drawdown_halt)
        )


@dataclass(frozen=True)
class BreakerTrip:
    kind: BreakerKind
    reason: str
    #: The first day the halt no longer applies; ``None`` latches it.
    expires_on: date | None


def next_month_start(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def _daily(curve: Sequence[tuple[date, float]], as_of: date) -> list[tuple[date, float]]:
    """One point per day (the last one wins), oldest first, none after ``as_of``."""
    by_day: dict[date, float] = {}
    for day, value in curve:
        if day <= as_of:
            by_day[day] = float(value)
    return sorted(by_day.items())


def breaker_trips(
    curve: Sequence[tuple[date, float]],
    as_of: date,
    settings: CircuitBreakerSettings,
    *,
    since: Mapping[str, date] | None = None,
) -> list[BreakerTrip]:
    """The halts ``curve`` (``(day, value)``, today included) trips on
    ``as_of``.

    ``since`` maps a kind to the day a person last cleared it: only points
    after that day can trip it again. The month base stays the month's
    first snapshot; the drawdown replay (and its peak) restarts after it."""
    since = since or {}
    points = _daily(curve, as_of)
    if not points:
        return []
    trips: list[BreakerTrip] = []
    sticky = settings.cooldown == "rest_of_month"
    month_end = next_month_start(as_of)
    tomorrow = as_of + timedelta(days=1)
    in_month = [
        i for i, (d, _) in enumerate(points) if (d.year, d.month) == (as_of.year, as_of.month)
    ]
    checked = in_month if sticky else in_month[-1:]

    def after(kind: str) -> list[int]:
        cut = since.get(kind)
        return [i for i in checked if cut is None or points[i][0] > cut]

    if settings.max_month_loss is not None and in_month:
        base = points[in_month[0]][1]
        month_points = after("month_loss")
        worst = (
            min((points[i][1] / base - 1 for i in month_points), default=0.0) if base > 0 else 0.0
        )
        if worst <= -settings.max_month_loss:
            trips.append(
                BreakerTrip(
                    "month_loss",
                    f"month loss {-worst:.2%} from {base:.2f} reached the "
                    f"{settings.max_month_loss:.2%} limit",
                    month_end if sticky else tomorrow,
                )
            )
    if settings.max_week_loss is not None and in_month:
        n = settings.week_sessions
        losses = [
            points[i][1] / points[max(i - n, 0)][1] - 1
            for i in after("week_loss")
            if i > 0 and points[max(i - n, 0)][1] > 0
        ]
        worst = min(losses, default=0.0)
        if worst <= -settings.max_week_loss:
            trips.append(
                BreakerTrip(
                    "week_loss",
                    f"loss of {-worst:.2%} over {n} sessions reached the "
                    f"{settings.max_week_loss:.2%} limit",
                    month_end if sticky else tomorrow,
                )
            )
    if settings.max_drawdown_halt is not None:
        peak, deepest = 0.0, 0.0
        cut = since.get("drawdown")
        for day, value in points:
            if cut is not None and day <= cut:
                continue
            peak = max(peak, value)
            if peak > 0:
                deepest = max(deepest, 1 - value / peak)
        if deepest >= settings.max_drawdown_halt:
            trips.append(
                BreakerTrip(
                    "drawdown",
                    f"drawdown of {deepest:.2%} from the peak reached the "
                    f"{settings.max_drawdown_halt:.2%} limit (latched until cleared)",
                    None,
                )
            )
    return trips


@register_rule
class CircuitBreaker(RiskRule):
    name = "circuit_breaker"
    order = 4
    needs_history = True

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: CircuitBreakerSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or not settings.active:
            return list(orders), []
        curve = list(ctx.equity_curve)
        as_of = ctx.as_of or (curve[-1][0] if curve else None)
        if as_of is None:
            return list(orders), []
        value = book_value(ctx)
        if value is None:  # a holding has no mark: no fake drawdown (BE-45)
            return list(orders), []
        curve.append((as_of, value))
        trips = breaker_trips(curve, as_of, settings)
        if not trips:
            return list(orders), []
        reason = "circuit breaker: " + "; ".join(t.reason for t in trips)
        return scale_opens(orders, ctx, 0.0, self.name, reason)
