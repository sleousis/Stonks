"""Margin call and margin check (roadmap 16.2). Runs first.

The margin model is the book's own (``ctx.margin``, when the caller has a
non-cash one) or the one in these settings (``reg_t`` by default).

In breach (equity below the maintenance requirement at today's prices):

- every opening order is dropped (no new risk while in breach);
- positions are closed until the deficit is cured, the largest
  maintenance requirement first (shorts before longs on a tie), each only
  as far as needed: a cover for a short, a sell for a long. Forced orders
  get ``make_client_id(as_of, "risk.margin_call", ticker, "cover"|"sell")``,
  ``position_effect="close"`` and ``decision_context["forced"]``, and top
  up any close already proposed for the ticker.

A forced close is never blocked by another rule: covers skip the order
rules, sells of longs are within the position, and the batch rules only
scale opening orders.

In good standing, opening orders are clipped in turn so their initial
requirement fits the excess equity (plus what this tick's closes free),
keeping ``buffer`` of it unused. Off by default.

A live margin account (roadmap 19.13) is judged by the broker's own
numbers, not the model: its cushion (excess liquidity over equity, read
from IBKR) is compared with the settings. Below ``reduce_cushion`` the
book is treated as in breach before the broker liquidates: opening orders
are dropped and positions are closed as above until the cushion is back at
``restore_cushion``. Above it, the broker's what-if margin (the
``margin_what_if`` account rule) governs new orders, so the model check is
skipped. ``warn_cushion`` only alerts (``production.live.margin``). Only
the book's own positions are closed, never the owner's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.execution.brokers.base import LiveAccountState
from stonks.execution.margin import MarginModel, MarginSettings
from stonks.execution.orders import make_client_id
from stonks.production.rules import EPS, RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import adjustment, settings_of
from stonks.production.rules._shorts import decision_day, is_opening, price, signed

#: The pseudo strategy in forced closes' client ids.
CLIENT_ID_SOURCE = "risk.margin_call"


class MarginCallSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    margin: MarginSettings = MarginSettings(model="reg_t")
    #: Share of the excess equity new opens may not use.
    buffer: float = Field(default=0.0, ge=0.0, lt=1.0)
    #: Live margin accounts (roadmap 19.13), as shares of equity. Below
    #: ``warn_cushion`` the owner gets an alert, below ``reduce_cushion``
    #: the book closes positions until the cushion is back at
    #: ``restore_cushion``. The broker liquidates at 0.
    warn_cushion: float = Field(default=0.15, gt=0.0, lt=1.0)
    reduce_cushion: float = Field(default=0.10, gt=0.0, lt=1.0)
    restore_cushion: float = Field(default=0.20, gt=0.0, lt=1.0)

    @property
    def active(self) -> bool:
        return self.enabled


def margin_model(ctx: RiskContext, settings: MarginCallSettings) -> MarginModel:
    if isinstance(ctx.margin, MarginModel) and ctx.margin.name != "cash":
        return ctx.margin
    return settings.margin.build()


@register_rule
class MarginCall(RiskRule):
    name = "margin_call"
    order = 0

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: MarginCallSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or not settings.active:
            return list(orders), []
        model = margin_model(ctx, settings)
        account = ctx.live.account if ctx.live is not None else None
        if account is not None and account.account_type == "margin":
            return self._live(orders, ctx, model, account, settings)
        prices = dict(ctx.prices)
        deficit = model.deficit(ctx.portfolio, prices, ctx.asset_classes)
        if deficit > 0:
            return self._call(orders, ctx, model, deficit)
        return self._check(orders, ctx, model, settings.buffer)

    def _live(
        self,
        orders: Sequence[Order],
        ctx: RiskContext,
        model: MarginModel,
        account: LiveAccountState,
        settings: MarginCallSettings,
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        """The broker's cushion decides (roadmap 19.13)."""
        cushion = account.cushion
        reduce_at = settings.reduce_cushion
        if cushion is None or cushion >= reduce_at:
            return list(orders), []
        restore = max(settings.restore_cushion, reduce_at)
        excess = cushion * account.equity
        deficit = restore * account.equity - excess
        why = (
            f"margin cushion {cushion:.1%} is below {reduce_at:.0%}: "
            f"reducing to {restore:.0%} before the broker liquidates"
        )
        return self._call(orders, ctx, model, deficit, why=why)

    def _call(
        self,
        orders: Sequence[Order],
        ctx: RiskContext,
        model: MarginModel,
        deficit: float,
        *,
        why: str | None = None,
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        positions = ctx.portfolio.positions
        kept: list[Order] = []
        adjustments: list[RiskAdjustment] = []
        for order in orders:
            if is_opening(order, positions):
                reason = (
                    f"{why}: no new positions"
                    if why
                    else (f"margin breach of {deficit:.2f}: no new positions")
                )
                adjustments.append(adjustment(order, self.name, 0.0, reason))
            else:
                kept.append(order)

        def requirement(item: tuple[str, float]) -> tuple[float, int, str]:
            ticker, qty = item
            p = price(ctx, ticker) or 0.0
            rate = model.maintenance_rate(qty, ctx.asset_classes.get(ticker, "equity"))  # type: ignore[arg-type]
            return (-abs(qty) * p * rate, 0 if qty < 0 else 1, ticker)

        tick_ids = {o.tick_id for o in orders}
        tick_id = tick_ids.pop() if len(tick_ids) == 1 else None
        left = deficit
        for ticker, qty in sorted(positions.items(), key=requirement):
            p = price(ctx, ticker)
            if left <= 0 or p is None or abs(qty) <= EPS:
                continue
            asset_class = ctx.asset_classes.get(ticker, "equity")
            need = model.cover_quantity(qty, p, left, asset_class)  # type: ignore[arg-type]
            left -= need * p * model.maintenance_rate(qty, asset_class)  # type: ignore[arg-type]
            closing = sum(o.quantity for o in kept if o.ticker == ticker and signed(o) * qty < 0)
            extra = min(need, abs(qty)) - closing
            if extra <= EPS:
                continue
            short = qty < 0
            forced = Order(
                client_id=make_client_id(
                    as_of=decision_day(ctx),
                    strategy_id=CLIENT_ID_SOURCE,
                    ticker=ticker,
                    side="cover" if short else "sell",
                    portfolio_id=ctx.portfolio_id,
                ),
                ticker=ticker,
                side="buy" if short else "sell",
                quantity=extra,
                tick_id=tick_id,
                position_effect="close",
                decision_context={"forced": self.name, "deficit": deficit},
            )
            kept.append(forced)
            reason = (
                f"{why}: forced close" if why else f"margin breach of {deficit:.2f}: forced close"
            )
            adjustments.append(adjustment(forced, self.name, extra, reason, original=0.0))
        return kept, adjustments

    def _check(
        self, orders: Sequence[Order], ctx: RiskContext, model: MarginModel, buffer: float
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        positions = ctx.portfolio.positions
        prices = dict(ctx.prices)
        room = model.excess_equity(ctx.portfolio, prices, ctx.asset_classes)
        for order in orders:
            held = positions.get(order.ticker, 0.0)
            p = price(ctx, order.ticker)
            if p is None or held == 0 or is_opening(order, positions):
                continue
            # A close frees its share of the position's initial requirement.
            freed = min(order.quantity / abs(held), 1.0)
            ac = ctx.asset_classes.get(order.ticker, "equity")
            room += freed * model.initial_requirement(order.ticker, held, p, ac)  # type: ignore[arg-type]
        room *= 1.0 - buffer
        kept: list[Order] = []
        adjustments: list[RiskAdjustment] = []
        for order in orders:
            p = price(ctx, order.ticker)
            if not is_opening(order, positions) or p is None:
                kept.append(order)
                continue
            if order.side == "sell" and not model.allows_short:
                adjustments.append(
                    adjustment(order, self.name, 0.0, f"margin model {model.name} allows no shorts")
                )
                continue
            ac: Any = ctx.asset_classes.get(order.ticker, "equity")
            per_share = model.initial_requirement(
                order.ticker, signed(order) / order.quantity, p, ac
            )
            qty = min(order.quantity, max(room, 0.0) / per_share)
            if qty < order.quantity:
                reason = f"initial margin {per_share:.2f}/share leaves room for {qty:.4f}"
                adjustments.append(adjustment(order, self.name, qty, reason))
            if qty > EPS:
                kept.append(order if qty == order.quantity else replace(order, quantity=qty))
                room -= qty * per_share
        return kept, adjustments
