"""Per-strategy protections for live books (roadmap 19.6, freqtrade style).

Three registered rules that read the book's recent closed trades
(``ctx.live.closed_trades``, from its own fills):

- ``stop_cooldown``: after a stop-out on a ticker, the same strategy does
  not reopen it for ``cooldown_days``;
- ``stop_guard``: after ``max_stops`` stop-outs within ``window_days``, the
  strategy opens nothing until the count falls under the limit;
- ``losing_lock``: a ticker whose last ``max_consecutive_losses`` trades
  for the strategy all lost is locked for ``lock_days`` after the last one.

A stop-out is an exit by a stop order (``decision_context.trigger ==
"stop"``). Until broker-side stops exist (19.10), ``count_losses = true``
(the default) also counts any losing exit. An order built for the whole
portfolio (no strategy, or the ``portfolio`` constructor) is checked
against every strategy's trades.

They only drop opening orders. Closing orders are never blocked (P28).
They act only on live books. Every one is off by default.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.production.live.trades import ClosedTrade
from stonks.production.rules import RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import adjustment, is_opening_order, settings_of
from stonks.production.rules._shorts import decision_day

#: Orders with no single strategy behind them.
_WHOLE_BOOK = (None, "portfolio")


class StopCooldownSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cooldown_days: int | None = Field(default=None, ge=1)
    count_losses: bool = True

    @property
    def active(self) -> bool:
        return self.cooldown_days is not None


class StopGuardSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_stops: int | None = Field(default=None, ge=1)
    window_days: int = Field(default=7, ge=1)
    count_losses: bool = True

    @property
    def active(self) -> bool:
        return self.max_stops is not None


class LosingLockSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_consecutive_losses: int | None = Field(default=None, ge=1)
    lock_days: int = Field(default=30, ge=1)

    @property
    def active(self) -> bool:
        return self.max_consecutive_losses is not None


def _trades_for(order: Order, ctx: RiskContext) -> list[ClosedTrade]:
    trades = list(ctx.live.closed_trades) if ctx.live is not None else []
    if order.strategy_id in _WHOLE_BOOK:
        return trades
    return [t for t in trades if t.strategy_id == order.strategy_id]


def _is_stop(trade: ClosedTrade, count_losses: bool) -> bool:
    return trade.stop or (count_losses and trade.loss)


class _Protection(RiskRule):
    """Drops the opening orders :meth:`blocked` names a reason for."""

    order = 5

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def blocked(
        self, order: Order, trades: list[ClosedTrade], settings: Any, ctx: RiskContext
    ) -> str | None:
        raise NotImplementedError

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings = settings_of(ctx.policy, self.name)
        if settings is None or not settings.active or ctx.live is None:
            return list(orders), []
        kept: list[Order] = []
        adjustments: list[RiskAdjustment] = []
        for order in orders:
            reason = None
            if is_opening_order(order, ctx):
                reason = self.blocked(order, _trades_for(order, ctx), settings, ctx)
            if reason is None:
                kept.append(order)
            else:
                adjustments.append(adjustment(order, self.name, 0.0, reason))
        return kept, adjustments


@register_rule
class StopCooldown(_Protection):
    name = "stop_cooldown"

    def blocked(
        self,
        order: Order,
        trades: list[ClosedTrade],
        settings: StopCooldownSettings,
        ctx: RiskContext,
    ) -> str | None:
        assert settings.cooldown_days is not None
        since = decision_day(ctx) - timedelta(days=settings.cooldown_days)
        stops = [
            t
            for t in trades
            if t.ticker == order.ticker
            and t.exit_day > since
            and _is_stop(t, settings.count_losses)
        ]
        if not stops:
            return None
        last = max(t.exit_day for t in stops)
        return (
            f"cooling down after a stop-out on {last.isoformat()} ({settings.cooldown_days} days)"
        )


@register_rule
class StopGuard(_Protection):
    name = "stop_guard"

    def blocked(
        self, order: Order, trades: list[ClosedTrade], settings: StopGuardSettings, ctx: RiskContext
    ) -> str | None:
        assert settings.max_stops is not None
        since = decision_day(ctx) - timedelta(days=settings.window_days)
        count = sum(1 for t in trades if t.exit_day > since and _is_stop(t, settings.count_losses))
        if count < settings.max_stops:
            return None
        who = order.strategy_id if order.strategy_id not in _WHOLE_BOOK else "the book"
        return (
            f"{who} paused: {count} stop-outs in {settings.window_days} days "
            f"(limit {settings.max_stops})"
        )


@register_rule
class LosingLock(_Protection):
    name = "losing_lock"

    def blocked(
        self,
        order: Order,
        trades: list[ClosedTrade],
        settings: LosingLockSettings,
        ctx: RiskContext,
    ) -> str | None:
        n = settings.max_consecutive_losses
        assert n is not None
        mine = sorted((t for t in trades if t.ticker == order.ticker), key=lambda t: t.exit_day)
        recent = mine[-n:]
        if len(recent) < n or not all(t.loss for t in recent):
            return None
        last = recent[-1].exit_day
        if decision_day(ctx) - last > timedelta(days=settings.lock_days):
            return None
        return f"{order.ticker} locked: its last {n} trades lost (until {settings.lock_days} days after {last.isoformat()})"
