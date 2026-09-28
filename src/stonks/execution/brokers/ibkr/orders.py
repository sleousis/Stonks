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
- An intraday book (``intraday=True``, roadmap 21.2.3) sends day orders:
  no time in force means ``day``, never the opening auction, and ``opg``
  or ``gtc`` are refused. ``ioc`` stays allowed.
- An order with an ``oca_group`` (a protective stop and the exits of its
  position, roadmap 19.10) goes out with OCA type 2: a fill of one reduces
  the others by the filled quantity, and IBKR blocks an overfill.
- Prices arrive in the currency's major unit and leave in IBKR's price
  unit: times the contract's ``price_magnifier`` (pounds to pence for
  London), snapped on IBKR's own tick grid (roadmap 19.16).
- An order with an execution algo (``Order.algo``, roadmap 23.16) goes out
  with IBKR's ``algoStrategy`` and ``algoParams`` (Adaptive, VWAP, TWAP).
  It is a limit or collared market order, never a stop, and a day order:
  no opening auction, no good-till-cancelled, no immediate-or-cancel.
"""

from __future__ import annotations

import base64
import hashlib
import math
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from stonks.core.instruments import InstrumentSpec
from stonks.core.types import Order, TimeInForce
from stonks.execution.algos import AlgoParamsError, algo_name_of, native_of
from stonks.execution.brokers.base import OrderRejectedError
from stonks.execution.brokers.ibkr.client import IbOrderRequest, IbOrderType, IbTif
from stonks.execution.brokers.ibkr.contracts import to_ib_price
from stonks.execution.brokers.ibkr.settings import IbkrOrderSettings

#: The prefix of a hashed order reference.
HASH_PREFIX = "stk-"
_HASH_CHARS = 20

_TIF: dict[TimeInForce, IbTif] = {"day": "DAY", "gtc": "GTC", "opg": "OPG", "ioc": "IOC"}
#: IBKR's OCA type: reduce the other orders by the filled quantity, with
#: overfill protection (block).
OCA_REDUCE_WITH_BLOCK = 2


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


#: What an intraday book may send: orders that end with the session.
INTRADAY_TIFS: frozenset[TimeInForce] = frozenset({"day", "ioc"})


def _time_in_force(
    order: Order, is_stop: bool, settings: IbkrOrderSettings, *, intraday: bool = False
) -> TimeInForce:
    if intraday:
        tif = order.time_in_force or "day"
        if tif not in INTRADAY_TIFS:
            raise OrderRejectedError(
                f"{order.client_id}: an intraday book sends day orders only, not {tif}"
            )
        return tif
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
    price_magnifier: int = 1,
    intraday: bool = False,
) -> IbOrderRequest:
    """The IBKR order for ``order`` on the contract ``spec`` describes.
    ``spec`` and the order's prices are in the major unit, and the request
    is in IBKR's (times ``price_magnifier``). ``intraday`` sends a day
    order (see the module doc). Raises ``OrderRejectedError`` for anything
    this phase does not send."""
    if order.outside_rth:
        raise OrderRejectedError(f"{order.client_id}: orders outside regular hours are refused")
    if not account:
        raise OrderRejectedError(f"{order.client_id}: no IBKR account to trade")
    shares = whole_shares(order)
    mag = max(1, price_magnifier)
    tick = to_ib_price(spec.tick_size, mag)

    def ib(price: float) -> float:
        return to_ib_price(price, mag)

    is_stop = order.order_type in ("stop", "stop_limit")
    algo_strategy, algo_params = _algo(order, is_stop)
    if algo_strategy is not None:
        tif: TimeInForce = "day"
    else:
        tif = _time_in_force(order, is_stop, settings, intraday=intraday)
    limit: float | None = None
    aux: float | None = None
    order_type: IbOrderType
    if order.order_type == "market":
        reference = order.decision_price
        if reference is not None and settings.collar_bps > 0:
            order_type = "LMT"
            limit = collar_price(ib(reference), order.side, settings.collar_bps, tick)
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
        limit = snap_price(ib(order.limit_price), tick, side=order.side)
    else:
        if order.stop_price is None:
            raise OrderRejectedError(f"{order.client_id}: a stop order needs stop_price")
        aux = snap_price(ib(order.stop_price), tick, side=None)
        if order.order_type == "stop":
            order_type = "STP"
        else:
            assert order.limit_price is not None
            order_type = "STP LMT"
            limit = snap_price(ib(order.limit_price), tick, side=order.side)
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
        oca_group=order.oca_group,
        oca_type=OCA_REDUCE_WITH_BLOCK if order.oca_group else None,
        algo_strategy=algo_strategy,
        algo_params=algo_params,
    )


def _algo(order: Order, is_stop: bool) -> tuple[str | None, tuple[tuple[str, str], ...]]:
    """IBKR's ``algoStrategy`` and ``algoParams`` for ``order.algo``, or
    ``(None, ())`` for a plain order."""
    name = algo_name_of(order.algo)
    if name is None or order.algo is None:
        return None, ()
    if is_stop:
        raise OrderRejectedError(f"{order.client_id}: a stop order cannot use the {name} algo")
    if order.time_in_force not in (None, "day"):
        raise OrderRejectedError(
            f"{order.client_id}: an algo order is a day order, not {order.time_in_force}"
        )
    try:
        native = native_of(order.algo)
    except AlgoParamsError as exc:
        raise OrderRejectedError(f"{order.client_id}: {exc}") from None
    return native.strategy, native.params
