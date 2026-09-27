"""Squeeze guard (roadmap 16.2): get out of a short before a squeeze.

A held short is covered in full when any set trigger fires:

- ``max_borrow_fee``: the borrow fee (``borrow_check``'s source rules)
  is above it, the classic sign of a crowded short;
- ``max_adverse_pct``: the price is that far above the entry (the close on
  the position's entry date);
- ``atr_multiple``: the price is that many ATR(20) above the entry;
- ``spike_pct`` over ``spike_bars``: the close rose that much over the
  last ``spike_bars`` bars.

The cover tops up any cover already proposed to the whole short, gets the
client id ``make_client_id(as_of, "risk.squeeze_guard", ticker, "cover")``
and ``position_effect="close"``, and never meets another rule's limit
(covers skip the order rules). Short sales of a ticker whose borrow fee or
recent spike trips a trigger are dropped, so the guard never opens into a
squeeze. The price triggers need history and are skipped without it. Runs
right after the margin call. Off by default.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.execution.borrow import BorrowSettings
from stonks.execution.orders import make_client_id
from stonks.production.rules import EPS, RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import adjustment, history, last_atr, settings_of
from stonks.production.rules._shorts import decision_day, is_opening, price
from stonks.production.rules.borrow_check import borrow_source, quote_for

#: The pseudo strategy in forced covers' client ids.
CLIENT_ID_SOURCE = "risk.squeeze_guard"


class SqueezeGuardSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_borrow_fee: float | None = Field(default=None, ge=0.0)
    max_adverse_pct: float | None = Field(default=None, gt=0.0)
    atr_multiple: float | None = Field(default=None, gt=0.0)
    spike_pct: float | None = Field(default=None, gt=0.0)
    spike_bars: int = Field(default=5, ge=1)
    #: The static borrow source when the caller brings none.
    borrow: BorrowSettings | None = None

    @property
    def active(self) -> bool:
        return any(
            v is not None
            for v in (self.max_borrow_fee, self.max_adverse_pct, self.atr_multiple, self.spike_pct)
        )


def _entry_close(ctx: RiskContext, ticker: str) -> float | None:
    entry = ctx.entry_dates.get(ticker)
    frame = history(ctx, ticker)
    if entry is None or frame is None:
        return None
    upto = frame[frame.index <= pd.Timestamp(entry)]
    if upto.empty:
        return None
    value = float(upto["close"].iloc[-1])
    return value if value > 0 else None


def _spike(ctx: RiskContext, ticker: str, settings: SqueezeGuardSettings) -> str | None:
    if settings.spike_pct is None:
        return None
    frame = history(ctx, ticker)
    if frame is None or len(frame) <= settings.spike_bars:
        return None
    closes = frame["close"].astype(float)
    base = float(closes.iloc[-1 - settings.spike_bars])
    rise = float(closes.iloc[-1]) / base - 1.0 if base > 0 else 0.0
    if rise >= settings.spike_pct:
        return f"close up {rise:.1%} in {settings.spike_bars} bars >= {settings.spike_pct:.1%}"
    return None


def _fee(ctx: RiskContext, ticker: str, settings: SqueezeGuardSettings, source: Any) -> str | None:
    if settings.max_borrow_fee is None:
        return None
    quote = quote_for(ctx, source, ticker)
    if quote is not None and quote.fee_rate_annual > settings.max_borrow_fee:
        return f"borrow fee {quote.fee_rate_annual:.2%} > {settings.max_borrow_fee:.2%}"
    return None


def _adverse(ctx: RiskContext, ticker: str, settings: SqueezeGuardSettings) -> str | None:
    now = price(ctx, ticker)
    entry = _entry_close(ctx, ticker)
    if now is None or entry is None:
        return None
    if settings.max_adverse_pct is not None and now >= entry * (1 + settings.max_adverse_pct):
        return f"price {now:g} is {now / entry - 1:.1%} above entry {entry:g}"
    if settings.atr_multiple is not None:
        atr = last_atr(ctx, ticker)
        if atr is not None and now - entry >= settings.atr_multiple * atr:
            return f"price {now:g} is {(now - entry) / atr:.1f} ATR above entry {entry:g}"
    return None


@register_rule
class SqueezeGuard(RiskRule):
    name = "squeeze_guard"
    order = 1

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: SqueezeGuardSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or not settings.active:
            return list(orders), []
        source = borrow_source(ctx, settings.borrow)
        positions = ctx.portfolio.positions
        held_shorts = {t: q for t, q in positions.items() if q < -EPS}
        blocked: dict[str, str] = {}
        cover: dict[str, str] = {}
        tickers = set(held_shorts) | {o.ticker for o in orders if o.side == "sell"}
        for ticker in sorted(tickers):
            reason = _fee(ctx, ticker, settings, source) or _spike(ctx, ticker, settings)
            if ticker in held_shorts:
                reason = reason or _adverse(ctx, ticker, settings)
                if reason is not None:
                    cover[ticker] = reason
            if reason is not None:
                blocked[ticker] = reason
        if not blocked:
            return list(orders), []

        kept: list[Order] = []
        adjustments: list[RiskAdjustment] = []
        for order in orders:
            if order.side == "sell" and order.ticker in blocked and is_opening(order, positions):
                reason = f"squeeze risk: {blocked[order.ticker]}"
                adjustments.append(adjustment(order, self.name, 0.0, reason))
            else:
                kept.append(order)
        tick_ids = {o.tick_id for o in orders}
        tick_id = tick_ids.pop() if len(tick_ids) == 1 else None
        for ticker, reason in cover.items():
            covering = sum(
                o.quantity
                for o in kept
                if o.ticker == ticker and o.side == "buy" and o.position_effect != "open"
            )
            remaining = -held_shorts[ticker] - covering
            if remaining <= EPS:
                continue
            forced = Order(
                client_id=make_client_id(
                    as_of=decision_day(ctx),
                    strategy_id=CLIENT_ID_SOURCE,
                    ticker=ticker,
                    side="cover",
                    portfolio_id=ctx.portfolio_id,
                ),
                ticker=ticker,
                side="buy",
                quantity=remaining,
                tick_id=tick_id,
                position_effect="close",
                decision_context={"forced": self.name, "reason": reason},
            )
            kept.append(forced)
            adjustments.append(
                adjustment(forced, self.name, remaining, f"forced cover: {reason}", original=0.0)
            )
        return kept, adjustments
