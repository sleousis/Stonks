"""Typed views of a tick's ``summary_json`` (written by
:func:`stonks.production.tick.run_tick`).

The summary is a free-form dict in the store; these models give transports
a schema for the keys the tick writes today. Unknown keys are kept
(``extra="allow"``) so a newer tick never fails to render.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from stonks.app.orders import OrderView
from stonks.app.ticks import TickRunView


class RiskAdjustmentView(BaseModel):
    """One order the risk policy clipped or dropped."""

    ticker: str
    side: str
    rule: str
    original_quantity: float
    adjusted_quantity: float
    reason: str


class ShadowOutcomeView(BaseModel):
    """How one shadow strategy was evaluated during the tick."""

    strategy_id: str
    status: str
    decisions: int = 0
    fills: int = 0
    total_value: float | None = None
    error: str | None = None


class TickSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    #: ``no_candidates`` when nothing was ranked (a no-op tick).
    reason: str | None = None
    #: Set when the tick crashed.
    error: str | None = None
    error_type: str | None = None
    winner_strategy_id: str | None = None
    #: Set with ``reason="no_candidates"`` when the tick only ran exits for
    #: this strategy's positions (no winner was traded).
    exit_strategy_id: str | None = None
    winner_expected_return: float | None = None
    orders_placed: int | None = None
    fills: int | None = None
    #: A single-book tick: the portfolio the book-level keys belong to.
    #: Shown only to that portfolio's owner, like those keys.
    portfolio_id: str | None = None
    #: Tickers whose buys were dropped because their latest close was stale.
    stale_buys_dropped: list[str] = []
    #: A scoped tick: tickers outside its universe it left alone.
    outside_universe_skipped: list[str] = []
    #: Corporate actions waiting for their ex-date bar.
    deferred_corporate_actions: list[dict[str, Any]] = []
    risk_adjustments: list[RiskAdjustmentView] = []
    shadow: list[ShadowOutcomeView] = []
    shadow_error: str | None = None


class TickRun(TickRunView):
    summary: TickSummary | None  # type: ignore[assignment]


class TickRunWithOrders(TickRun):
    orders: list[OrderView]


def typed_tick_run(view: Any) -> TickRun:
    """A ``TickRunView`` (or ``TickRunDetail``) with its summary parsed."""
    data = view.model_dump()
    if "orders" in data:
        return TickRunWithOrders.model_validate(data)
    return TickRun.model_validate(data)
