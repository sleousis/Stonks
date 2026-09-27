"""The stale data gate of an intraday book (roadmap 21.3.2).

No new risk on old prices: an opening order is dropped when the latest bar
of its ticker is older than ``max_bar_age_seconds`` at the event, when the
ticker has no bar at all, or when the stream is stale or reconnecting
(``IntradayContext.stream_stale``). Closing orders always pass, so exits
still go out on the last known prices.

This is the per-ticker, per-event half. The daily ``operational_halt``
rule and the global ``operational`` halt watch the feed across days.
Acts only on an intraday book. Off by default.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.production.rules import RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import adjustment, is_opening_order, settings_of
from stonks.production.rules._intraday import IntradayContext, intraday_of

__all__ = ["IntradayStaleData", "IntradayStaleDataSettings"]


class IntradayStaleDataSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Seconds the latest bar of a ticker may lag the event; ``None`` is off.
    max_bar_age_seconds: int | None = Field(default=None, ge=1)

    @property
    def active(self) -> bool:
        return self.max_bar_age_seconds is not None


@register_rule
class IntradayStaleData(RiskRule):
    name = "intraday_stale_data"
    order = 5
    needs_history = True

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: IntradayStaleDataSettings | None = settings_of(ctx.policy, self.name)
        intraday = intraday_of(ctx)
        if settings is None or settings.max_bar_age_seconds is None or intraday is None:
            return list(orders), []
        limit = settings.max_bar_age_seconds
        kept: list[Order] = []
        adjustments: list[RiskAdjustment] = []
        for order in orders:
            reason = None
            if is_opening_order(order, ctx):
                reason = self._stale(order.ticker, intraday, limit)
            if reason is None:
                kept.append(order)
            else:
                adjustments.append(adjustment(order, self.name, 0.0, reason))
        return kept, adjustments

    @staticmethod
    def _stale(ticker: str, intraday: IntradayContext, limit: int) -> str | None:
        """Why ``ticker``'s data is too old to open on, or ``None``."""
        if intraday.stream_stale:
            return "stale data: the stream is stale or reconnecting"
        last = intraday.last_bar_at.get(ticker)
        if last is None or last > intraday.now:
            return f"stale data: no bar for {ticker} yet"
        age = (intraday.now - last).total_seconds()
        if age > limit:
            return f"stale data: the latest {ticker} bar is {age:.0f}s old (limit {limit}s)"
        return None
