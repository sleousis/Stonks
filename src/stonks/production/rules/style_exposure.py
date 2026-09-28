"""Style exposure cap (roadmap 22.4): the book's net exposure to each style
factor stays within ``max_abs_exposure``.

A style exposure is ``sum_i w_i z_i``: ``w_i`` the signed weight of name
``i`` (value over the book's value before the tick) and ``z_i`` its
standardised style score (:func:`stonks.portfolio.factor_model.standardize_exposures`,
mean 0 and standard deviation 1 across the names the context knows). An
exposure of 0.5 means the book leans half a standard deviation towards
that style.

The book after the tick is ``h + s * d``: ``h`` the holdings once the
sells go through, ``d`` the opening orders. The rule keeps the largest
``s`` in ``[0, 1]`` with every configured style inside the cap. When the
holdings alone are over it, opening orders may still go as long as they
leave no style further out than before. Sells and covers always pass.

Scores come from ``ctx.factor_exposures`` (the factor library, read point
in time by :func:`stonks.factors.style.style_exposures`) when the caller
filled it, else from the context's bars (:func:`bars_style_exposures`:
12-1 momentum, 60-bar volatility and log dollar volume, the same formulas
as the library). A name with no score sits at the average and cannot move
an exposure. Off unless ``max_abs_exposure`` is set.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.portfolio.factor_model import SECTOR, STYLE_FACTORS, standardize_exposures
from stonks.production.rules import RiskAdjustment, RiskContext, RiskRule, register_rule
from stonks.production.rules._common import (
    is_opening_order,
    largest_scale,
    marks,
    positions_after_sells,
    scale_opens,
    settings_of,
)

Style = Literal["momentum", "size", "value", "volatility"]

#: Bars behind each score (as in ``mom_12_1``, ``low_vol_60`` and ``size_dv_60``).
MOMENTUM_BARS = 252
MOMENTUM_SKIP = 21
WINDOW_BARS = 60
MIN_BARS = 21


class StyleExposureSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Max absolute net exposure to any configured style (e.g. 0.5).
    max_abs_exposure: float | None = Field(default=None, gt=0.0)
    #: The styles the cap applies to.
    styles: tuple[Style, ...] = STYLE_FACTORS  # type: ignore[assignment]

    @property
    def active(self) -> bool:
        return self.max_abs_exposure is not None and bool(self.styles)


def wants_exposures(policy: Any) -> bool:
    """``policy`` switches the rule on, so a caller with a lake should fill
    ``RiskContext.factor_exposures``."""
    settings = settings_of(policy, "style_exposure")
    return settings is not None and settings.active


def union_styles(a: Sequence[str], b: Sequence[str]) -> tuple[str, ...]:
    """Capping more styles is tighter."""
    chosen = {*a, *b}
    return tuple(s for s in STYLE_FACTORS if s in chosen)


def _momentum(close: pd.Series) -> float:
    if len(close) <= MOMENTUM_SKIP + MIN_BARS:
        return math.nan
    start = close.iloc[-(MOMENTUM_BARS + 1)] if len(close) > MOMENTUM_BARS else close.iloc[0]
    end = close.iloc[-(MOMENTUM_SKIP + 1)]
    return float(end / start - 1.0) if start > 0 else math.nan


def bars_style_exposures(
    history: Mapping[str, pd.DataFrame],
    sectors: Mapping[str, str],
    *,
    as_of: date | None = None,
) -> pd.DataFrame:
    """Raw momentum, size and volatility per ticker from adjusted daily bars
    dated up to ``as_of``, plus ``sector``. Rows tickers."""
    rows: dict[str, dict[str, Any]] = {}
    for ticker, frame in history.items():
        if frame is None or frame.empty or "close" not in frame:
            continue
        if as_of is not None:
            frame = frame[frame.index <= pd.Timestamp(as_of)]
        close = pd.Series(frame["close"], dtype=float)
        close = close.loc[close.to_numpy() > 0]
        if len(close) < MIN_BARS:
            continue
        recent = frame.loc[close.index].tail(WINDOW_BARS)
        returns = close.pct_change().dropna().tail(WINDOW_BARS)
        volume = recent.get("volume", np.nan)
        dollar = float(np.nanmean(recent["close"].to_numpy(float) * np.asarray(volume, float)))
        rows[str(ticker)] = {
            "momentum": _momentum(close),
            "size": math.log(dollar) if math.isfinite(dollar) and dollar > 0 else math.nan,
            "volatility": float(np.std(returns.to_numpy(float), ddof=1))
            if len(returns) > 1
            else math.nan,
            SECTOR: sectors.get(str(ticker)),
        }
    return pd.DataFrame.from_dict(rows, orient="index")


def _scores(ctx: RiskContext, styles: Sequence[str]) -> pd.DataFrame:
    raw = ctx.factor_exposures
    if raw is None:
        raw = bars_style_exposures(ctx.history, ctx.sectors, as_of=ctx.as_of)
    columns = [s for s in styles if s in raw.columns]
    if raw.empty or not columns:
        return pd.DataFrame()
    return standardize_exposures(pd.DataFrame(raw[columns]))


@register_rule
class StyleExposure(RiskRule):
    name = "style_exposure"
    order = 3
    #: Scores come from the context's exposures or bars.
    needs_history = True

    def enabled(self, policy: Any) -> bool:
        settings = settings_of(policy, self.name)
        return settings is not None and settings.active

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        settings: StyleExposureSettings | None = settings_of(ctx.policy, self.name)
        if settings is None or not settings.active or settings.max_abs_exposure is None:
            return list(orders), []
        opens = [o for o in orders if is_opening_order(o, ctx) and ctx.prices.get(o.ticker)]
        if not opens:
            return list(orders), []
        scores = _scores(ctx, settings.styles)
        prices = marks(ctx)
        if scores.empty or prices is None:
            return list(orders), []
        equity = ctx.portfolio.total_value(prices)
        if equity <= 0:
            return list(orders), []
        names = list(scores.index)
        z = scores.to_numpy(dtype=float)
        held = positions_after_sells(orders, ctx)
        h = np.array([held.get(t, 0.0) * prices.get(t, 0.0) / equity for t in names])
        d = np.zeros(len(names))
        position = {t: i for i, t in enumerate(names)}
        for order in opens:
            i = position.get(order.ticker)
            if i is not None:
                signed = order.quantity if order.side == "buy" else -order.quantity
                d[i] += signed * float(ctx.prices[order.ticker]) / equity

        def exposure(s: float) -> np.ndarray:
            return z.T @ (h + s * d)

        def worst(s: float) -> float:
            return float(np.max(np.abs(exposure(s))))

        cap = max(settings.max_abs_exposure, worst(0.0))
        scale = largest_scale(worst, cap)
        if scale < 1e-9:  # bisection noise when the holdings sit on the cap
            scale = 0.0
        if scale >= 1.0:
            return list(orders), []
        after = exposure(1.0)
        style = str(scores.columns[int(np.argmax(np.abs(after)))])
        reason = (
            f"{style} exposure {after[int(np.argmax(np.abs(after)))]:+.3f} over the "
            f"{settings.max_abs_exposure:.3f} cap; opening orders scaled by {scale:.4f}"
        )
        moved = {o.ticker for o in opens if o.ticker in position}
        return scale_opens(orders, ctx, scale, self.name, reason, only=moved)
