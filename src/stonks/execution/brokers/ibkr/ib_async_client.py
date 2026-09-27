"""``IbAsyncClient``: the ``IbClient`` protocol over ``ib_async`` (roadmap 19.2).

The only module that imports ``ib_async``. It runs the library on its own
event loop thread (:class:`~stonks.execution.brokers.ibkr.session.LoopThread`)
and turns every vendor object into our plain types before it leaves:
contracts, orders, trades, executions with commission reports, positions,
account values, what-if answers and ticker snapshots.

- ``connect`` retries with backoff and jitter (capped at 60 seconds) up to
  the endpoint's deadline. It asks for market data type 3, so IBKR sends
  live data where there is a subscription and delayed data otherwise, and
  says which (``IbSnapshot.market_data_type``).
- The error event keeps the link state: 1100 marks the link lost until a
  1101 or 1102, 10197 marks a competing session until the next connect.
  An order's own error (201, 110, 103, ...) is raised from ``place_order``.
- ``place_order`` waits until IBKR acknowledges the order (a status past
  ``PendingSubmit``) or rejects it. No answer in time raises
  ``TimeoutError``, which the broker turns into an unknown outcome.
- Requests other than submits and cancels go through a token bucket.
"""

from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from ib_async import IB, Contract
from ib_async import Order as IbAsyncOrder

from stonks.execution.brokers.ibkr.client import (
    IbAccountValue,
    IbAction,
    IbApiError,
    IbConnectionError,
    IbContract,
    IbContractDetails,
    IbContractQuery,
    IbEndpoint,
    IbExecution,
    IbLinkStatus,
    IbOrderRequest,
    IbPosition,
    IbShortability,
    IbSnapshot,
    IbTrade,
    IbWhatIf,
)
from stonks.execution.brokers.ibkr.errors import classify, is_info
from stonks.execution.brokers.ibkr.session import LoopThread, TokenBucket, retry_until
from stonks.logging import get_logger

_log = get_logger("stonks.execution.brokers.ibkr.client")


#: IBKR's "not set" for a double.
UNSET = 1.7976931348623157e308
#: Statuses that mean IBKR has not acknowledged an order yet.
_UNACKED = frozenset({"", "PendingSubmit", "ApiPending"})
#: Order errors that answer a submit (the order is refused or in doubt).
_ORDER_ERROR_KINDS = frozenset({"rejected", "bad_tick", "duplicate_order_id"})
_ACK_POLL_SECONDS = 0.05
#: Market data type 3: live where subscribed, else delayed.
_MARKET_DATA_TYPE = 3
#: Generic tick list for the shortable indicator and shares.
_SHORTABLE_TICKS = "236"


