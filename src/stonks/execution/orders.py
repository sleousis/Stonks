"""Order-id helpers shared by backtest and production.

``make_client_id`` assembles a deterministic, idempotency-friendly identifier
from the (as_of date, strategy, ticker, side) tuple. It deliberately does not
include the per-run ``tick_id``: a crashed tick re-run for the same ``as_of``
must reproduce the same client_ids so already-submitted orders can be
recognized and skipped. Any broker honoring the Broker Protocol must
short-circuit duplicate submissions of the same client_id.

A portfolio other than the default one adds its id
(``<as_of>:<portfolio_id>:<strategy>:<ticker>:<side>``), so two portfolios
trading the same strategy and ticker on one day never share a client id.
The default portfolio keeps the pre-accounts format, so a same-day re-run
across the accounts upgrade still recognises its orders.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from typing import Literal

from stonks.core.types import Order, OrderSide

#: The side segment of a client id. Long-only orders keep ``buy`` and
#: ``sell``, so their ids never change; a short sale is ``short`` and a
#: cover ``cover`` (``docs/design/shorting.md`` section 2).
SideToken = Literal["buy", "sell", "short", "cover"]
#: Relative excess over the holding that is float dust, not a new position.
_DUST = 1e-9


def make_client_id(
    *,
    as_of: date,
    strategy_id: str,
    ticker: str,
    side: SideToken | OrderSide,
    portfolio_id: str | None = None,
) -> str:
    """Return ``"<as_of ISO date>:<strategy_id>:<ticker>:<side>"``, with
    ``<portfolio_id>:`` after the date for a non-default portfolio
    (``None`` or the default portfolio: the plain format).

    Stable across re-runs of the same trading day, distinct across days,
    portfolios, strategies, tickers and sides.
    """
    # Imported here: the accounts package imports config, which reaches
    # modules that import this one.
    from stonks.accounts.models import DEFAULT_PORTFOLIO_ID

    if portfolio_id is not None and portfolio_id != DEFAULT_PORTFOLIO_ID:
        return f"{as_of.isoformat()}:{portfolio_id}:{strategy_id}:{ticker}:{side}"
    return f"{as_of.isoformat()}:{strategy_id}:{ticker}:{side}"


def side_token(order: Order) -> SideToken:
    """``short`` for a sell that opens, ``cover`` for a buy that closes,
    else the order's side."""
    if order.side == "sell" and order.position_effect == "open":
        return "short"
    if order.side == "buy" and order.position_effect == "close":
        return "cover"
    return order.side


def classify(order: Order, held: float) -> list[Order]:
    """``order`` with its position effect set against ``held`` (signed
    quantity), split in two when it crosses zero: long 100, sell 150 gives
    sell-close 100 then sell-open 50. Each leg's client id carries its
    side token (:func:`side_token`); the leg whose token is the order's own
    side keeps the id unchanged, so long-only orders keep their ids.

    A sell or buy within float dust of the holding closes exactly what is
    held and opens nothing."""
    sign = 1.0 if order.side == "buy" else -1.0
    # Shares of the order that reduce the current position.
    closable = max(-held, 0.0) if sign > 0 else max(held, 0.0)
    close_qty = min(order.quantity, closable)
    open_qty = order.quantity - close_qty
    if open_qty <= _DUST * max(order.quantity, 1.0) and close_qty > 0:
        close_qty, open_qty = order.quantity, 0.0
    legs: list[Order] = []
    if close_qty > 0:
        legs.append(_leg(order, close_qty, "close"))
    if open_qty > 0:
        legs.append(_leg(order, open_qty, "open"))
    return legs


def _leg(order: Order, quantity: float, effect: Literal["open", "close"]) -> Order:
    leg = replace(order, quantity=quantity, position_effect=effect)
    token = side_token(leg)
    return replace(leg, client_id=retoken(order.client_id, order, token))


def retoken(client_id: str, order: Order, token: SideToken) -> str:
    """``client_id`` with its side segment swapped for ``token``. An id not
    ending in a side segment gets ``#<token>`` appended (only when the
    token differs from the order's side)."""
    current = side_token(order)
    if token == current:
        return client_id
    for suffix in (current, order.side):
        if client_id.endswith(f":{suffix}"):
            return f"{client_id[: -len(suffix)]}{token}"
    return f"{client_id}#{token}"


def classify_all(orders: Sequence[Order], positions: Mapping[str, float]) -> list[Order]:
    """Every order classified in turn against the positions the orders
    before it leave (:func:`classify`). An order that already carries a
    position effect passes through unchanged, so running this twice is
    safe."""
    held = dict(positions)
    out: list[Order] = []
    for order in orders:
        legs = (
            [order]
            if order.position_effect is not None
            else classify(order, held.get(order.ticker, 0.0))
        )
        for leg in legs:
            out.append(leg)
            sign = 1.0 if leg.side == "buy" else -1.0
            held[leg.ticker] = held.get(leg.ticker, 0.0) + sign * leg.quantity
    return out
