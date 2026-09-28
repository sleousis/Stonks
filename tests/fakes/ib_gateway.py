"""``FakeIbGateway``: an in-memory IB Gateway behind the ``IbClient`` protocol
(roadmap 19.2, ``docs/design/live-trading.md`` section 8).

Scriptable: fills and partial fills, auction no-fills, rejections with IBKR
codes, late commission reports, a disconnect or timeout in the middle of a
submit, a gateway restart that renumbers order ids, a competing session,
a lost link to IBKR, delayed quotes, missing market data and a what-if
timeout. No network.

Options (roadmap 17.8): option contracts (:func:`option`), chain
parameters, option quotes with Greeks, combo (``BAG``) orders filled leg by
leg (:meth:`FakeIbGateway.fill_combo`), and assignments, exercises and
expiries (:meth:`FakeIbGateway.option_event`).

Client ids (roadmap 19.17): the gateway is itself API client ``client_id``
(11, the tick's). :meth:`FakeIbGateway.session` opens another client of the
same gateway with its own id and socket. A client id connects once at a
time. A client sees and cancels only its own orders, except the master
client (``master_client_id``, the IB Gateway setting), which sees and may
cancel every order. ``all_open_trades`` (``reqAllOpenOrders``) and
``global_cancel`` (``reqGlobalCancel``) reach every order from any client.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any, Literal

from stonks.execution.brokers.ibkr.client import (
    CancelOrigin,
    IbAccountValue,
    IbApiError,
    IbConnectionError,
    IbContract,
    IbContractDetails,
    IbContractQuery,
    IbExecution,
    IbLinkStatus,
    IbOptionEvent,
    IbOptionEventKind,
    IbOptionParams,
    IbOptionSnapshot,
    IbOrderRequest,
    IbPosition,
    IbShortability,
    IbSnapshot,
    IbTrade,
    IbWhatIf,
)

T0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
TERMINAL = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})

SubmitFault = Literal["disconnect_before", "disconnect_after", "timeout_after"]


def stock(
    con_id: int,
    symbol: str,
    *,
    currency: str = "USD",
    primary: str | None = "NASDAQ",
    min_tick: float = 0.01,
    isin: str | None = None,
    magnifier: int = 1,
) -> IbContractDetails:
    return IbContractDetails(
        contract=IbContract(
            con_id=con_id,
            symbol=symbol,
            sec_type="STK",
            currency=currency,
            exchange="SMART",
            primary_exchange=primary,
            trading_class=symbol,
        ),
        min_tick=min_tick,
        price_magnifier=magnifier,
        isin=isin,
    )


def option(
    con_id: int,
    symbol: str,
    expiry: date,
    right: Literal["C", "P"],
    strike: float,
    *,
    multiplier: str = "100",
    min_tick: float = 0.01,
    trading_class: str | None = None,
) -> IbContractDetails:
    """A listed US option (roadmap 17.8)."""
    return IbContractDetails(
        contract=IbContract(
            con_id=con_id,
            symbol=symbol,
            sec_type="OPT",
            currency="USD",
            exchange="SMART",
            trading_class=trading_class or symbol,
            last_trade_date=f"{expiry:%Y%m%d}",
            strike=strike,
            right=right,
            multiplier=multiplier,
        ),
        min_tick=min_tick,
    )


AAPL = stock(265598, "AAPL", isin="US0378331005")
MSFT = stock(272093, "MSFT", isin="US5949181045")
BRKB = stock(72063691, "BRK B", primary="NYSE")
VOD = stock(9999, "VOD", currency="GBP", primary="LSE", magnifier=100)


#: The algos IBKR accepts here, with the order types each takes.
_ALGO_ORDER_TYPES: dict[str, frozenset[str]] = {
    "Adaptive": frozenset({"MKT", "LMT"}),
    "Vwap": frozenset({"MKT", "LMT"}),
    "Twap": frozenset({"MKT", "LMT"}),
}


def _algo_rejection(order: IbOrderRequest) -> tuple[int, str] | None:
    """What IBKR answers an algo order it would refuse, as (code, text)."""
    name = order.algo_strategy or ""
    kinds = _ALGO_ORDER_TYPES.get(name)
    if kinds is None:
        return 442, f"Invalid algo strategy: {name}"
    if order.order_type not in kinds:
        return 442, f"{name} does not take {order.order_type} orders"
    if order.tif != "DAY":
        return 442, f"{name} orders must be DAY orders"
    params = dict(order.algo_params)
    if name == "Adaptive" and params.get("adaptivePriority") not in ("Patient", "Normal", "Urgent"):
        return 442, "Invalid adaptivePriority"
    if name == "Vwap":
        pct = float(params.get("maxPctVol", "0.1"))
        if not 0.0 < pct <= 0.5:
            return 442, "maxPctVol must be above 0 and at most 0.5"
    start, end = params.get("startTime"), params.get("endTime")
    if start and end:
        fmt = "%Y%m%d-%H:%M:%S"
        if datetime.strptime(end, fmt) <= datetime.strptime(start, fmt):
            return 442, "endTime must be after startTime"
    return None


class FakeIbGateway:
    def __init__(
        self,
        accounts: Sequence[str] = ("DU1234567",),
        *,
        contracts: Sequence[IbContractDetails] = (AAPL, MSFT, BRKB, VOD),
        now: datetime = T0,
        client_id: int = 11,
        master_client_id: int | None = 11,
    ) -> None:
        #: this client's API client id, and the gateway's master client id
        self.client_id = client_id
        self.master_client_id = master_client_id
        self.sessions: list[FakeIbSession] = []
        #: the client a session call runs as (``None``: the gateway itself)
        self._viewer: FakeIbSession | None = None
        self.accounts = list(accounts)
        self.contracts = list(contracts)
        self.now = now
        self.connected = False
        self.connects = 0
        self.link_ok = True
        self.competing = False
        self.last_error: tuple[int, str] | None = None
        #: connect() fails this many times first
        self.connect_failures = 0
        self.submit_fault: SubmitFault | None = None
        self.reject_next: tuple[int, str] | None = None
        self.what_if_timeout = False
        self.what_if_result: IbWhatIf | None = None
        #: conId -> (initial, maintenance margin per share, warning)
        self.what_if_rates: dict[int, tuple[float, float, str | None]] = {}
        self.snapshot_data: dict[int, IbSnapshot] = {}
        #: the shortable ticks by conId (roadmap 19.3)
        self.shortable_data: dict[int, IbShortability] = {}
        self.shortable_requests = 0
        self.no_market_data = False
        self.positions_by_account: dict[str, list[IbPosition]] = {}
        self.values_by_account: dict[str, list[IbAccountValue]] = {}
        self.cancel_leaves_pending = False
        # state
        self._trades: dict[int, IbTrade] = {}  # by perm id
        self._executions: list[IbExecution] = []
        self._next_order_id = 1
        self._next_perm = 900_000
        self._next_exec = 1
        #: OCA group per perm id (orders sent with ``oca_group``)
        self._oca: dict[int, str] = {}
        # what the gateway saw
        self.sent: list[tuple[IbContract, IbOrderRequest]] = []
        self.lookups: list[IbContractQuery] = []
        self.cancels: list[int] = []
        #: (client id, order id) of each accepted cancel
        self.cancels_by: list[tuple[int, int]] = []
        self.global_cancels = 0
        self.what_ifs: list[tuple[IbContract, IbOrderRequest]] = []
        self.all_open_requests = 0
        #: option quotes with Greeks by conId, and option events (17.8)
        self.option_snapshot_data: dict[int, IbOptionSnapshot] = {}
        self.option_snapshot_requests: list[list[int]] = []
        self._option_events: list[IbOptionEvent] = []
        #: IBKR algo orders by orderRef: the algo and its params (23.16)
        self.algo_orders: dict[str, tuple[str, dict[str, str]]] = {}

    # ---- client ids -----------------------------------------------------------------

    def session(self, client_id: int) -> FakeIbSession:
        """Another API client of this gateway (not connected yet)."""
        found = FakeIbSession(self, client_id)
        self.sessions.append(found)
        return found

    def _view(self) -> FakeIbGateway | FakeIbSession:
        return self._viewer or self

    def _clients(self) -> list[FakeIbGateway | FakeIbSession]:
        return [self, *self.sessions]

    def _is_master(self, client_id: int) -> bool:
        return self.master_client_id is not None and client_id == self.master_client_id

    # ---- scripting -----------------------------------------------------------------

    def trade(self, order_ref: str) -> IbTrade:
        found = [t for t in self._trades.values() if t.order_ref == order_ref]
        assert found, f"no trade {order_ref}"
        return found[-1]

    def sent_count(self, order_ref: str) -> int:
        return sum(1 for _, r in self.sent if r.order_ref == order_ref)

    def _put(self, trade: IbTrade) -> None:
        self._trades[trade.perm_id] = trade

    def fill(
        self,
        order_ref: str,
        quantity: float,
        price: float,
        *,
        commission: float | None = None,
        currency: str = "USD",
        exec_id: str | None = None,
        at: datetime | None = None,
    ) -> str:
        t = self.trade(order_ref)
        exec_id = exec_id or f"0001.{self._next_exec:04d}"
        self._next_exec += 1
        self._executions.append(
            IbExecution(
                exec_id=exec_id,
                order_ref=order_ref,
                perm_id=t.perm_id,
                contract=t.contract,
                side="BOT" if t.action == "BUY" else "SLD",
                shares=quantity,
                price=price,
                time=at or self.now,
                account=t.account,
                commission=commission,
                commission_currency=currency if commission is not None else None,
            )
        )
        before = t.filled * (t.avg_fill_price or 0.0)
        filled = t.filled + quantity
        avg = (before + quantity * price) / filled
        status = "Filled" if filled >= t.total_quantity - 1e-9 else "Submitted"
        self._put(replace(t, filled=filled, avg_fill_price=avg, status=status))
        self._reduce_oca(t.perm_id, quantity)
        return exec_id

    def work_algo(
        self,
        order_ref: str,
        fills: Sequence[tuple[float, float]],
        *,
        commission: float | None = None,
    ) -> list[str]:
        """IBKR's own child orders of an algo fill: one execution per
        ``(quantity, price)``, all under the parent's orderRef (23.16)."""
        assert order_ref in self.algo_orders, "not an algo order"
        return [self.fill(order_ref, q, p, commission=commission) for q, p in fills]

    def _reduce_oca(self, perm_id: int, quantity: float) -> None:
        """OCA type 2: the other open orders of the group shrink by the
        filled quantity, and one with nothing left is cancelled."""
        group = self._oca.get(perm_id)
        if group is None:
            return
        for perm, other in list(self._trades.items()):
            if perm == perm_id or self._oca.get(perm) != group or other.status in TERMINAL:
                continue
            left = other.total_quantity - quantity
            if left <= other.filled + 1e-9:
                self._put(replace(other, status="Cancelled"))
            else:
                self._put(replace(other, total_quantity=left))

    def trigger_stop(self, order_ref: str, price: float) -> str:
        """The market reached a working stop: it fills in full at ``price``."""
        t = self.trade(order_ref)
        return self.fill(order_ref, t.total_quantity - t.filled, price)

    def report_commission(self, exec_id: str, amount: float, currency: str = "USD") -> None:
        for i, e in enumerate(self._executions):
            if e.exec_id == exec_id:
                self._executions[i] = replace(e, commission=amount, commission_currency=currency)
                return
        raise AssertionError(f"no execution {exec_id}")

    def auction_no_fill(self, order_ref: str, *, origin: CancelOrigin | None = None) -> None:
        """The auction ended without a fill. ``origin``: what IBKR reports
        as the canceller (``None``: it reports nothing)."""
        t = self.trade(order_ref)
        self._put(replace(t, status="Cancelled", cancel_origin=origin))

    def cancel_by_hand(self, order_ref: str) -> None:
        """Someone cancelled the order in TWS or the portal."""
        t = self.trade(order_ref)
        self._put(replace(t, status="Cancelled", cancel_origin="trader"))

    def set_cancel_origin(self, order_ref: str, origin: CancelOrigin | None) -> None:
        self._put(replace(self.trade(order_ref), cancel_origin=origin))

    def set_status(self, order_ref: str, status: str, reason: str | None = None) -> None:
        t = self.trade(order_ref)
        self._put(replace(t, status=status, reason=reason))

    def add_manual_order(self, symbol_details: IbContractDetails, quantity: float) -> IbTrade:
        """An order placed by hand in TWS: no orderRef."""
        t = IbTrade(
            order_id=0,
            perm_id=self._perm(),
            client_id=0,
            order_ref="",
            contract=symbol_details.contract,
            action="BUY",
            total_quantity=quantity,
            status="Submitted",
            account=self.accounts[0],
        )
        self._put(t)
        return t

    def fill_combo(
        self,
        order_ref: str,
        units: float,
        leg_prices: Sequence[float],
        *,
        commission: float | None = None,
    ) -> list[str]:
        """A ``BAG`` order fills ``units``: one execution per leg (the leg's
        contract, its side and ``units x ratio`` contracts at its price)."""
        t = self.trade(order_ref)
        legs = t.contract.combo_legs
        assert len(legs) == len(leg_prices), "one price per leg"
        by_con = {d.contract.con_id: d.contract for d in self.contracts}
        ids: list[str] = []
        for leg, price in zip(legs, leg_prices, strict=True):
            exec_id = f"0002.{self._next_exec:04d}"
            self._next_exec += 1
            self._executions.append(
                IbExecution(
                    exec_id=exec_id,
                    order_ref=order_ref,
                    perm_id=t.perm_id,
                    contract=by_con[leg.con_id],
                    side="BOT" if leg.action == "BUY" else "SLD",
                    shares=units * leg.ratio,
                    price=price,
                    time=self.now,
                    account=t.account,
                    commission=commission,
                    commission_currency="USD" if commission is not None else None,
                )
            )
            ids.append(exec_id)
        filled = t.filled + units
        status = "Filled" if filled >= t.total_quantity - 1e-9 else "Submitted"
        self._put(replace(t, filled=filled, status=status))
        return ids

    def option_event(
        self,
        details: IbContractDetails,
        kind: IbOptionEventKind,
        quantity: float,
        *,
        event_id: str | None = None,
        account: str | None = None,
    ) -> IbOptionEvent:
        """IBKR assigned, exercised or expired ``quantity`` (signed, the
        position removed) of an option."""
        event = IbOptionEvent(
            event_id=event_id or f"eae-{len(self._option_events) + 1}",
            account=account or self.accounts[0],
            contract=details.contract,
            kind=kind,
            quantity=quantity,
            time=self.now,
        )
        self._option_events.append(event)
        return event

    def restart(self) -> None:
        """The gateway restarts: every session drops and open orders come back
        under new order ids."""
        for view in self._clients():
            view.connected = False
            view._next_order_id = 1
        renumbered = 1000
        for perm, t in list(self._trades.items()):
            if t.status not in TERMINAL:
                self._trades[perm] = replace(t, order_id=renumbered)
                renumbered += 1

    def new_day(self) -> None:
        """The session rolled: completed orders and executions are gone."""
        self._trades = {p: t for p, t in self._trades.items() if t.status not in TERMINAL}
        self._executions.clear()

    def drop(self) -> None:
        self.connected = False

    def set_position(self, contract: IbContractDetails, qty: float, account: str | None = None):
        acct = account or self.accounts[0]
        self.positions_by_account.setdefault(acct, []).append(
            IbPosition(account=acct, contract=contract.contract, position=qty, avg_cost=1.0)
        )

    def set_values(self, account: str | None = None, **tags: str | tuple[str, str]) -> None:
        acct = account or self.accounts[0]
        rows = self.values_by_account.setdefault(acct, [])
        for tag, value in tags.items():
            if isinstance(value, tuple):
                rows.append(IbAccountValue(acct, tag, value[0], value[1]))
            else:
                rows.append(IbAccountValue(acct, tag, value, "USD"))

    # ---- a margin account (roadmap 19.13) ----------------------------------------------

    def margin_account(
        self,
        *,
        equity: float,
        excess_liquidity: float | None = None,
        initial: float = 0.0,
        maintenance: float = 0.0,
        available_funds: float | None = None,
        buying_power: float | None = None,
        cash: float | None = None,
        day_trades_remaining: int = -1,
        trading_type: str = "STKMRGN",
        account: str | None = None,
    ) -> None:
        """Account values of a Reg T margin account, replacing any set
        before. Excess liquidity defaults to equity minus maintenance."""
        acct = account or self.accounts[0]
        self.values_by_account[acct] = []
        excess = equity - maintenance if excess_liquidity is None else excess_liquidity
        available = equity - initial if available_funds is None else available_funds
        self.set_values(
            acct,
            NetLiquidation=f"{equity}",
            TotalCashValue=f"{equity if cash is None else cash}",
            SettledCash=f"{equity if cash is None else cash}",
            AvailableFunds=f"{available}",
            BuyingPower=f"{available * 4 if buying_power is None else buying_power}",
            ExcessLiquidity=f"{excess}",
            FullInitMarginReq=f"{initial}",
            FullMaintMarginReq=f"{maintenance}",
            DayTradesRemaining=f"{day_trades_remaining}",
            **{"TradingType-S": (trading_type, "")},
        )

    def margin_call(self, *, excess_liquidity: float, account: str | None = None) -> None:
        """The market moved against the account: excess liquidity falls to
        ``excess_liquidity`` (below 0: IBKR may liquidate), the maintenance
        requirement grows by the same amount."""
        acct = account or self.accounts[0]
        rows = self.values_by_account.get(acct, [])
        old = next(float(v.value) for v in rows if v.tag == "ExcessLiquidity")
        maint = next(float(v.value) for v in rows if v.tag == "FullMaintMarginReq")
        grown = maint + (old - excess_liquidity)
        self.values_by_account[acct] = [
            replace(v, value=f"{excess_liquidity}")
            if v.tag == "ExcessLiquidity"
            else replace(v, value=f"{grown}")
            if v.tag == "FullMaintMarginReq"
            else v
            for v in rows
        ]

    def what_if_margin(
        self,
        contract: IbContractDetails,
        *,
        initial_per_share: float,
        maintenance_per_share: float,
        warning: str | None = None,
    ) -> None:
        """What-if answers for ``contract`` that scale with the quantity."""
        self.what_if_rates[contract.contract.con_id] = (
            initial_per_share,
            maintenance_per_share,
            warning,
        )

    def _perm(self) -> int:
        self._next_perm += 1
        return self._next_perm

    def _need_connection(self) -> None:
        if not self._view().connected:
            raise IbConnectionError("socket closed")

    # ---- IbClient -----------------------------------------------------------------

    def connect(self) -> None:
        view = self._view()
        if view.connected:
            return
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise IbConnectionError("connection refused")
        if any(v is not view and v.connected and v.client_id == view.client_id
               for v in self._clients()):  # fmt: skip
            # IBKR answers 326 and closes the socket
            raise IbConnectionError(
                f"IBKR 326: Unable to connect as the client id {view.client_id} is already in use"
            )
        view.connected = True
        view.connects += 1

    def disconnect(self) -> None:
        self._view().connected = False

    def status(self) -> IbLinkStatus:
        view = self._view()
        return IbLinkStatus(
            connected=view.connected,
            link_ok=self.link_ok,
            competing=self.competing,
            last_error=self.last_error,
            connects=view.connects,
        )

    def managed_accounts(self) -> Sequence[str]:
        self._need_connection()
        return list(self.accounts)

    def server_time(self) -> datetime:
        self._need_connection()
        return self.now

    def contract_details(self, query: IbContractQuery) -> Sequence[IbContractDetails]:
        self._need_connection()
        self.lookups.append(query)
        if query.sec_type == "OPT":
            found = [
                d
                for d in self.contracts
                if d.contract.sec_type == "OPT"
                and d.contract.symbol == query.symbol
                and d.contract.currency == query.currency
                and (query.last_trade_date is None
                     or d.contract.last_trade_date == query.last_trade_date)
                and (query.right is None or d.contract.right == query.right)
                and (query.strike is None or d.contract.strike == query.strike)
                and (query.multiplier is None or d.contract.multiplier == query.multiplier)
            ]  # fmt: skip
        elif query.isin:
            found = [d for d in self.contracts if d.isin == query.isin]
        else:
            found = [
                d
                for d in self.contracts
                if d.contract.symbol == query.symbol and d.contract.currency == query.currency
            ]
        if not found:
            raise IbApiError(200, "No security definition has been found for the request")
        return found

    def place_order(self, contract: IbContract, order: IbOrderRequest) -> IbTrade:
        view = self._view()
        if self.submit_fault == "disconnect_before":
            self.submit_fault = None
            view.connected = False
        self._need_connection()
        self.sent.append((contract, order))
        rejection, self.reject_next = self.reject_next, None
        if rejection is None and order.algo_strategy:
            rejection = _algo_rejection(order)
            if rejection is None:
                self.algo_orders[order.order_ref] = (order.algo_strategy, dict(order.algo_params))
        status = "PreSubmitted" if order.tif == "OPG" else "Submitted"
        trade = IbTrade(
            order_id=view._next_order_id,
            perm_id=self._perm(),
            client_id=view.client_id,
            order_ref=order.order_ref,
            contract=contract,
            action=order.action,
            total_quantity=order.total_quantity,
            status="Inactive" if rejection else status,
            tif=order.tif,
            account=order.account,
            reason=rejection[1] if rejection else None,
        )
        view._next_order_id += 1
        self._put(trade)
        if order.oca_group:
            self._oca[trade.perm_id] = order.oca_group
        if rejection:
            raise IbApiError(rejection[0], rejection[1], req_id=trade.order_id)
        fault, self.submit_fault = self.submit_fault, None
        if fault == "disconnect_after":
            view.connected = False
            raise IbConnectionError("socket closed during submit")
        if fault == "timeout_after":
            raise TimeoutError("no answer")
        return trade

    def cancel_order(self, order_id: int) -> None:
        """A client cancels its own order. The master may cancel any."""
        self._need_connection()
        cid = self._view().client_id
        working = [t for t in self._trades.values()
                   if t.order_id == order_id and t.status not in TERMINAL]  # fmt: skip
        own = [t for t in working if t.client_id == cid]
        target = own[0] if own else (working[0] if working and self._is_master(cid) else None)
        if target is None:
            if working:
                raise IbApiError(
                    10147, f"OrderId {order_id} that needs to be cancelled is not found."
                )
            raise IbApiError(135, f"Can't find order with id = {order_id}")
        self.cancels.append(order_id)
        self.cancels_by.append((cid, order_id))
        status = "PendingCancel" if self.cancel_leaves_pending else "Cancelled"
        self._put(replace(target, status=status, cancel_origin="trader"))

    def global_cancel(self) -> None:
        self._need_connection()
        self.global_cancels += 1
        for t in list(self._trades.values()):
            if t.status not in TERMINAL:
                self._put(replace(t, status="Cancelled"))

    def open_trades(self) -> Sequence[IbTrade]:
        """The orders this client keeps in sync: its own, or every order
        for the master client."""
        self._need_connection()
        cid = self._view().client_id
        master = self._is_master(cid)
        return [t for t in self._trades.values()
                if t.status not in TERMINAL and (master or t.client_id == cid)]  # fmt: skip

    def all_open_trades(self) -> Sequence[IbTrade]:
        """``reqAllOpenOrders``: every open order, whoever placed it."""
        self._need_connection()
        self.all_open_requests += 1
        return [t for t in self._trades.values() if t.status not in TERMINAL]

    def completed_trades(self) -> Sequence[IbTrade]:
        self._need_connection()
        return [t for t in self._trades.values() if t.status in TERMINAL]

    def executions(self) -> Sequence[IbExecution]:
        self._need_connection()
        return list(self._executions)

    def positions(self, account: str) -> Sequence[IbPosition]:
        self._need_connection()
        return list(self.positions_by_account.get(account, []))

    def account_values(self, account: str) -> Sequence[IbAccountValue]:
        self._need_connection()
        return list(self.values_by_account.get(account, []))

    def what_if(self, contract: IbContract, order: IbOrderRequest) -> IbWhatIf:
        self._need_connection()
        self.what_ifs.append((contract, order))
        if self.what_if_timeout:
            raise TimeoutError("what-if timed out")
        if self.what_if_result is not None:
            return self.what_if_result
        rates = self.what_if_rates.get(contract.con_id)
        if rates is not None:
            initial, maintenance, warning = rates
            rows = self.values_by_account.get(order.account or self.accounts[0], [])
            equity = next((float(v.value) for v in rows if v.tag == "NetLiquidation"), 0.0)
            qty = order.total_quantity
            return IbWhatIf(
                init_margin_change=initial * qty,
                maint_margin_change=maintenance * qty,
                equity_with_loan_after=equity,
                commission=1.0,
                commission_currency="USD",
                warning=warning,
            )
        return IbWhatIf(
            init_margin_change=0.0,
            maint_margin_change=0.0,
            equity_with_loan_after=100_000.0,
            commission=1.0,
            commission_currency="USD",
        )

    def snapshots(self, contracts: Sequence[IbContract]) -> Sequence[IbSnapshot]:
        self._need_connection()
        if self.no_market_data:
            raise IbApiError(354, "Requested market data is not subscribed")
        return [self.snapshot_data[c.con_id] for c in contracts if c.con_id in self.snapshot_data]

    def shortability(self, contracts: Sequence[IbContract]) -> Sequence[IbShortability]:
        self._need_connection()
        self.shortable_requests += 1
        return [self.shortable_data[c.con_id] for c in contracts if c.con_id in self.shortable_data]

    # ---- options (roadmap 17.8) -----------------------------------------------------

    def option_params(self, symbol: str, underlying_con_id: int) -> Sequence[IbOptionParams]:
        self._need_connection()
        del underlying_con_id
        options = [d.contract for d in self.contracts
                   if d.contract.sec_type == "OPT" and d.contract.symbol == symbol]  # fmt: skip
        if not options:
            return []
        return [
            IbOptionParams(
                exchange="SMART",
                trading_class=options[0].trading_class or symbol,
                multiplier=options[0].multiplier or "100",
                expirations=tuple(sorted({c.last_trade_date or "" for c in options})),
                strikes=tuple(sorted({float(c.strike or 0.0) for c in options})),
            )
        ]

    def option_snapshots(self, contracts: Sequence[IbContract]) -> Sequence[IbOptionSnapshot]:
        self._need_connection()
        if self.no_market_data:
            raise IbApiError(354, "Requested market data is not subscribed")
        self.option_snapshot_requests.append([c.con_id for c in contracts])
        return [
            self.option_snapshot_data[c.con_id]
            for c in contracts
            if c.con_id in self.option_snapshot_data
        ]

    def option_events(self) -> Sequence[IbOptionEvent]:
        self._need_connection()
        return list(self._option_events)


