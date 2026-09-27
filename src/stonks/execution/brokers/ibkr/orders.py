"""Our ``Order`` onto an IBKR order (roadmap 19.2).

Rules, from ``docs/design/live-trading.md`` section 2:

- ``client_id`` goes out as ``orderRef``: the client id itself when it fits
  ``order_ref_max_length``, else ``stk-`` plus 20 base32 characters of its
  SHA-256 (:func:`broker_ref`). IBKR does not dedupe on it, the broker does.
- Whole shares only. The quantity is rounded down and an order below one
  share is refused.
- A market order becomes a collared limit when it carries a reference
  price (``decision_price``): the reference plus the collar for a buy,
  minus it for a sell. A bare ``MKT`` is only sent for a closing order with
  no reference (the emergency path). An opening market order with no
  reference is refused.
- Limit prices snap to the contract's minimum tick, never to a worse price:
  buys round down, sells round up.
- ``stop`` needs ``stop_price``. ``gtc`` is only for stops. Opening-auction
  (``opg``) orders are market or limit only.
- ``outside_rth`` is refused in this phase. ``account`` is always set.
"""

from __future__ import annotations

import base64
import hashlib
import math
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from stonks.core.instruments import InstrumentSpec
from stonks.core.types import Order, TimeInForce
from stonks.execution.brokers.base import OrderRejectedError
from stonks.execution.brokers.ibkr.client import IbOrderRequest, IbOrderType, IbTif
from stonks.execution.brokers.ibkr.settings import IbkrOrderSettings

#: The prefix of a hashed order reference.
HASH_PREFIX = "stk-"
_HASH_CHARS = 20

_TIF: dict[TimeInForce, IbTif] = {"day": "DAY", "gtc": "GTC", "opg": "OPG", "ioc": "IOC"}


def broker_ref(client_id: str, max_length: int) -> str:
    """The ``orderRef`` sent for ``client_id``: itself when it fits, else a
    stable hash. The same client id always gives the same reference."""
    if not client_id:
        raise ValueError("an order needs a client id")
    if len(client_id) <= max_length:
        return client_id
    digest = hashlib.sha256(client_id.encode("utf-8")).digest()
    return HASH_PREFIX + base64.b32encode(digest).decode("ascii").lower()[:_HASH_CHARS]


def whole_shares(order: Order) -> int:
    """The quantity sent: rounded down to whole shares, at least one."""
    shares = math.floor(order.quantity + 1e-9)
    if shares < 1:
        raise OrderRejectedError(
            f"{order.ticker}: {order.quantity} is below one share (IBKR orders are whole shares)"
        )
    return shares


def snap_price(price: float, tick: float, *, side: str | None) -> float:
    """``price`` on the tick grid. A buy rounds down and a sell up (never a
    worse price than asked). ``side=None`` rounds to the nearest tick."""
    if not (math.isfinite(price) and price > 0):
        raise OrderRejectedError(f"price {price!r} is not a positive number")
    step = Decimal(str(tick))
    rounding = {"buy": ROUND_FLOOR, "sell": ROUND_CEILING}.get(side or "", ROUND_HALF_UP)
    ticks = (Decimal(str(price)) / step).to_integral_value(rounding=rounding)
    snapped = float(ticks * step)
    if snapped <= 0:
        raise OrderRejectedError(f"price {price} rounds to zero on a tick of {tick}")
    return snapped


def collar_price(reference: float, side: str, collar_bps: float, tick: float) -> float:
    """The limit of a collared order: the reference plus the collar for a
    buy, minus it for a sell, on the tick grid."""
    factor = 1.0 + collar_bps / 10_000.0 if side == "buy" else 1.0 - collar_bps / 10_000.0
    return snap_price(reference * factor, tick, side=side)


def _time_in_force(order: Order, is_stop: bool, settings: IbkrOrderSettings) -> TimeInForce:
    tif = order.time_in_force or ("day" if is_stop else settings.default_time_in_force)
    if tif == "gtc" and not is_stop:
        raise OrderRejectedError(f"{order.client_id}: good-till-cancelled is only for stop orders")
    if tif == "opg" and is_stop:
        raise OrderRejectedError(f"{order.client_id}: a stop cannot join the opening auction")
    return tif


def to_ib_order(
    order: Order,
    spec: InstrumentSpec,
    *,
    account: str,
    settings: IbkrOrderSettings,
) -> IbOrderRequest:
    """The IBKR order for ``order`` on the contract ``spec`` describes.
    Raises ``OrderRejectedError`` for anything this phase does not send."""
    if order.outside_rth:
        raise OrderRejectedError(f"{order.client_id}: orders outside regular hours are refused")
    if not account:
        raise OrderRejectedError(f"{order.client_id}: no IBKR account to trade")
    shares = whole_shares(order)
    tick = spec.tick_size
    is_stop = order.order_type in ("stop", "stop_limit")
    tif = _time_in_force(order, is_stop, settings)
    limit: float | None = None
    aux: float | None = None
    order_type: IbOrderType
    if order.order_type == "market":
        reference = order.decision_price
        if reference is not None and settings.collar_bps > 0:
            order_type = "LMT"
            limit = collar_price(reference, order.side, settings.collar_bps, tick)
        elif order.position_effect == "close":
            order_type = "MKT"
        else:
            raise OrderRejectedError(
                f"{order.client_id}: an opening market order needs a reference price for its "
                "collar (decision_price)"
            )
    elif order.order_type == "limit":
        assert order.limit_price is not None  # Order checks it
        order_type = "LMT"
        limit = snap_price(order.limit_price, tick, side=order.side)
    else:
        if order.stop_price is None:
            raise OrderRejectedError(f"{order.client_id}: a stop order needs stop_price")
        aux = snap_price(order.stop_price, tick, side=None)
        if order.order_type == "stop":
            order_type = "STP"
        else:
            assert order.limit_price is not None
            order_type = "STP LMT"
            limit = snap_price(order.limit_price, tick, side=order.side)
    return IbOrderRequest(
        action="BUY" if order.side == "buy" else "SELL",
        total_quantity=float(shares),
        order_type=order_type,
        tif=_TIF[tif],
        order_ref=broker_ref(order.client_id, settings.order_ref_max_length),
        account=account,
        limit_price=limit,
        aux_price=aux,
        outside_rth=False,
    )
