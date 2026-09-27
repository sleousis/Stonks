"""The ``IbClient`` protocol and the plain types it speaks (roadmap 19.2).

``IbClient`` is the narrow, blocking view of an IB Gateway session the
broker needs. It is ours: every value crossing it is one of the frozen
dataclasses below, never an ``ib_async`` object. Two implementations:

- ``IbAsyncClient`` (``ib_async_client.py``) wraps ``ib_async`` on its own
  event loop thread (``session.py``). It is the only module that imports
  the vendor library.
- ``FakeIbGateway`` (``tests/fakes/ib_gateway.py``) is an in-memory gateway
  for hermetic tests.

Errors cross the protocol as :class:`IbApiError` (an IBKR error code and its
text), :class:`IbConnectionError` (the socket is gone) or ``TimeoutError``
(no answer in time). ``errors.py`` maps them onto the broker exceptions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

#: IBKR's order actions.
IbAction = Literal["BUY", "SELL"]
#: The order types the adapter sends.
IbOrderType = Literal["MKT", "LMT", "STP", "STP LMT"]
#: IBKR's time in force codes the adapter sends.
IbTif = Literal["DAY", "GTC", "OPG", "IOC"]


class IbApiError(RuntimeError):
    """IBKR answered a request with an error code."""

    def __init__(self, code: int, message: str, *, req_id: int = -1) -> None:
        self.code = code
        self.message = message
        self.req_id = req_id
        super().__init__(f"IBKR error {code}: {message}")


class IbConnectionError(ConnectionError):
    """The gateway socket is closed or could not be opened."""


@dataclass(frozen=True)
class IbContractQuery:
    """What to ask IBKR for. ``isin`` wins when set: IBKR then matches by
    ``secIdType = ISIN`` and the symbol is only a hint."""

    symbol: str
    currency: str
    sec_type: str = "STK"
    exchange: str = "SMART"
    primary_exchange: str | None = None
    isin: str | None = None


@dataclass(frozen=True)
class IbContract:
    """A qualified IBKR contract."""

    con_id: int
    symbol: str
    sec_type: str
    currency: str
    exchange: str = "SMART"
    primary_exchange: str | None = None
    trading_class: str | None = None
    local_symbol: str | None = None


@dataclass(frozen=True)
class IbContractDetails:
    contract: IbContract
    min_tick: float
    #: Price units per currency unit (100 for a London stock quoted in pence).
    price_magnifier: int = 1
    long_name: str | None = None
    isin: str | None = None


@dataclass(frozen=True)
class IbOrderRequest:
    """An order as IBKR reads it. ``what_if`` asks for a preview only."""

    action: IbAction
    total_quantity: float
    order_type: IbOrderType
    tif: IbTif
    order_ref: str
    account: str
    limit_price: float | None = None
    aux_price: float | None = None
    outside_rth: bool = False
    transmit: bool = True
    #: One-cancels-other group (``ocaGroup``). ``oca_type`` 2: a fill of one
    #: order reduces the others by the same quantity, with overfill blocked.
    oca_group: str | None = None
    oca_type: int | None = None


@dataclass(frozen=True)
class IbTrade:
    """One order IBKR knows, open or completed."""

    order_id: int
    perm_id: int
    order_ref: str
    contract: IbContract
    action: IbAction
    total_quantity: float
    status: str
    filled: float = 0.0
    avg_fill_price: float | None = None
    tif: str | None = None
    account: str | None = None
    #: The last error IBKR sent for this order (a rejection's reason).
    reason: str | None = None


@dataclass(frozen=True)
class IbExecution:
    """One execution, with its commission once the report arrived."""

    exec_id: str
    order_ref: str
    perm_id: int
    contract: IbContract
    #: ``BOT`` or ``SLD``.
    side: str
    shares: float
    price: float
    time: datetime
    account: str | None = None
    commission: float | None = None
    commission_currency: str | None = None


@dataclass(frozen=True)
class IbPosition:
    account: str
    contract: IbContract
    position: float
    avg_cost: float = 0.0


@dataclass(frozen=True)
class IbAccountValue:
    account: str
    tag: str
    value: str
    currency: str = ""


@dataclass(frozen=True)
class IbWhatIf:
    """IBKR's preview of an order (nothing was sent)."""

    init_margin_change: float | None
    maint_margin_change: float | None
    equity_with_loan_after: float | None
    commission: float | None
    commission_currency: str | None = None
    warning: str | None = None


