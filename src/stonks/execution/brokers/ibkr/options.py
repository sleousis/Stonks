"""Options at IBKR: contracts, orders and combos (roadmap 17.8).

**Contracts.** An option position is keyed by our contract id
(``AAPL.US:2026-01-16:C:150``). :func:`option_query` asks IBKR for the
``OPT`` contract by underlying symbol, expiry, right, strike and
multiplier, ``SMART`` routed. Exactly one match whose fields agree is
kept, and the answer is cached in ``broker_contracts`` like a stock's
(keyed by the contract id). Anything else refuses the contract. US
listed options only in this phase.

**Orders.** Option orders are never market orders. Every one is a limit
(the pipeline sets it at the mid), a day order, whole contracts. A
multi-leg :class:`~stonks.core.combos.ComboOrder` goes out as one ``BAG``
order, all legs or none, at a net limit per unit: positive for a debit,
negative for a credit. The ``BAG`` action is always ``BUY`` and each leg
carries its own action, so a lower limit is always the better price.

Prices are per share of the underlying, as IBKR quotes them. Money is
``contracts x price x multiplier``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal

from stonks.core.combos import ComboOrder
from stonks.core.options import OptionContract, format_number, parse_contract_id
from stonks.core.types import Order
from stonks.execution.brokers.base import OrderRejectedError, UnsupportedTickerError
from stonks.execution.brokers.ibkr.client import (
    IbComboLeg,
    IbContract,
    IbContractDetails,
    IbContractQuery,
    IbOrderRequest,
)
from stonks.execution.brokers.ibkr.contracts import MARKETS, ib_symbol, split_ticker
from stonks.execution.brokers.ibkr.orders import broker_ref, snap_price
from stonks.execution.brokers.ibkr.settings import IbkrOrderSettings

#: The markets whose listed options trade through this adapter.
OPTION_MARKETS = frozenset({"US"})
_RIGHT_CODE = {"call": "C", "put": "P"}
_RIGHT_OF = {"C": "call", "P": "put", "CALL": "call", "PUT": "put"}


def option_query(contract: OptionContract) -> IbContractQuery:
    """The ``OPT`` lookup for one of our contracts."""
    base, suffix = split_ticker(contract.underlying)
    if suffix not in OPTION_MARKETS:
        raise UnsupportedTickerError(
            f"{contract.contract_id}: options on {suffix} do not trade through IBKR here"
        )
    market = MARKETS[suffix]
    return IbContractQuery(
        symbol=ib_symbol(base, market),
        currency=contract.currency or market.currency,
        sec_type="OPT",
        last_trade_date=f"{contract.expiry:%Y%m%d}",
        strike=contract.strike,
        right=_RIGHT_CODE[contract.right],
        multiplier=format_number(contract.multiplier),
    )


def _expiry_of(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return datetime.strptime(text[:8], "%Y%m%d").date()
    except ValueError:
        return None


def matches(details: IbContractDetails, contract: OptionContract) -> bool:
    """IBKR's contract is exactly ours: an option on the same expiry, right,
    strike, multiplier and currency."""
    c = details.contract
    if c.sec_type != "OPT" or c.currency != contract.currency:
        return False
    if _expiry_of(c.last_trade_date) != contract.expiry:
        return False
    if _RIGHT_OF.get((c.right or "").upper()) != contract.right:
        return False
    if c.strike is None or not math.isclose(c.strike, contract.strike, abs_tol=1e-6):
        return False
    try:
        multiplier = float(c.multiplier or "100")
    except ValueError:
        return False
    return math.isclose(multiplier, contract.multiplier, abs_tol=1e-9)


def option_id_for_contract(c: IbContract) -> str | None:
    """Our contract id for an IBKR ``OPT`` contract (a position placed by
    hand, say). ``None`` when it is not a US listed option we can name."""
    if c.sec_type != "OPT" or c.currency != "USD":
        return None
    expiry = _expiry_of(c.last_trade_date)
    right = _RIGHT_OF.get((c.right or "").upper())
    if expiry is None or right is None or c.strike is None or c.strike <= 0:
        return None
    try:
        multiplier = float(c.multiplier or "100")
    except ValueError:
        return None
    base = (c.symbol or "").strip().upper().replace(" ", "-")
    if not base:
        return None
    return OptionContract(
        underlying=f"{base}.US",
        expiry=expiry,
        strike=float(c.strike),
        right=right,  # type: ignore[arg-type]
        multiplier=multiplier,
    ).contract_id


def whole_contracts(quantity: float, what: str) -> int:
    contracts = math.floor(quantity + 1e-9)
    if contracts < 1 or abs(quantity - contracts) > 1e-9:
        raise OrderRejectedError(f"{what}: {quantity:g} is not a whole number of contracts")
    return contracts


def _check_tif(order_tif: str | None, what: str) -> None:
    if order_tif not in (None, "day"):
        raise OrderRejectedError(f"{what}: option orders are day orders, not {order_tif}")


def to_ib_option_order(
    order: Order, *, tick: float, account: str, settings: IbkrOrderSettings
) -> IbOrderRequest:
    """One option leg as an IBKR order: a day limit, whole contracts."""
    if not account:
        raise OrderRejectedError(f"{order.client_id}: no IBKR account to trade")
    if order.outside_rth:
        raise OrderRejectedError(f"{order.client_id}: orders outside regular hours are refused")
    if order.order_type != "limit" or order.limit_price is None:
        raise OrderRejectedError(
            f"{order.client_id}: option orders are limit orders at the mid, never "
            f"{order.order_type} orders"
        )
    _check_tif(order.time_in_force, order.client_id)
    return IbOrderRequest(
        action="BUY" if order.side == "buy" else "SELL",
        total_quantity=float(whole_contracts(order.quantity, order.client_id)),
        order_type="LMT",
        tif="DAY",
        order_ref=broker_ref(order.client_id, settings.order_ref_max_length),
        account=account,
        limit_price=snap_price(order.limit_price, tick, side=order.side),
    )


def snap_net(price: float, tick: float) -> float:
    """A combo's net limit on the tick grid, never a worse price: the
    ``BAG`` is always bought, so it rounds down (a credit grows)."""
    if not math.isfinite(price):
        raise OrderRejectedError(f"net limit {price!r} is not a number")
    step = Decimal(str(tick))
    ticks = (Decimal(str(price)) / step).to_integral_value(rounding=ROUND_FLOOR)
    return float(ticks * step)


def combo_contract(combo: ComboOrder, con_ids: Sequence[int], currency: str) -> IbContract:
    """The ``BAG`` contract of ``combo``: one leg per combo leg, in order."""
    if len(con_ids) != len(combo.legs):
        raise OrderRejectedError(f"{combo.client_id}: a contract for every leg is needed")
    legs: list[IbComboLeg] = []
    for leg, con_id in zip(combo.legs, con_ids, strict=True):
        ratio = leg.ratio
        if abs(ratio - round(ratio)) > 1e-9 or round(ratio) < 1:
            raise OrderRejectedError(f"{combo.client_id}: leg ratios must be whole numbers")
        legs.append(
            IbComboLeg(
                con_id=con_id, ratio=round(ratio), action="BUY" if leg.side == "buy" else "SELL"
            )
        )
    underlyings = sorted(combo.underlyings)
    if len(underlyings) != 1:
        raise OrderRejectedError(f"{combo.client_id}: a combo trades one underlying")
    base, _ = split_ticker(underlyings[0])
    return IbContract(
        con_id=0,
        symbol=ib_symbol(base, MARKETS["US"]),
        sec_type="BAG",
        currency=currency,
        exchange="SMART",
        combo_legs=tuple(legs),
    )


def to_ib_combo_order(
    combo: ComboOrder, *, tick: float, account: str, settings: IbkrOrderSettings
) -> IbOrderRequest:
    """A combo as one ``BAG`` day limit order at its net limit per unit."""
    if not account:
        raise OrderRejectedError(f"{combo.client_id}: no IBKR account to trade")
    if combo.net_limit is None:
        raise OrderRejectedError(
            f"{combo.client_id}: a combo needs a net limit at the mid, never a market order"
        )
    return IbOrderRequest(
        action="BUY",
        total_quantity=float(whole_contracts(combo.quantity, combo.client_id)),
        order_type="LMT",
        tif="DAY",
        order_ref=broker_ref(combo.client_id, settings.order_ref_max_length),
        account=account,
        limit_price=snap_net(combo.net_limit, tick),
    )


def contract_of(ticker: str) -> OptionContract:
    """Our option contract for a contract id (raises ``UnsupportedTickerError``)."""
    try:
        return parse_contract_id(ticker)
    except ValueError as exc:
        raise UnsupportedTickerError(f"{ticker!r} is not an option contract id") from exc