class IbAsyncClient:
    def __init__(
        self,
        endpoint: IbEndpoint,
        *,
        ib_factory: Callable[[], Any] = IB,
        loop_thread: LoopThread | None = None,
        pacing: TokenBucket | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._thread = loop_thread or LoopThread(name=f"ibkr-{endpoint.host}-{endpoint.client_id}")
        self._pacing = pacing or TokenBucket(40.0, 40.0)
        self._sleep = sleep
        self._lock = threading.Lock()
        self._link_ok = True
        self._competing = False
        self._last_error: tuple[int, str] | None = None
        self._connects = 0
        self._req_errors: dict[int, tuple[int, str]] = {}
        self.ib: Any = self._thread.call(ib_factory, endpoint.request_timeout)
        self.ib.errorEvent += self._on_error

    # ---- session ---------------------------------------------------------------------

    def _on_error(self, req_id: int, code: int, message: str, *_: Any) -> None:
        with self._lock:
            if code == 1100:
                self._link_ok = False
            elif code in (1101, 1102):
                self._link_ok = True
            elif code == 10197:
                self._competing = True
            if not is_info(code):
                self._last_error = (code, message)
                if req_id >= 0:
                    self._req_errors[req_id] = (code, message)
        log = _log.info if is_info(code) else _log.warning
        log("ibkr.gateway_message", code=code, req_id=req_id, message=message[:200])

    def _connected(self) -> bool:
        return bool(self._thread.call(self.ib.isConnected, self.endpoint.request_timeout))

    def connect(self) -> None:
        if self._connected():
            return
        kwargs: dict[str, Any] = {
            "deadline_seconds": self.endpoint.reconnect_deadline,
            "retry_on": (IbConnectionError,),
            "on_retry": _log_retry,
        }
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        retry_until(self._connect_once, **kwargs)

    def _connect_once(self) -> None:
        ep = self.endpoint
        try:
            self._thread.run(
                lambda: self.ib.connectAsync(
                    ep.host,
                    ep.port,
                    clientId=ep.client_id,
                    timeout=ep.connect_timeout,
                    readonly=ep.readonly,
                ),
                ep.connect_timeout + 2.0,
            )
        except (OSError, TimeoutError) as exc:
            self._thread.call(self.ib.disconnect, ep.request_timeout)
            raise IbConnectionError(f"{type(exc).__name__}: {exc}") from exc
        with self._lock:
            self._link_ok = True
            self._competing = False
            self._connects += 1
        self._thread.call(lambda: self.ib.reqMarketDataType(_MARKET_DATA_TYPE), ep.request_timeout)
        _log.info("ibkr.connected", client_id=ep.client_id)

    def disconnect(self) -> None:
        self._thread.call(self.ib.disconnect, self.endpoint.request_timeout)

    def close(self) -> None:
        """Disconnect and stop the session thread."""
        try:
            self.disconnect()
        finally:
            self._thread.stop()

    def status(self) -> IbLinkStatus:
        connected = self._connected()
        with self._lock:
            return IbLinkStatus(
                connected=connected,
                link_ok=self._link_ok,
                competing=self._competing,
                last_error=self._last_error,
                connects=self._connects,
            )

    # ---- plumbing --------------------------------------------------------------------

    def _need(self) -> None:
        if not self._connected():
            raise IbConnectionError("IB Gateway is not connected")

    def _call[T](self, fn: Callable[[], T], *, paced: bool = True) -> T:
        self._need()
        if paced:
            self._pacing.acquire()
        return self._thread.call(fn, self.endpoint.request_timeout)

    def _run(self, factory: Callable[[], Any], *, paced: bool = True) -> Any:
        self._need()
        if paced:
            self._pacing.acquire()
        return self._thread.run(factory, self.endpoint.request_timeout)

    # ---- reads -----------------------------------------------------------------------

    def managed_accounts(self) -> Sequence[str]:
        return [str(a) for a in self._call(self.ib.managedAccounts)]

    def server_time(self) -> datetime:
        return _utc(self._run(self.ib.reqCurrentTimeAsync))

    def contract_details(self, query: IbContractQuery) -> Sequence[IbContractDetails]:
        found = self._run(lambda: self.ib.reqContractDetailsAsync(query_contract(query)))
        return [from_details(d) for d in _items(found)]

    def open_trades(self) -> Sequence[IbTrade]:
        return [from_trade(t) for t in self._call(self.ib.openTrades)]

    def completed_trades(self) -> Sequence[IbTrade]:
        found = self._run(lambda: self.ib.reqCompletedOrdersAsync(False))
        return [from_trade(t) for t in _items(found)]

    def executions(self) -> Sequence[IbExecution]:
        self._run(self.ib.reqExecutionsAsync)
        # the cached fills carry the commission reports that came later
        return [from_fill(f) for f in self._call(self.ib.fills, paced=False)]

    def positions(self, account: str) -> Sequence[IbPosition]:
        return [from_position(p) for p in self._call(lambda: self.ib.positions(account))]

    def account_values(self, account: str) -> Sequence[IbAccountValue]:
        values = self._call(lambda: self.ib.accountValues(account))
        if not values:
            values = self._run(lambda: self.ib.accountSummaryAsync(account))
        return [from_account_value(v) for v in _items(values)]

    def what_if(self, contract: IbContract, order: IbOrderRequest) -> IbWhatIf:
        state = self._run(lambda: self.ib.whatIfOrderAsync(to_contract(contract), to_order(order)))
        return from_order_state(state)

    def snapshots(self, contracts: Sequence[IbContract]) -> Sequence[IbSnapshot]:
        wanted = [to_contract(c) for c in contracts]
        tickers = self._run(lambda: self.ib.reqTickersAsync(*wanted))
        return [from_ticker(t) for t in _items(tickers)]

    def shortability(self, contracts: Sequence[IbContract]) -> Sequence[IbShortability]:
        """The shortable ticks (generic tick 236) of each contract. IBKR
        sends them only on a streaming request, so this subscribes, waits
        until every answer arrived (or most of the request timeout passed)
        and cancels again. A contract with no answer has ``indicator=None``."""
        wanted = [to_contract(c) for c in contracts]
        wait = max(0.1, self.endpoint.request_timeout * 0.8)

        async def collect() -> list[IbShortability]:
            tickers = [self.ib.reqMktData(c, _SHORTABLE_TICKS, False, False) for c in wanted]
            try:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + wait
                while loop.time() < deadline and any(_missing(t.shortable) for t in tickers):
                    await asyncio.sleep(_ACK_POLL_SECONDS)
            finally:
                for c in wanted:
                    self.ib.cancelMktData(c)
            return [from_shortable(t) for t in tickers]

        return self._run(collect)

    # ---- orders ----------------------------------------------------------------------

    def place_order(self, contract: IbContract, order: IbOrderRequest) -> IbTrade:
        ib_contract, ib_order = to_contract(contract), to_order(order)
        trade = self._call(lambda: self.ib.placeOrder(ib_contract, ib_order), paced=False)
        order_id = int(trade.order.orderId)

        async def acknowledged() -> None:
            while True:
                if trade.orderStatus.status not in _UNACKED:
                    return
                with self._lock:
                    if order_id in self._req_errors:
                        return
                await asyncio.sleep(_ACK_POLL_SECONDS)

        self._thread.run(acknowledged, self.endpoint.request_timeout)
        with self._lock:
            error = self._req_errors.pop(order_id, None)
        if error is not None and classify(error[0]) in _ORDER_ERROR_KINDS:
            raise IbApiError(error[0], error[1], req_id=order_id)
        return self._thread.call(lambda: from_trade(trade), self.endpoint.request_timeout)

    def cancel_order(self, order_id: int) -> None:
        def cancel() -> None:
            for t in self.ib.openTrades():
                if int(t.order.orderId) == order_id:
                    self.ib.cancelOrder(t.order)
                    return
            raise IbApiError(135, f"no open order with id {order_id}", req_id=order_id)

        self._call(cancel, paced=False)

    def global_cancel(self) -> None:
        self._call(self.ib.reqGlobalCancel, paced=False)


def _log_retry(attempt: int, wait: float, exc: BaseException) -> None:
    _log.warning("ibkr.reconnect", attempt=attempt, wait_seconds=round(wait, 2), error=str(exc))


def _items(value: Any) -> list[Any]:
    """A vendor list (or ``None``) as a plain list."""
    return list(cast("list[Any]", value)) if value else []


# ---- vendor objects onto our types ------------------------------------------------------


def _opt(value: Any) -> float | None:
    """A number, or ``None`` for IBKR's unset values, NaN and blanks."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or abs(number) >= UNSET:
        return None
    return number


def _utc(value: Any) -> datetime:
    if not isinstance(value, datetime):
        return datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _text(value: Any) -> str | None:
    return str(value) if value else None


def from_contract(c: Any) -> IbContract:
    return IbContract(
        con_id=int(c.conId),
        symbol=str(c.symbol),
        sec_type=str(c.secType or "STK"),
        currency=str(c.currency),
        exchange=str(c.exchange or "SMART"),
        primary_exchange=_text(c.primaryExchange),
        trading_class=_text(c.tradingClass),
        local_symbol=_text(c.localSymbol),
    )


def to_contract(c: IbContract) -> Contract:
    return Contract(
        conId=c.con_id,
        symbol=c.symbol,
        secType=c.sec_type,
        exchange=c.exchange or "SMART",
        primaryExchange=c.primary_exchange or "",
        currency=c.currency,
    )


def query_contract(q: IbContractQuery) -> Contract:
    if q.isin:
        return Contract(
            secType=q.sec_type,
            secIdType="ISIN",
            secId=q.isin,
            exchange=q.exchange,
            currency=q.currency,
        )
    return Contract(
        secType=q.sec_type,
        symbol=q.symbol,
        exchange=q.exchange,
        primaryExchange=q.primary_exchange or "",
        currency=q.currency,
    )


def from_details(d: Any) -> IbContractDetails:
    isin = next((str(t.value) for t in _items(d.secIdList) if t.tag == "ISIN"), None)
    magnifier = int(d.priceMagnifier or 1)
    return IbContractDetails(
        contract=from_contract(d.contract),
        min_tick=float(d.minTick),
        price_magnifier=max(1, magnifier),
        long_name=_text(d.longName),
        isin=isin,
    )


def to_order(req: IbOrderRequest) -> IbAsyncOrder:
    order = IbAsyncOrder(
        action=req.action,
        totalQuantity=req.total_quantity,
        orderType=req.order_type,
        tif=req.tif,
        orderRef=req.order_ref,
        account=req.account,
        outsideRth=req.outside_rth,
        transmit=req.transmit,
    )
    if req.limit_price is not None:
        order.lmtPrice = req.limit_price
    if req.aux_price is not None:
        order.auxPrice = req.aux_price
    return order


def from_trade(t: Any) -> IbTrade:
    order, status = t.order, t.orderStatus
    filled = _opt(status.filled) or 0.0
    if filled <= 0:
        filled = _opt(order.filledQuantity) or 0.0
    reason = next(
        (str(e.message) for e in reversed(list(t.log or [])) if getattr(e, "errorCode", 0)),
        None,
    )
    action: IbAction = "BUY" if str(order.action).upper() == "BUY" else "SELL"
    avg = _opt(status.avgFillPrice)
    return IbTrade(
        order_id=int(order.orderId),
        perm_id=int(status.permId or order.permId or 0),
        order_ref=str(order.orderRef or ""),
        contract=from_contract(t.contract),
        action=action,
        total_quantity=float(order.totalQuantity),
        status=str(status.status),
        filled=filled,
        avg_fill_price=avg if avg else None,
        tif=_text(order.tif),
        account=_text(order.account),
        reason=reason,
    )


def from_fill(f: Any) -> IbExecution:
    e: Any = f.execution
    report: Any = f.commissionReport
    commission: float | None = None
    currency: str | None = None
    if report is not None and report.execId:
        commission, currency = _opt(report.commission), _text(report.currency)
    return IbExecution(
        exec_id=str(e.execId),
        order_ref=str(e.orderRef or ""),
        perm_id=int(e.permId),
        contract=from_contract(f.contract),
        side=str(e.side),
        shares=float(e.shares),
        price=float(e.price),
        time=_utc(e.time),
        account=_text(e.acctNumber),
        commission=commission,
        commission_currency=currency,
    )


def from_position(p: Any) -> IbPosition:
    return IbPosition(
        account=str(p.account),
        contract=from_contract(p.contract),
        position=float(p.position),
        avg_cost=float(p.avgCost or 0.0),
    )


def from_account_value(v: Any) -> IbAccountValue:
    return IbAccountValue(
        account=str(v.account), tag=str(v.tag), value=str(v.value), currency=str(v.currency or "")
    )


def from_order_state(s: Any) -> IbWhatIf:
    return IbWhatIf(
        init_margin_change=_opt(s.initMarginChange),
        maint_margin_change=_opt(s.maintMarginChange),
        equity_with_loan_after=_opt(s.equityWithLoanAfter),
        commission=_opt(s.commission),
        commission_currency=_text(s.commissionCurrency),
        warning=_text(s.warningText),
    )


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def from_shortable(t: Any) -> IbShortability:
    indicator = None if _missing(t.shortable) else float(t.shortable)
    shares = None if _missing(t.shortableShares) else float(t.shortableShares)
    return IbShortability(con_id=int(t.contract.conId), indicator=indicator, shares=shares)


def from_ticker(t: Any) -> IbSnapshot:
    return IbSnapshot(
        con_id=int(t.contract.conId),
        last=_opt(t.last),
        bid=_opt(t.bid),
        ask=_opt(t.ask),
        time=_utc(t.time),
        market_data_type=int(cast(int, t.marketDataType or 1)),
    )
