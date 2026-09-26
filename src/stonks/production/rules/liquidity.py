"""Liquidity (BL-27; Karasan, Harris): keep each buy small against what the
market trades, over the last 20 bars up to ``as_of``.

- ``max_pct_adv``: a buy's notional is at most this share of the median
  daily dollar volume (``close * volume``). Tag ``max_pct_adv``.
- ``min_median_dollar_volume``: buys of names trading less are dropped.
- ``max_amihud``: buys of names whose Amihud illiquidity (mean
  ``|log return| / dollar volume``) is above this are dropped.

With any limit set, a buy without 20 bars of volume is dropped
(``insufficient_history``). Sells are never touched. Off by default.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.production.rules import OrderRule, Recorder, RiskBook, RiskContext, register_rule
from stonks.production.rules._common import (
    LIQUIDITY_BARS,
    clip_with_reason,
    history,
    settings_of,
)


class LiquiditySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_pct_adv: float | None = Field(default=None, gt=0.0, le=1.0)
    min_median_dollar_volume: float | None = Field(default=None, ge=0.0)
    max_amihud: float | None = Field(default=None, gt=0.0)

    @property
    def active(self) -> bool:
        return any(
            v is not None
            for v in (self.max_pct_adv, self.min_median_dollar_volume, self.max_amihud)
        )


@register_rule
class Liquidity(OrderRule):
    name = "liquidity"
    order = 56
    needs_history = True

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def check(
        self, order: Order, qty: float, book: RiskBook, ctx: RiskContext, record: Recorder
    ) -> float | None:
        settings: LiquiditySettings | None = settings_of(ctx.policy, self.name)
        if order.side != "buy" or settings is None or not settings.active:
            return qty
        price = ctx.prices.get(order.ticker)
        if not price or price <= 0:
            return qty
        frame = history(ctx, order.ticker)
        window = None if frame is None else frame.iloc[-LIQUIDITY_BARS:]
        dollar = None if window is None else window["close"] * window["volume"]
        if dollar is None or len(dollar) < LIQUIDITY_BARS or dollar.isna().any():
            record(
                order,
                "insufficient_history",
                0.0,
                f"need {LIQUIDITY_BARS} bars of volume to measure liquidity",
            )
            return None
        median = float(dollar.median())
        floor = settings.min_median_dollar_volume
        if floor is not None and median < floor:
            record(
                order,
                "min_median_dollar_volume",
                0.0,
                f"median dollar volume {median:.0f} < min {floor:.0f}",
            )
            return None
        if settings.max_amihud is not None:
            assert frame is not None
            returns = np.log(frame["close"]).diff().iloc[-LIQUIDITY_BARS:]
            illiq = float((returns.abs() / dollar.where(dollar > 0)).mean())
            if math.isnan(illiq) or illiq > settings.max_amihud:
                record(
                    order,
                    "max_amihud",
                    0.0,
                    f"Amihud illiquidity {illiq:.3g} > max {settings.max_amihud:.3g}",
                )
                return None
        if settings.max_pct_adv is not None:
            limit = settings.max_pct_adv * median / price
            detail = f"{settings.max_pct_adv:.2%} of median dollar volume {median:.0f}"
            return clip_with_reason(order, qty, limit, "max_pct_adv", detail, record)
        return qty