class FakeIbSession:
    """Another API client of a :class:`FakeIbGateway`: its own client id,
    socket and order ids, the gateway's orders and scripting."""

    def __init__(self, gateway: FakeIbGateway, client_id: int) -> None:
        self.gateway = gateway
        self.client_id = client_id
        self.connected = False
        self.connects = 0
        self.closed = False
        self._next_order_id = 1

    def _via(self, name: str, *args: object) -> Any:
        gw = self.gateway
        before, gw._viewer = gw._viewer, self
        try:
            return getattr(gw, name)(*args)
        finally:
            gw._viewer = before

    def close(self) -> None:
        self.connected = False
        self.closed = True

    def connect(self) -> None:
        self._via("connect")

    def disconnect(self) -> None:
        self.connected = False

    def status(self) -> IbLinkStatus:
        return self._via("status")

    def managed_accounts(self) -> Sequence[str]:
        return self._via("managed_accounts")

    def server_time(self) -> datetime:
        return self._via("server_time")

    def contract_details(self, query: IbContractQuery) -> Sequence[IbContractDetails]:
        return self._via("contract_details", query)

    def place_order(self, contract: IbContract, order: IbOrderRequest) -> IbTrade:
        return self._via("place_order", contract, order)

    def cancel_order(self, order_id: int) -> None:
        self._via("cancel_order", order_id)

    def global_cancel(self) -> None:
        self._via("global_cancel")

    def open_trades(self) -> Sequence[IbTrade]:
        return self._via("open_trades")

    def all_open_trades(self) -> Sequence[IbTrade]:
        return self._via("all_open_trades")

    def completed_trades(self) -> Sequence[IbTrade]:
        return self._via("completed_trades")

    def executions(self) -> Sequence[IbExecution]:
        return self._via("executions")

    def positions(self, account: str) -> Sequence[IbPosition]:
        return self._via("positions", account)

    def account_values(self, account: str) -> Sequence[IbAccountValue]:
        return self._via("account_values", account)

    def what_if(self, contract: IbContract, order: IbOrderRequest) -> IbWhatIf:
        return self._via("what_if", contract, order)

    def snapshots(self, contracts: Sequence[IbContract]) -> Sequence[IbSnapshot]:
        return self._via("snapshots", contracts)

    def shortability(self, contracts: Sequence[IbContract]) -> Sequence[IbShortability]:
        return self._via("shortability", contracts)

    def option_params(self, symbol: str, underlying_con_id: int) -> Sequence[IbOptionParams]:
        return self._via("option_params", symbol, underlying_con_id)

    def option_snapshots(self, contracts: Sequence[IbContract]) -> Sequence[IbOptionSnapshot]:
        return self._via("option_snapshots", contracts)

    def option_events(self) -> Sequence[IbOptionEvent]:
        return self._via("option_events")
