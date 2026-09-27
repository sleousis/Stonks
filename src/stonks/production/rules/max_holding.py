"""Maximum holding time (BL-27; Ghosh & Donadio): a position held for
``max_holding_bars`` bars or more since its entry date is closed in full
(a long is sold, a short is covered, BE-04).
This guards against zombie positions, e.g. ones left by a retired
strategy that no longer proposes orders for them.

Bars held are the context history's bars dated after the entry date, up
to ``as_of``; the history is about a year long, so older positions count
as held for at least that many bars. The forced sell tops up any sell the
strategies already proposed to the whole position, gets the client id
``make_client_id(as_of, "risk.max_holding", ticker, "sell")`` (with the
book's ``portfolio_id`` for a non-default portfolio) and no
strategy id, and takes the tick id the other orders share. Buys of an
expiring ticker are dropped in the same tick, so the tick doesn't sell and
buy the same name. A short is covered the same way (client id side
``cover``), and new short sales of it are dropped.

Runs first, so the later rules see the freed exposure. Off by default.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.execution.orders import make_client_id
from stonks.production.rules import EPS, RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import adjustment, history, settings_of

#: The pseudo strategy in forced sells' client ids.
CLIENT_ID_SOURCE = "risk.max_holding"


class MaxHoldingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_holding_bars: int | None = Field(default=None, ge=1)

    @property
    def active(self) -> bool:
        return self.max_holding_bars is not None


@register_rule
class MaxHolding(RiskRule):
    name = "max_holding"
    order = 1
    needs_history = True

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: MaxHoldingSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or settings.max_holding_bars is None:
            return list(orders), []
        limit = settings.max_holding_bars
        expired: dict[str, tuple[float, int, date]] = {}
        for ticker, qty in sorted(ctx.portfolio.positions.items()):
            entry = ctx.entry_dates.get(ticker)
            frame = history(ctx, ticker)
            if abs(qty) <= EPS or entry is None or frame is None:
                continue
            held_bars = int((frame.index > pd.Timestamp(entry)).sum())
            if held_bars >= limit:
                expired[ticker] = (qty, held_bars, frame.index[-1].date())
        if not expired:
            return list(orders), []

        kept: list[Order] = []
        adjustments: list[RiskAdjustment] = []
        for order in orders:
            held = expired.get(order.ticker)
            # an order that grows the expiring position (a buy of a long, a
            # sell of a short) is dropped: the tick closes it instead
            grows = held is not None and (order.side == "buy") == (held[0] > 0)
            if grows:
                assert held is not None
                adjustments.append(
                    adjustment(
                        order,
                        self.name,
                        0.0,
                        f"position held {held[1]} bars >= max {limit}; being closed",
                    )
                )
            else:
                kept.append(order)
        tick_ids = {o.tick_id for o in orders}
        tick_id = tick_ids.pop() if len(tick_ids) == 1 else None
        for ticker, (qty, bars, last_bar) in expired.items():
            side: Literal["buy", "sell"] = "sell" if qty > 0 else "buy"
            closing = sum(o.quantity for o in kept if o.side == side and o.ticker == ticker)
            remaining = abs(qty) - closing
            if remaining <= EPS:
                continue
            forced = Order(
                client_id=make_client_id(
                    as_of=ctx.as_of or last_bar,
                    strategy_id=CLIENT_ID_SOURCE,
                    ticker=ticker,
                    side="sell" if qty > 0 else "cover",
                    portfolio_id=ctx.portfolio_id,
                ),
                ticker=ticker,
                side=side,
                quantity=remaining,
                tick_id=tick_id,
                position_effect="close",
            )
            kept.append(forced)
            what = "forced sell" if qty > 0 else "forced cover"
            reason = f"position held {bars} bars >= max {limit}; {what}"
            adjustments.append(adjustment(forced, self.name, remaining, reason, original=0.0))
        return kept, adjustments
