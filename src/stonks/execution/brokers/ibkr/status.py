"""IBKR order statuses onto our order state machine (roadmap 19.1).

IBKR reports these strings for an order (TWS API ``orderStatus``). The
adapter (19.2) passes each one here with the filled quantity and the
time in force, and gets our ``OrderState``. Only this module knows the vendor
vocabulary.

A cancelled order with no fill is ``cancelled`` or ``expired`` (roadmap
19.16). IBKR states who cancelled a completed order in its completed
status (``Cancelled by Trader``, ``Cancelled by System``).
:func:`cancel_origin` reads that text. A person's cancel is ``cancelled``,
the auction or the exchange ending the order is ``expired``. Without an
origin an unfilled opening-auction order is ``expired`` and anything else
``cancelled``.
"""

from __future__ import annotations

from collections.abc import Mapping

from stonks.core.types import TimeInForce
from stonks.execution.brokers.base import OrderState
from stonks.execution.brokers.ibkr.client import CancelOrigin

#: The plain mapping, before quantities are looked at.
IBKR_STATUS: Mapping[str, OrderState] = {
    # not yet sent by the API client
    "ApiPending": "pending",
    # sent, IBKR has not confirmed it
    "PendingSubmit": "submitted",
    # accepted and held at IBKR (an opening-auction order waiting, a stop)
    "PreSubmitted": "accepted",
    # working at the exchange
    "Submitted": "accepted",
    "PendingCancel": "pending_cancel",
    "ApiCancelled": "cancelled",
    "Cancelled": "cancelled",
    "Filled": "filled",
    # rejected, or held back for a reason IBKR only states in an error:
    # reconciliation must look at it before anything is sent again
    "Inactive": "unknown",
}


#: Words of IBKR's completed status that name the canceller.
_BY_TRADER = ("trader", "user", "manual")
_BY_SYSTEM = ("system", "exchange", "expired")


def cancel_origin(completed_status: str | None) -> CancelOrigin | None:
    """Who cancelled an order, from IBKR's completed status text, or
    ``None`` when the text does not say."""
    text = (completed_status or "").lower()
    if any(word in text for word in _BY_TRADER):
        return "trader"
    if any(word in text for word in _BY_SYSTEM):
        return "system"
    return None


def ibkr_state(
    status: str,
    *,
    filled: float = 0.0,
    time_in_force: TimeInForce | None = None,
    cancel_origin: CancelOrigin | None = None,
) -> OrderState:
    """Our state for an IBKR status. A working order with a fill is
    ``partially_filled``. A cancelled order with no fill is ``cancelled``
    when a person cancelled it and ``expired`` when IBKR or the exchange
    did. With no origin, an unfilled opening-auction order is ``expired``.
    An unknown string is ``unknown``."""
    state = IBKR_STATUS.get(status, "unknown")
    if state == "accepted" and filled > 0:
        return "partially_filled"
    if state == "cancelled" and filled <= 0:
        if cancel_origin == "trader":
            return "cancelled"
        if cancel_origin == "system" or time_in_force == "opg":
            return "expired"
    return state
