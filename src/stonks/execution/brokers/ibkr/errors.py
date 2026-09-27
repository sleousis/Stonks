"""IBKR error codes onto our broker exceptions (roadmap 19.2).

``docs/design/live-trading.md`` section 2 lists the codes that matter. Only
this module knows them. :func:`classify` names what a code means and
:func:`to_broker_error` turns a client error into the exception the rest of
Stonks handles:

- an outage (502, 504, 1100, a closed socket, a timeout) is a
  ``BrokerUnavailableError``: the book skips and alerts;
- a competing session (10197) is one too, with the cause in the text;
- a rejection (201, 110 and the other order errors) is an
  ``OrderRejectedError`` with IBKR's text.
"""

from __future__ import annotations

from typing import Literal

from stonks.execution.brokers.base import (
    BrokerError,
    BrokerUnavailableError,
    OrderRejectedError,
)
from stonks.execution.brokers.ibkr.client import IbApiError, IbConnectionError

ErrorKind = Literal[
    "unavailable",
    "link_lost",
    "link_restored",
    "competing_session",
    "info",
    "rejected",
    "bad_tick",
    "no_market_data",
    "duplicate_order_id",
    "not_found",
    "other",
]

#: The codes the adapter reacts to. Anything else is ``other``.
ERROR_KINDS: dict[int, ErrorKind] = {
    502: "unavailable",  # could not connect
    504: "unavailable",  # not connected
    326: "unavailable",  # the client id is already in use
    1100: "link_lost",  # the gateway lost its link to IBKR
    1101: "link_restored",  # restored, market data lost
    1102: "link_restored",  # restored, data kept
    2103: "info",  # a market data farm is broken (it reconnects itself)
    2104: "info",  # market data farm OK
    2105: "info",  # historical data farm broken
    2106: "info",  # historical data farm OK
    2107: "info",
    2108: "info",
    2158: "info",  # sec-def data farm OK
    10197: "competing_session",
    201: "rejected",  # order rejected
    202: "rejected",  # order cancelled (with a reason)
    203: "rejected",  # the security is not allowed for this account
    110: "bad_tick",  # the price does not fit the minimum tick
    354: "no_market_data",  # no market data subscription
    10167: "no_market_data",  # delayed data shown instead
    10168: "no_market_data",
    103: "duplicate_order_id",
    135: "not_found",  # no such order id
    161: "not_found",  # cancel of an order that is not open
    200: "not_found",  # no security definition found
}


class OrderOwnedElsewhereError(BrokerError):
    """IBKR lets only the API client that placed an order (or the master
    client) cancel it, and that client id is busy (roadmap 19.17)."""


def classify(code: int) -> ErrorKind:
    return ERROR_KINDS.get(code, "other")


def is_info(code: int) -> bool:
    """A notice that is never an error (farm OK, link restored)."""
    return classify(code) in ("info", "link_restored")


def to_broker_error(exc: BaseException, *, action: str) -> BrokerError:
    """The broker exception for a client failure during ``action``."""
    if isinstance(exc, IbConnectionError):
        return BrokerUnavailableError(f"IB Gateway not reachable during {action}: {exc}")
    if isinstance(exc, TimeoutError):
        return BrokerUnavailableError(f"IB Gateway did not answer {action} in time")
    if isinstance(exc, IbApiError):
        kind = classify(exc.code)
        text = f"IBKR {exc.code} during {action}: {exc.message}"
        if kind in ("unavailable", "link_lost"):
            return BrokerUnavailableError(text)
        if kind == "competing_session":
            return BrokerUnavailableError(
                f"{text}. Someone logged in with the gateway's username elsewhere "
                "(TWS, the web portal or the phone app). Use a separate username for the API."
            )
        if kind in ("rejected", "bad_tick"):
            return OrderRejectedError(text)
        return BrokerError(text)
    return BrokerError(f"IBKR {action} failed: {type(exc).__name__}: {exc}")
