"""The live stage guard (roadmap 19.9): the one rule for real-money orders.

At a broker that trades real money, an order that may open a position needs
the portfolio at ``live_small`` or higher. A close (a sell that does not
open a short, or a buy marked as covering one) goes out at any stage, so a
demoted book can always wind down (P28, P40).

The rule sits on the broker object, so every order path meets it: the tick,
manual orders, tickets, algo slices, protective stops and the intraday
engine. :func:`guard_live_stage` is applied where real brokers are built:
``execution.brokers.make_broker`` (the default portfolio's broker) and
``ConnectionService.open_trader`` (a linked account's trader). The IBKR
adapter checks the same rule itself (:func:`stage_refusal`), since it is
also built directly.

A broker says whether it trades real money with a ``real_money``
attribute (IBKR: a live gateway, Alpaca: the live endpoint). A broker
without one counts as real money, so an unknown adapter fails safe.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from stonks.core.types import Order
from stonks.execution.brokers.base import StageRefusedError
from stonks.production.live.stages import REAL_MONEY

#: Reads the portfolio's live stage at each call (``None``: unknown).
StageLookup = Callable[[], "str | None"]

_GUARDED = "_stonks_stage_guarded"


def order_closes(order: Order) -> bool:
    """The order only reduces a position: a sell that does not open a
    short, or a buy marked as a cover."""
    return order.position_effect == "close" or (
        order.side == "sell" and order.position_effect != "open"
    )


def stage_refusal(order: Order, stage: str | None) -> str | None:
    """Why ``order`` may not go to a real-money broker at ``stage``, or
    ``None`` when it may."""
    if order_closes(order) or stage in REAL_MONEY:
        return None
    return (
        "real-money orders that open need the portfolio at stage live_small or higher"
        f" (it is {stage or 'unknown'})"
    )


def ensure_stage_allows(order: Order, lookup: StageLookup | None) -> None:
    """Raise :class:`StageRefusedError` (a rejection, and a
    ``LiveTradingRefusedError``) when ``order`` may not go to
    a real-money broker now. A stage that cannot be read refuses opens."""
    if order_closes(order):
        return
    try:
        stage = lookup() if lookup is not None else None
    except Exception as exc:
        raise StageRefusedError(
            f"the portfolio's live stage could not be read ({exc}): refusing to open"
        ) from exc
    reason = stage_refusal(order, stage)
    if reason is not None:
        raise StageRefusedError(reason)


def trades_real_money(broker: object) -> bool:
    """The broker's own answer, or ``True`` when it gives none."""
    flag = getattr(broker, "real_money", None)
    return True if flag is None else bool(flag)


def guard_live_stage[B](broker: B, lookup: StageLookup | None) -> B:
    """Put the stage rule in front of ``broker.place_order``, on this very
    object, so every capability check (``isinstance`` against the broker
    protocols) still sees the adapter. Guarding twice is a no-op."""
    if getattr(broker, _GUARDED, False):
        return broker
    send: Callable[..., Any] = broker.place_order  # type: ignore[attr-defined]

    def place_order(order: Order, *args: Any, **kwargs: Any) -> Any:
        if trades_real_money(broker):
            ensure_stage_allows(order, lookup)
        return send(order, *args, **kwargs)

    setattr(broker, "place_order", place_order)  # noqa: B010 - an instance attribute
    setattr(broker, _GUARDED, True)
    return broker


def portfolio_stage_lookup(state: Any, portfolio_id: str) -> StageLookup:
    """The portfolio's live stage, read from ``state`` at each call
    (``None`` before migration 037 or for an unknown portfolio)."""
    from stonks.production.live.stages import get_stage, stages_enabled

    def lookup() -> str | None:
        if not stages_enabled(state):
            return None
        rows = state.sql("SELECT 1 FROM portfolios WHERE id = ?", [portfolio_id])
        return get_stage(state, portfolio_id) if rows else None

    return lookup


def state_stage_lookup(state_path: Any, portfolio_id: str) -> StageLookup:
    """The same, opening the state DB at each call (for a broker that
    outlives the caller's connection)."""
    from stonks.store.state import SqliteState

    def lookup() -> str | None:
        with SqliteState(state_path) as state:
            return portfolio_stage_lookup(state, portfolio_id)()

    return lookup
