"""The per-minute loss limit of an intraday book (roadmap 21.3.2, P27, P40).

The loss is the fall of the book's marked value from its highest mark in
the last ``window_minutes`` (the current value included). Two levels:

- ``max_loss``: a soft breach. Every opening order is dropped (tag
  ``intraday_loss``), and :func:`stonks.production.intraday_halts.trip_intraday_loss`
  opens an ``intraday_loss`` halt on new buys for the portfolio;
- ``hard_loss``: a hard breach. Opening orders are dropped (tag
  ``intraday_loss_hard``) and the halt stops every new order (``all``).
  With ``flatten`` the rule also closes every position at once (tag
  ``intraday_flatten``) and the halt stays on buys, so those closes and
  later exits still pass the halt gate.

Closing orders are never dropped or shrunk (P28). Clearing the halt needs
a person and a reason, like every halt (P41). Acts only on an intraday
book. Off by default.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.execution.orders import make_client_id
from stonks.production.rules import EPS, RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import adjustment, scale_opens, settings_of
from stonks.production.rules._intraday import day_values, intraday_of, minute_suffix

__all__ = [
    "INTRADAY_FLATTEN",
    "INTRADAY_LOSS",
    "INTRADAY_LOSS_HARD",
    "IntradayLossLimit",
    "IntradayLossLimitSettings",
    "LossBreach",
    "loss_breach",
]

#: Adjustment tags: a soft breach, a hard breach, and a flattening close.
INTRADAY_LOSS = "intraday_loss"
INTRADAY_LOSS_HARD = "intraday_loss_hard"
INTRADAY_FLATTEN = "intraday_flatten"
#: The pseudo strategy in the client ids of flattening closes.
CLIENT_ID_SOURCE = "risk.intraday_loss"


class IntradayLossLimitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Fall from the window's high that halts new buys; ``None`` is off.
    max_loss: float | None = Field(default=None, gt=0.0, lt=1.0)
    #: Fall that halts every new order; ``None`` is off.
    hard_loss: float | None = Field(default=None, gt=0.0, lt=1.0)
    #: The rolling window, in minutes.
    window_minutes: int = Field(default=5, ge=1)
    #: Close every position on a hard breach.
    flatten: bool = False

    @property
    def active(self) -> bool:
        return self.max_loss is not None or self.hard_loss is not None


@dataclass(frozen=True)
class LossBreach:
    level: Literal["soft", "hard"]
    loss: float
    peak: float
    value: float
    window_minutes: int
    flatten: bool

    @property
    def halt(self) -> Literal["buys", "all"]:
        """The halt mode: a hard breach stops every order unless the book
        is being flattened, which needs its closes to get through."""
        return "all" if self.level == "hard" and not self.flatten else "buys"

    @property
    def tag(self) -> str:
        return INTRADAY_LOSS_HARD if self.level == "hard" else INTRADAY_LOSS

    @property
    def reason(self) -> str:
        return (
            f"intraday loss {self.loss:.2%} within {self.window_minutes} min "
            f"(from {self.peak:.2f} to {self.value:.2f}, {self.level} limit)"
        )


def loss_breach(ctx: RiskContext) -> LossBreach | None:
    """The breach the book is in at ``ctx.intraday.now``, or ``None``
    (no intraday state, the rule off, an unmarked holding, no breach)."""
    settings: IntradayLossLimitSettings | None = settings_of(ctx.policy, "intraday_loss_limit")
    intraday = intraday_of(ctx)
    if settings is None or not settings.active or intraday is None:
        return None
    since = intraday.now - timedelta(minutes=settings.window_minutes)
    values = day_values(ctx, intraday, since=since)
    if values is None:
        return None
    peak, value = max(values), values[-1]
    if peak <= 0:
        return None
    loss = 1.0 - value / peak
    level: Literal["soft", "hard"] | None = None
    if settings.hard_loss is not None and loss >= settings.hard_loss:
        level = "hard"
    elif settings.max_loss is not None and loss >= settings.max_loss:
        level = "soft"
    if level is None:
        return None
    return LossBreach(
        level=level,
        loss=loss,
        peak=peak,
        value=value,
        window_minutes=settings.window_minutes,
        flatten=settings.flatten,
    )


@register_rule
class IntradayLossLimit(RiskRule):
    name = "intraday_loss_limit"
    order = 4
    needs_history = True

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        breach = loss_breach(ctx)
        if breach is None:
            return list(orders), []
        kept, adjustments = scale_opens(orders, ctx, 0.0, breach.tag, breach.reason)
        if breach.level == "hard" and breach.flatten:
            forced, notes = self._flatten(kept, ctx, breach)
            kept += forced
            adjustments += notes
        return kept, adjustments

    def _flatten(
        self, kept: Sequence[Order], ctx: RiskContext, breach: LossBreach
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        """A close for what of each position the kept orders leave open."""
        intraday = intraday_of(ctx)
        assert intraday is not None
        day = ctx.as_of or intraday.now.date()
        tick_ids = {o.tick_id for o in kept}
        tick_id = tick_ids.pop() if len(tick_ids) == 1 else None
        forced: list[Order] = []
        notes: list[RiskAdjustment] = []
        for ticker, qty in sorted(ctx.portfolio.positions.items()):
            if abs(qty) <= EPS:
                continue
            side: Literal["buy", "sell"] = "sell" if qty > 0 else "buy"
            closing = sum(o.quantity for o in kept if o.ticker == ticker and o.side == side)
            remaining = abs(qty) - closing
            if remaining <= EPS:
                continue
            base = make_client_id(
                as_of=day,
                strategy_id=CLIENT_ID_SOURCE,
                ticker=ticker,
                side=side if qty > 0 else "cover",
                portfolio_id=ctx.portfolio_id,
            )
            order = Order(
                client_id=f"{base}:{minute_suffix(intraday)}",
                ticker=ticker,
                side=side,
                quantity=remaining,
                tick_id=tick_id,
                portfolio_id=ctx.portfolio_id,
                position_effect="close",
            )
            forced.append(order)
            notes.append(
                adjustment(
                    order, INTRADAY_FLATTEN, remaining, f"{breach.reason}; flatten", original=0.0
                )
            )
        return forced, notes