@dataclass(frozen=True)
class IbSnapshot:
    """A market data snapshot. ``market_data_type`` is IBKR's: 1 live,
    2 frozen, 3 delayed, 4 delayed frozen."""

    con_id: int
    last: float | None
    bid: float | None
    ask: float | None
    time: datetime
    market_data_type: int = 1


@dataclass(frozen=True)
class IbShortability:
    """What IBKR says about shorting one contract (generic tick 236).

    ``indicator`` is IBKR's shortable value: above 2.5 easy to borrow,
    above 1.5 hard to borrow (a locate is needed), else not shortable.
    ``shares`` is how many shares IBKR can lend. ``None``: no answer."""

    con_id: int
    indicator: float | None
    shares: float | None = None


@dataclass(frozen=True)
class IbLinkStatus:
    """The session as the gateway reports it.

    ``link_ok`` is false between a 1100 (the gateway lost IBKR) and a 1101
    or 1102. ``competing`` is set by a 10197 (the same username logged in
    somewhere else)."""

    connected: bool
    link_ok: bool = True
    competing: bool = False
    last_error: tuple[int, str] | None = None
    #: How many times the session connected since it was built.
    connects: int = 0


@runtime_checkable
class IbClient(Protocol):
    """A blocking view of one IB Gateway session. Every call raises
    :class:`IbApiError`, :class:`IbConnectionError` or ``TimeoutError``."""

    def connect(self) -> None:
        """Open the session (a no-op when connected). Retries with backoff
        up to the client's deadline."""
        ...

    def disconnect(self) -> None: ...

    def status(self) -> IbLinkStatus: ...

    def managed_accounts(self) -> Sequence[str]: ...

    def server_time(self) -> datetime: ...

    def contract_details(self, query: IbContractQuery) -> Sequence[IbContractDetails]: ...

    def place_order(self, contract: IbContract, order: IbOrderRequest) -> IbTrade: ...

    def cancel_order(self, order_id: int) -> None: ...

    def global_cancel(self) -> None: ...

    def open_trades(self) -> Sequence[IbTrade]: ...

    def completed_trades(self) -> Sequence[IbTrade]:
        """Orders completed in the current session (about a day back)."""
        ...

    def executions(self) -> Sequence[IbExecution]:
        """Executions of the current session (about a day back), with
        commissions where the report arrived."""
        ...

    def positions(self, account: str) -> Sequence[IbPosition]: ...

    def account_values(self, account: str) -> Sequence[IbAccountValue]: ...

    def what_if(self, contract: IbContract, order: IbOrderRequest) -> IbWhatIf: ...

    def snapshots(self, contracts: Sequence[IbContract]) -> Sequence[IbSnapshot]: ...


@runtime_checkable
class IbShortableClient(Protocol):
    """An ``IbClient`` that can also read the shortable ticks (roadmap
    19.3). A separate protocol, so a client without it still works for
    long-only accounts."""

    def shortability(self, contracts: Sequence[IbContract]) -> Sequence[IbShortability]: ...


@dataclass(frozen=True)
class IbEndpoint:
    """Where a client connects: a gateway and the process's API client id."""

    host: str
    port: int
    client_id: int
    connect_timeout: float = 5.0
    request_timeout: float = 10.0
    #: Reconnect attempts stop at this many seconds after the first failure.
    reconnect_deadline: float = 60.0
    readonly: bool = False
