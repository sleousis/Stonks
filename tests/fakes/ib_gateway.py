"""``FakeIbGateway``: an in-memory IB Gateway behind the ``IbClient`` protocol
(roadmap 19.2, ``docs/design/live-trading.md`` section 8).

Scriptable: fills and partial fills, auction no-fills, rejections with IBKR
codes, late commission reports, a disconnect or timeout in the middle of a
submit, a gateway restart that renumbers order ids, a competing session,
a lost link to IBKR, delayed quotes, missing market data and a what-if
timeout. No network.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal

from stonks.execution.brokers.ibkr.client import (
    IbAccountValue,
    IbApiError,
    IbConnectionError,
    IbContract,
    IbContractDetails,
    IbContractQuery,
    IbExecution,
    IbLinkStatus,
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


AAPL = stock(265598, "AAPL", isin="US0378331005")
MSFT = stock(272093, "MSFT", isin="US5949181045")
BRKB = stock(72063691, "BRK B", primary="NYSE")
VOD = stock(9999, "VOD", currency="GBP", primary="LSE", magnifier=100)


class FakeIbGateway:
    def __init__(
        self,
        accounts: Sequence[str] = ("DU1234567",),
        *,
        contracts: Sequence[IbContractDetails] = (AAPL, MSFT, BRKB, VOD),
        now: datetime = T0,
    ) -> None:
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
        # what the gateway saw
        self.sent: list[tuple[IbContract, IbOrderRequest]] = []
        self.lookups: list[IbContractQuery] = []
        self.cancels: list[int] = []
        self.global_cancels = 0

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
        return exec_id

    def report_commission(self, exec_id: str, amount: float, currency: str = "USD") -> None:
        for i, e in enumerate(self._executions):
            if e.exec_id == exec_id:
                self._executions[i] = replace(e, commission=amount, commission_currency=currency)
                return
        raise AssertionError(f"no execution {exec_id}")

    def auction_no_fill(self, order_ref: str) -> None:
        t = self.trade(order_ref)
        self._put(replace(t, status="Cancelled"))

    def set_status(self, order_ref: str, status: str, reason: str | None = None) -> None:
        t = self.trade(order_ref)
        self._put(replace(t, status=status, reason=reason))

    def add_manual_order(self, symbol_details: IbContractDetails, quantity: float) -> IbTrade:
        """An order placed by hand in TWS: no orderRef."""
        t = IbTrade(
            order_id=0,
            perm_id=self._perm(),
            order_ref="",
            contract=symbol_details.contract,
            action="BUY",
            total_quantity=quantity,
            status="Submitted",
            account=self.accounts[0],
        )
        self._put(t)
        return t

    def restart(self) -> None:
        """The gateway restarts: the session drops and open orders come back
        under new order ids."""
        self.connected = False
        renumbered = 1000
        for perm, t in list(self._trades.items()):
            if t.status not in TERMINAL:
                self._trades[perm] = replace(t, order_id=renumbered)
                renumbered += 1
        self._next_order_id = 1

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

    def _perm(self) -> int:
        self._next_perm += 1
        return self._next_perm

    def _need_connection(self) -> None:
        if not self.connected:
            raise IbConnectionError("socket closed")

    # ---- IbClient -----------------------------------------------------------------

    def connect(self) -> None:
        if self.connected:
            return
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise IbConnectionError("connection refused")
        self.connected = True
        self.connects += 1

    def disconnect(self) -> None:
        self.connected = False

    def status(self) -> IbLinkStatus:
        return IbLinkStatus(
            connected=self.connected,
            link_ok=self.link_ok,
            competing=self.competing,
            last_error=self.last_error,
            connects=self.connects,
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
        if query.isin:
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
        if self.submit_fault == "disconnect_before":
            self.submit_fault = None
            self.connected = False
        self._need_connection()
        self.sent.append((contract, order))
        rejection, self.reject_next = self.reject_next, None
        status = "PreSubmitted" if order.tif == "OPG" else "Submitted"
        trade = IbTrade(
            order_id=self._next_order_id,
            perm_id=self._perm(),
            order_ref=order.order_ref,
            contract=contract,
            action=order.action,
            total_quantity=order.total_quantity,
            status="Inactive" if rejection else status,
            tif=order.tif,
            account=order.account,
            reason=rejection[1] if rejection else None,
        )
        self._next_order_id += 1
        self._put(trade)
        if rejection:
            raise IbApiError(rejection[0], rejection[1], req_id=trade.order_id)
        fault, self.submit_fault = self.submit_fault, None
        if fault == "disconnect_after":
            self.connected = False
            raise IbConnectionError("socket closed during submit")
        if fault == "timeout_after":
            raise TimeoutError("no answer")
        return trade

    def cancel_order(self, order_id: int) -> None:
        self._need_connection()
        for t in self._trades.values():
            if t.order_id == order_id and t.status not in TERMINAL:
                self.cancels.append(order_id)
                status = "PendingCancel" if self.cancel_leaves_pending else "Cancelled"
                self._put(replace(t, status=status))
                return
        raise IbApiError(135, f"Can't find order with id = {order_id}")

    def global_cancel(self) -> None:
        self._need_connection()
        self.global_cancels += 1
        for t in list(self._trades.values()):
            if t.status not in TERMINAL:
                self._put(replace(t, status="Cancelled"))

    def open_trades(self) -> Sequence[IbTrade]:
        self._need_connection()
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
        if self.what_if_timeout:
            raise TimeoutError("what-if timed out")
        if self.what_if_result is not None:
            return self.what_if_result
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
