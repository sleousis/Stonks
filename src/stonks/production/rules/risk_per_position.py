"""Risk per position (BL-27; Elder's 2% rule, Vince): caps the loss one
position may carry, as a fraction of the value before the tick's orders.

- ``max_risk``: position risk is ``|position| * k * ATR(20)`` (Wilder,
  adjusted bars), so the most a position may hold is
  ``max_risk * equity / (k * ATR)``. Tag ``risk_per_position``.
- ``max_var``: the one-day 99% VaR ``|position| * price * sigma * 2.33``
  (``sigma`` the zero-mean EWMA daily sigma, span 35) may not exceed
  ``max_var * equity``. Tag ``position_var``.

Only buys are touched; the position after the buy counts the quantity
already held and bought earlier in the tick. A buy that covers a short
may always bring the position back to flat. A buy of a ticker whose
history is too short to measure its risk is dropped
(``insufficient_history``). Off unless a limit is set.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.production.rules import OrderRule, Recorder, RiskBook, RiskContext, register_rule
from stonks.production.rules._common import (
    ATR_BARS,
    Z99,
    clip_with_reason,
    daily_sigma,
    last_atr,
    settings_of,
)


class RiskPerPositionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Max ``k * ATR`` risk per position as a fraction of equity (e.g. 0.02).
    max_risk: float | None = Field(default=None, ge=0.0025, le=0.05)
    #: ``k``: the ATR multiple a position is assumed to lose (its stop).
    atr_multiple: float = Field(default=3.0, gt=0.0)
    #: Max one-day 99% VaR per position as a fraction of equity.
    max_var: float | None = Field(default=None, gt=0.0, le=1.0)

    @property
    def active(self) -> bool:
        return self.max_risk is not None or self.max_var is not None


@register_rule
class RiskPerPosition(OrderRule):
    name = "risk_per_position"
    order = 52
    needs_history = True

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def check(
        self, order: Order, qty: float, book: RiskBook, ctx: RiskContext, record: Recorder
    ) -> float | None:
        settings: RiskPerPositionSettings | None = settings_of(ctx.policy, self.name)
        if order.side != "buy" or settings is None or not settings.active:
            return qty
        price = ctx.prices.get(order.ticker)
        if not price or price <= 0:
            return qty  # require_price drops it
        held = book.positions.get(order.ticker, 0.0)
        equity = max(book.equity, 0.0)
        if settings.max_risk is not None:
            atr = last_atr(ctx, order.ticker)
            if atr is None:
                return _no_history(order, record, f"ATR({ATR_BARS})")
            per_unit = settings.atr_multiple * atr
            limit = settings.max_risk * equity / per_unit
            detail = (
                f"{settings.max_risk:.2%} of {equity:.2f} / "
                f"({settings.atr_multiple} x ATR {atr:.4f}) = {limit:.4f} units"
            )
            new = clip_with_reason(order, qty, limit - held, self.name, detail, record)
            if new is None:
                return None
            qty = new
        if settings.max_var is not None:
            sigma = daily_sigma(ctx, order.ticker)
            if sigma is None:
                return _no_history(order, record, "daily sigma")
            limit = settings.max_var * equity / (price * sigma * Z99)
            detail = (
                f"one-day 99% VaR {settings.max_var:.2%} of {equity:.2f} at sigma "
                f"{sigma:.4%} = {limit:.4f} units"
            )
            new = clip_with_reason(order, qty, limit - held, "position_var", detail, record)
            if new is None:
                return None
            qty = new
        return qty


def _no_history(order: Order, record: Recorder, what: str) -> None:
    record(order, "insufficient_history", 0.0, f"not enough bars to measure {what}")
