"""Portfolio volatility target (BL-27; Carver's risk overlay).

The book after the tick's orders is ``h + s * d``: ``h`` the holdings once
the sells go through, ``d`` the proposed opening orders (buys and short
sales, BE-05), both as signed weights of the value before the tick. They
are scaled by the largest
``s`` in ``[0, 1]`` that keeps

- the ex-ante annual volatility ``sqrt(w' S w)`` at or under ``vol_cap``
  (``S`` an EWMA covariance, span 60, of daily log returns of the context
  history up to ``as_of``), tag ``portfolio_vol``; and
- the correlation-shock bound ``sum |w_i| sigma_i`` (every correlation
  at 1) at or under ``shock_cap``, tag ``correlation_shock``.

Both are convex in ``s``, so the largest feasible ``s`` is found by
bisection; when the holdings alone breach a cap, opening orders are dropped.
Returns are taken on the union of the tickers' bar dates with closes
carried forward, and annualised with the most bars per year among the
asset classes involved (365 once crypto is in, else 252). A pair without
enough overlapping bars counts as perfectly correlated.

Buys of tickers with fewer than 20 returns are dropped
(``insufficient_history``); holdings without history or price are left
out of the estimate. Sells and covers pass. Off unless a cap is set.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.features.volatility import periods_per_year
from stonks.production.rules import (
    RiskAdjustment,
    RiskContext,
    RiskRule,
    register_rule,
)
from stonks.production.rules._common import (
    adjustment,
    history,
    is_opening_order,
    largest_scale,
    marks,
    positions_after_sells,
    scale_opens,
    settings_of,
)

#: EWMA span (bars) of the covariance, and the returns it needs.
COV_SPAN = 60
MIN_RETURNS = 20


class PortfolioVolSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Max ex-ante annual volatility of the book (e.g. 0.25).
    vol_cap: float | None = Field(default=None, gt=0.0)
    #: Max ``sum |w_i| sigma_i`` (annual; e.g. 0.40).
    shock_cap: float | None = Field(default=None, gt=0.0)

    @property
    def active(self) -> bool:
        return self.vol_cap is not None or self.shock_cap is not None


@register_rule
class PortfolioVol(RiskRule):
    name = "portfolio_vol"
    order = 3
    needs_history = True

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: PortfolioVolSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or not settings.active:
            return list(orders), []
        buys = [o for o in orders if is_opening_order(o, ctx) and _price(ctx, o.ticker)]
        if not buys:
            return list(orders), []
        prices = marks(ctx)
        if prices is None:  # a holding has no mark: the book can't be sized (BE-45)
            return list(orders), []
        equity = ctx.portfolio.total_value(prices)
        if equity <= 0:
            return scale_opens(orders, ctx, 0.0, self.name, "no positive portfolio value")

        closes = _closes(ctx, {*ctx.portfolio.positions, *(o.ticker for o in buys)})
        returns = np.log(closes).diff()
        counts = returns.count()
        short = sorted({o.ticker for o in buys if counts.get(o.ticker, 0) < MIN_RETURNS})
        adjustments: list[RiskAdjustment] = []
        kept: list[Order] = []
        buy_ids = {id(o) for o in buys}
        for order in orders:
            if id(order) in buy_ids and order.ticker in short:
                adjustments.append(
                    adjustment(
                        order,
                        "insufficient_history",
                        0.0,
                        f"need {MIN_RETURNS} daily returns to estimate portfolio volatility",
                    )
                )
            else:
                kept.append(order)
        buys = [o for o in buys if o.ticker not in short]
        if not buys:
            return kept, adjustments

        usable = [t for t in returns.columns if counts[t] >= MIN_RETURNS]
        held = positions_after_sells(orders, ctx)
        h = np.array([held.get(t, 0.0) * prices.get(t, 0.0) / equity for t in usable])
        d = np.zeros(len(usable))
        for order in buys:
            signed = order.quantity if order.side == "buy" else -order.quantity
            d[usable.index(order.ticker)] += signed * _price(ctx, order.ticker) / equity
        cov = _covariance(returns[usable]) * _periods(ctx, usable)
        sigma = np.sqrt(np.diag(cov))

        def vol(s: float) -> float:
            w = h + s * d
            return math.sqrt(max(float(w @ cov @ w), 0.0))

        def shock(s: float) -> float:
            return float(np.abs(h + s * d) @ sigma)

        checks = []
        if settings.vol_cap is not None:
            checks.append(("portfolio_vol", vol, settings.vol_cap))
        if settings.shock_cap is not None:
            checks.append(("correlation_shock", shock, settings.shock_cap))
        tag, scale, f, cap = min(
            ((tag, largest_scale(f, cap), f, cap) for tag, f, cap in checks),
            key=lambda item: item[1],
        )
        if scale >= 1.0:
            return kept, adjustments
        reason = (
            f"forecast {tag.replace('_', ' ')} {f(1.0):.2%} > cap {cap:.2%}; "
            f"opening orders scaled by {scale:.4f}"
        )
        scaled, adj = scale_opens(kept, ctx, scale, tag, reason, only={o.ticker for o in buys})
        return scaled, adjustments + adj


def _price(ctx: RiskContext, ticker: str) -> float:
    price = ctx.prices.get(ticker)
    return price if price and price > 0 else 0.0


def _closes(ctx: RiskContext, tickers: set[str]) -> pd.DataFrame:
    series = {}
    for t in sorted(tickers):
        frame = history(ctx, t)
        if frame is not None and _price(ctx, t):
            series[t] = frame["close"]
    if not series:
        return pd.DataFrame()
    return pd.DataFrame(series).sort_index().ffill()


def _covariance(returns: pd.DataFrame) -> np.ndarray:
    """The last EWMA covariance matrix; pairs without an estimate get the
    perfectly correlated ``sigma_i * sigma_j`` (a NaN variance makes every
    cap fail, so the buys are dropped rather than sized blind)."""
    ewm = returns.ewm(span=COV_SPAN, min_periods=MIN_RETURNS).cov()
    last = ewm.loc[returns.index[-1]].reindex(index=returns.columns, columns=returns.columns)
    cov = last.to_numpy(dtype=float)
    var = np.diag(cov)
    bound = np.sqrt(np.outer(var, var))
    return np.where(np.isnan(cov), bound, cov)


def _periods(ctx: RiskContext, tickers: Sequence[str]) -> float:
    return max(periods_per_year(ctx.asset_classes.get(t, "equity")) for t in tickers)  # type: ignore[arg-type]
