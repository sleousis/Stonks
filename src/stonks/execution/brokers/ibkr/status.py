"""IBKR order statuses onto our order state machine (roadmap 19.1).

IBKR reports these strings for an order (TWS API ``orderStatus``). The
adapter (19.2) passes each one here with the filled quantity and the
time in force, and gets our ``OrderState``. Only this module knows the vendor
vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping

from stonks.core.types import TimeInForce
from stonks.execution.brokers.base import OrderState

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


def ibkr_state(
    status: str,
    *,
    filled: float = 0.0,
    time_in_force: TimeInForce | None = None,
) -> OrderState:
    """Our state for an IBKR status. A working order with a fill is
    ``partially_filled``. A cancelled opening-auction order the auction did
    not fill is ``expired``. An unknown string is ``unknown``."""
    state = IBKR_STATUS.get(status, "unknown")
    if state == "accepted" and filled > 0:
        return "partially_filled"
    if state == "cancelled" and filled <= 0 and time_in_force == "opg":
        return "expired"
    return state
