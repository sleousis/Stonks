"""``IbkrBroker``: Interactive Brokers behind the ``Broker`` seam (roadmap 19.2).

It speaks only to an :class:`~stonks.execution.brokers.ibkr.client.IbClient`
(``IbAsyncClient`` in production, ``FakeIbGateway`` in tests) and returns
only our types. Capabilities: ``Broker``, ``OrderStateSource``,
``OrderCanceller``, ``GlobalCanceller``, ``AccountReader``,
``MarginPreviewer``, ``ExecutionSource``, ``QuoteSource`` and
``OpenOrderSource``.

Safety, in the order every call meets it (:meth:`IbkrBroker.ensure_ready`):

1. connect (the client retries with backoff up to its deadline);
2. refuse while the gateway reports a competing session (10197) or a lost
   link to IBKR (1100);
3. after every (re)connect, the **account check**: the gateway's managed
   accounts must hold the expected account (or exactly one account when
   none is set), a paper gateway a ``DU`` account and a live gateway a
   ``U`` account. Anything else raises ``LiveTradingRefusedError``;
4. calls that change orders also need ``allow_live`` on a live gateway.

Idempotency: IBKR does not dedupe on ``orderRef``, so :meth:`place_order`
first looks the client id up (open orders, completed orders, executions)
and books what it finds instead of sending again. A submit whose outcome
is unknown (the link dropped, no answer) raises
``OrderOutcomeUnknownError``: the caller marks the order ``unknown`` and
nothing is sent for it until reconciliation finds it. ``place_order``
never returns a fill: fills come from executions (``executions``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.types import Fill, Order, OrderSide, Portfolio, TimeInForce
from stonks.execution.brokers.base import (
    AccountType,
    BrokerError,
    BrokerOpenOrder,
    BrokerOrderState,
    BrokerUnavailableError,
    Execution,
    LiveAccountState,
    LiveTradingRefusedError,
    MarginPreview,
    OrderOutcomeUnknownError,
    OrderRejectedError,
    OrderState,
    Quote,
    UnsupportedTickerError,
)
from stonks.execution.brokers.ibkr.client import (
    IbAccountValue,
    IbApiError,
    IbClient,
    IbContract,
    IbExecution,
    IbTrade,
)
from stonks.execution.brokers.ibkr.contracts import ContractResolver
from stonks.execution.brokers.ibkr.errors import classify, to_broker_error
from stonks.execution.brokers.ibkr.orders import broker_ref, to_ib_order
from stonks.execution.brokers.ibkr.settings import GatewayMode, IbkrOrderSettings
from stonks.execution.brokers.ibkr.status import ibkr_state
from stonks.execution.order_state import TERMINAL, ledger_status
from stonks.logging import get_logger

_log = get_logger("stonks.execution.brokers.ibkr")


_TIF_BACK: dict[str, TimeInForce] = {"DAY": "day", "GTC": "gtc", "OPG": "opg", "IOC": "ioc"}
#: A what-if or snapshot value IBKR sends for "not set".
_UNSET_LIMIT = 1e300

LoginFault = str  # ``login_refused``, ``wrong_account`` or ``competing_session``


@dataclass(frozen=True)
class LoginCheck:
    """What :meth:`IbkrBroker.login_check` found (the health probe's answer)."""

    connected: bool
    detail: str
    account_id: str | None = None
    server_time: datetime | None = None
    #: A real fault that pauses auto at once, else ``None``.
    fault: LoginFault | None = None

    @property
    def ok(self) -> bool:
        return self.connected and self.fault is None


class IbkrBroker:
    def __init__(
        self,
        client: IbClient,
        *,
        mode: GatewayMode,
        account_id: str | None = None,
        allow_live: bool = False,
        resolver: ContractResolver | None = None,
        order_settings: IbkrOrderSettings | None = None,
        account_type: AccountType = "cash",
        allow_short: bool = False,
        ref_lookup: Callable[[str], str | None] | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.client = client
        self.mode: GatewayMode = mode
        self.expected_account = account_id
        self.allow_live = allow_live
        self.resolver = resolver or ContractResolver(client, clock=clock)
        self.order_settings = order_settings or IbkrOrderSettings()
        self.account_type: AccountType = account_type
        self.allow_short = allow_short
        self.clock = clock
        #: orderRef -> client id, for references this process sent. A hashed
        #: reference from an earlier process is looked up through
        #: ``ref_lookup`` (``orders.broker_ref``).
        self._refs: dict[str, str] = {}
        self._ref_lookup = ref_lookup
        self._account: str | None = None
        self._checked_connects = -1
        self._seen_execs: set[str] = set()
        self._closers: list[Callable[[], object]] = []

    def on_close(self, fn: Callable[[], object]) -> None:
        """Run ``fn`` when the broker closes (a state DB it owns, say)."""
        self._closers.append(fn)

    def close(self) -> None:
        """Close the gateway session and anything the broker owns."""
        close = getattr(self.client, "close", None)
        try:
            if callable(close):
                close()
        finally:
            for fn in reversed(self._closers):
                fn()
            self._closers.clear()

    # ---- readiness and the account safety check ---------------------------------------

    @property
    def account_id(self) -> str:
        """The checked account (connects and checks on first use)."""
        self.ensure_ready()
        assert self._account is not None
        return self._account

    def ensure_ready(self) -> None:
        self._guard("connect", self.client.connect)
        status = self.client.status()
        if status.competing:
            raise BrokerUnavailableError(
                "IBKR reports a competing session (10197): someone logged in with the gateway's "
                "username elsewhere. Use a separate username for the API."
            )
        if not status.link_ok:
            raise BrokerUnavailableError(
                "IB Gateway lost its link to IBKR (1100); waiting for it to come back"
            )
        if status.connects != self._checked_connects or self._account is None:
            self._account = self._check_account()
            self._checked_connects = status.connects

    def _check_account(self) -> str:
        accounts = [a for a in self._guard("managed accounts", self.client.managed_accounts) if a]
        if not accounts:
            raise LiveTradingRefusedError(
                "IB Gateway reports no managed account: the login did not finish"
            )
        if self.expected_account is not None:
            if self.expected_account not in accounts:
                raise LiveTradingRefusedError(
                    "IB Gateway is logged in to another account than the one configured"
                )
            account = self.expected_account
        elif len(accounts) == 1:
            account = accounts[0]
        else:
            raise LiveTradingRefusedError(
                f"IB Gateway manages {len(accounts)} accounts: set the account_id to trade"
            )
        paper = account.upper().startswith("DU")
        if self.mode == "paper" and not paper:
            raise LiveTradingRefusedError(
                "a paper gateway is logged in to a live account: refusing to trade"
            )
        if self.mode == "live" and paper:
            raise LiveTradingRefusedError(
                "a live gateway is logged in to a paper (DU) account: refusing to trade"
            )
        _log.info("ibkr.account_checked", mode=self.mode)
        return account

    def _ensure_may_trade(self) -> str:
        account = self.account_id
        if self.mode == "live" and not self.allow_live:
            raise LiveTradingRefusedError(
                "real-money orders need [brokers.ibkr] allow_live = true and a live stage"
            )
        return account

    def login_check(self) -> LoginCheck:
        """Connect, check the account and read the server time. The health
        probe's view; it never raises."""
        try:
            self.ensure_ready()
            server_time = self._guard("server time", self.client.server_time)
        except LiveTradingRefusedError as exc:
            fault = "login_refused" if "no managed account" in str(exc) else "wrong_account"
            return LoginCheck(connected=False, detail=str(exc), fault=fault)
        except BrokerUnavailableError as exc:
            # down, or up without a usable link to IBKR (1100, 10197)
            fault = "competing_session" if self.client.status().competing else None
            return LoginCheck(connected=False, detail=str(exc), fault=fault)
        except BrokerError as exc:
            return LoginCheck(connected=False, detail=str(exc))
        return LoginCheck(
            connected=True,
            detail="logged in",
            account_id=self._account,
            server_time=server_time,
        )

    # ---- Broker ---------------------------------------------------------------------

    def fetch_portfolio(self) -> Portfolio:
        account = self.account_id
        positions: dict[str, float] = {}
        for p in self._guard("positions", lambda: self.client.positions(account)):
            if abs(p.position) < 1e-12 or (p.account and p.account != account):
                continue
            ticker = self._ticker_of(p.contract)
            positions[ticker] = positions.get(ticker, 0.0) + p.position
        cash = self.fetch_account().cash
        return Portfolio(cash=cash, positions=positions)

    def broker_ref(self, client_id: str) -> str:
        """The ``orderRef`` sent for ``client_id`` (stored in ``orders.broker_ref``)."""
        return broker_ref(client_id, self.order_settings.order_ref_max_length)

    def place_order(self, order: Order) -> None:
        account = self._ensure_may_trade()
        ref = self.broker_ref(order.client_id)
        self._refs[ref] = order.client_id
        existing = self.get_order_state(order.client_id)
        if existing is not None:
            _log.info("ibkr.order.already_at_broker", client_id=order.client_id)
            return
        if order.side == "sell" and order.position_effect == "open" and not self.allow_short:
            raise OrderRejectedError(
                f"short sale of {order.ticker} refused: this IBKR account trades long only"
            )
        resolved = self.resolver.resolve(order.ticker)
        request = to_ib_order(order, resolved.spec(), account=account, settings=self.order_settings)
        try:
            self.client.place_order(resolved.contract, request)
        except IbApiError as exc:
            if classify(exc.code) == "duplicate_order_id" and (
                self.get_order_state(order.client_id) is not None
            ):
                _log.warning("ibkr.order.duplicate_id_resolved", client_id=order.client_id)
                return
            raise to_broker_error(exc, action=f"submit of {order.client_id}") from exc
        except (ConnectionError, TimeoutError) as exc:
            _log.warning("ibkr.order.outcome_unknown", client_id=order.client_id, error=str(exc))
            raise OrderOutcomeUnknownError(
                order.client_id,
                f"submit of {order.client_id} did not finish ({type(exc).__name__}); "
                "reconcile before sending anything for it",
            ) from exc
        _log.info("ibkr.order.submitted", client_id=order.client_id, order_type=request.order_type)
        return

    def reconcile(self) -> list[Fill]:
        """Executions not returned before, as fills (one per execution id)."""
        fills: list[Fill] = []
        for e in self._session_executions():
            if e.broker_exec_id in self._seen_execs:
                continue
            self._seen_execs.add(e.broker_exec_id)
            fills.append(e.to_fill())
        return fills

    # ---- OrderStateSource / OrderCanceller / GlobalCanceller -----------------------------

    def get_order_state(self, client_id: str) -> BrokerOrderState | None:
        self.ensure_ready()
        ref = self.broker_ref(client_id)
        trade = self._find_trade(ref)
        execs = [e for e in self._guard("executions", self.client.executions) if e.order_ref == ref]
        exec_qty = sum(e.shares for e in execs)
        exec_avg = sum(e.shares * e.price for e in execs) / exec_qty if exec_qty > 0 else None
        if trade is None:
            if not execs:
                return None
            # executions but no order record (the gateway forgot it): the
            # order reached IBKR, its final state is for reconciliation
            first = execs[0]
            return BrokerOrderState(
                client_id=client_id,
                broker_order_id=str(first.perm_id),
                ticker=self._ticker_of(first.contract),
                side=_side(first.side),
                status=ledger_status("unknown"),
                quantity=exec_qty,
                filled_quantity=exec_qty,
                avg_fill_price=exec_avg,
                state="unknown",
            )
        filled = max(trade.filled, exec_qty)
        avg = trade.avg_fill_price if trade.filled >= exec_qty else exec_avg
        state: OrderState = ibkr_state(
            trade.status, filled=filled, time_in_force=_TIF_BACK.get(trade.tif or "")
        )
        return BrokerOrderState(
            client_id=client_id,
            broker_order_id=str(trade.perm_id),
            ticker=self._ticker_of(trade.contract),
            side="buy" if trade.action == "BUY" else "sell",
            status=ledger_status(state),
            quantity=trade.total_quantity,
            filled_quantity=filled,
            avg_fill_price=avg if filled > 0 else None,
            state=state,
        )

    def cancel_order(self, client_id: str) -> bool:
        self._ensure_may_trade()
        ref = self.broker_ref(client_id)
        open_trades = self._guard("open orders", self.client.open_trades)
        trade = next((t for t in open_trades if t.order_ref == ref), None)
        if trade is None:
            return False
        if ibkr_state(trade.status, filled=trade.filled) in TERMINAL:
            return False
        try:
            self.client.cancel_order(trade.order_id)
        except IbApiError as exc:
            if classify(exc.code) == "not_found":
                return False
            raise to_broker_error(exc, action=f"cancel of {client_id}") from exc
        except (ConnectionError, TimeoutError) as exc:
            raise to_broker_error(exc, action=f"cancel of {client_id}") from exc
        _log.info("ibkr.order.cancel_requested", client_id=client_id)
        return True

    def cancel_all(self) -> int:
        """Cancel every working order in the account, hand-placed ones too
        (the kill switch in stop-all mode). Returns how many were open."""
        self.ensure_ready()
        count = len(self._guard("open orders", self.client.open_trades))
        self._guard("global cancel", self.client.global_cancel)
        _log.warning("ibkr.orders.global_cancel", open_orders=count)
        return count

    # ---- OpenOrderSource (roadmap 19.5) ------------------------------------------------

    def open_orders(self) -> Sequence[BrokerOpenOrder]:
        """Every working order in the account. One with no ``orderRef`` was
        placed by hand and has no client id."""
        account = self.account_id
        out: list[BrokerOpenOrder] = []
        for t in self._guard("open orders", self.client.open_trades):
            if t.account and t.account != account:
                continue
            state = ibkr_state(t.status, filled=t.filled, time_in_force=_TIF_BACK.get(t.tif or ""))
            if state in TERMINAL:
                continue
            out.append(
                BrokerOpenOrder(
                    broker_order_id=str(t.perm_id),
                    client_id=self._client_id_for(t.order_ref) if t.order_ref else None,
                    ticker=self._ticker_of(t.contract),
                    side="buy" if t.action == "BUY" else "sell",
                    quantity=t.total_quantity,
                    filled_quantity=t.filled,
                    state=state,
                )
            )
        return out

    # ---- AccountReader --------------------------------------------------------------

    def fetch_account(self) -> LiveAccountState:
        account = self.account_id
        values = self._guard("account values", lambda: self.client.account_values(account))
        return account_state(values, account_id=account, account_type=self.account_type)

    # ---- MarginPreviewer ------------------------------------------------------------

    def what_if(self, order: Order) -> MarginPreview:
        """IBKR's preview of ``order``. A failure or timeout raises: the
        caller must never let a buy through on it."""
        account = self.account_id
        resolved = self.resolver.resolve(order.ticker)
        request = to_ib_order(order, resolved.spec(), account=account, settings=self.order_settings)
        answer = self._guard(
            f"what-if of {order.client_id}",
            lambda: self.client.what_if(resolved.contract, request),
        )
        return MarginPreview(
            client_id=order.client_id,
            initial_margin_change=_num(answer.init_margin_change),
            maintenance_margin_change=_num(answer.maint_margin_change),
            equity_with_loan_after=_num(answer.equity_with_loan_after),
            commission=_opt(answer.commission),
            commission_currency=answer.commission_currency or None,
            warning=answer.warning or None,
        )

    # ---- ExecutionSource ------------------------------------------------------------

    def executions(self, since: datetime) -> Sequence[Execution]:
        return [e for e in self._session_executions() if e.executed_at >= since]

    # ---- QuoteSource ----------------------------------------------------------------

    def quotes(self, tickers: Sequence[str]) -> Mapping[str, Quote]:
        self.ensure_ready()
        contracts: dict[int, str] = {}
        wanted: list[IbContract] = []
        for ticker in tickers:
            try:
                resolved = self.resolver.resolve(ticker)
            except UnsupportedTickerError as exc:
                _log.warning("ibkr.quote.unsupported", ticker=ticker, error=str(exc))
                continue
            contracts[resolved.con_id] = ticker
            wanted.append(resolved.contract)
        if not wanted:
            return {}
        try:
            snaps = self.client.snapshots(wanted)
        except IbApiError as exc:
            if classify(exc.code) == "no_market_data":
                _log.warning("ibkr.quote.no_subscription", code=exc.code)
                return {}
            raise to_broker_error(exc, action="quotes") from exc
        except (ConnectionError, TimeoutError) as exc:
            raise to_broker_error(exc, action="quotes") from exc
        out: dict[str, Quote] = {}
        for s in snaps:
            ticker = contracts.get(s.con_id)
            last, bid, ask = _opt_price(s.last), _opt_price(s.bid), _opt_price(s.ask)
            if ticker is None or (last is None and bid is None and ask is None):
                continue
            out[ticker] = Quote(
                ticker=ticker,
                last=last,
                bid=bid,
                ask=ask,
                as_of=s.time,
                delayed=s.market_data_type in (3, 4),
            )
        return out

    # ---- internals ------------------------------------------------------------------

    def _guard[T](self, action: str, fn: Callable[[], T]) -> T:
        try:
            return fn()
        except (IbApiError, ConnectionError, TimeoutError) as exc:
            raise to_broker_error(exc, action=action) from exc

    def _find_trade(self, ref: str) -> IbTrade | None:
        for t in self._guard("open orders", self.client.open_trades):
            if t.order_ref == ref:
                return t
        completed = [
            t for t in self._guard("completed orders", self.client.completed_trades)
            if t.order_ref == ref
        ]  # fmt: skip
        return completed[-1] if completed else None

    def _ticker_of(self, contract: IbContract) -> str:
        """Our ticker for a contract, else its raw IBKR symbol (not covered)."""
        ticker = self.resolver.ticker_for(contract.con_id)
        if ticker is None:
            _log.info("ibkr.contract.unmapped", con_id=contract.con_id)
            return contract.symbol
        return ticker

    def _client_id_for(self, ref: str) -> str:
        known = self._refs.get(ref)
        if known is not None:
            return known
        if self._ref_lookup is not None:
            looked = self._ref_lookup(ref)
            if looked is not None:
                self._refs[ref] = looked
                return looked
        return ref

    def _session_executions(self) -> list[Execution]:
        account = self.account_id
        out: list[Execution] = []
        for e in self._guard("executions", self.client.executions):
            if not e.order_ref:
                continue  # placed by hand: not ours (ownership by attribution)
            if e.account and e.account != account:
                continue
            out.append(self._to_execution(e))
        return out

    def _to_execution(self, e: IbExecution) -> Execution:
        return Execution(
            broker_exec_id=e.exec_id,
            client_id=self._client_id_for(e.order_ref),
            ticker=self._ticker_of(e.contract),
            side=_side(e.side),
            quantity=e.shares,
            price=e.price,
            executed_at=e.time,
            commission=e.commission,
            commission_currency=e.commission_currency,
        )


def _side(ib_side: str) -> OrderSide:
    return "buy" if ib_side.upper() in ("BOT", "BUY") else "sell"


def _opt(value: float | None) -> float | None:
    if value is None or value != value or abs(value) >= _UNSET_LIMIT:
        return None
    return float(value)


def _num(value: float | None) -> float:
    found = _opt(value)
    return 0.0 if found is None else found


def _opt_price(value: float | None) -> float | None:
    found = _opt(value)
    return found if found is not None and found > 0 else None


#: Account value tags -> ``LiveAccountState`` fields.
_TAGS = {
    "NetLiquidation": "equity",
    "TotalCashValue": "cash",
    "SettledCash": "settled_cash",
    "AvailableFunds": "available_funds",
    "BuyingPower": "buying_power",
    "ExcessLiquidity": "excess_liquidity",
    "DayTradesRemaining": "day_trades_remaining",
    "FullInitMarginReq": "initial_margin",
    "FullMaintMarginReq": "maintenance_margin",
}


def account_state(
    values: Sequence[IbAccountValue], *, account_id: str, account_type: AccountType
) -> LiveAccountState:
    """``LiveAccountState`` from IBKR account values. The base currency is
    the currency of ``NetLiquidation``. Cash per currency comes from the
    ``CashBalance`` rows (the ``$LEDGER`` values)."""
    found: dict[str, float] = {}
    base: str | None = None
    by_currency: dict[str, float] = {}
    for v in values:
        if v.account and v.account != account_id:
            continue
        number = _parse(v.value)
        if v.tag == "CashBalance" and v.currency and v.currency != "BASE":
            if number is not None:
                by_currency[v.currency] = number
            continue
        field = _TAGS.get(v.tag)
        if field is None or number is None:
            continue
        found[field] = number
        if v.tag == "NetLiquidation" and v.currency and v.currency != "BASE":
            base = v.currency
    if "equity" not in found:
        raise BrokerError("IBKR sent no NetLiquidation for the account")
    day_trades = found.get("day_trades_remaining")
    return LiveAccountState(
        equity=found["equity"],
        cash=found.get("cash", 0.0),
        settled_cash=found.get("settled_cash", found.get("cash", 0.0)),
        available_funds=found.get("available_funds", 0.0),
        buying_power=found.get("buying_power", 0.0),
        currency=base or "USD",
        account_type=account_type,
        excess_liquidity=found.get("excess_liquidity"),
        # IBKR sends -1 for "no limit"
        day_trades_remaining=int(day_trades)
        if day_trades is not None and day_trades >= 0
        else None,
        initial_margin=found.get("initial_margin", 0.0),
        maintenance_margin=found.get("maintenance_margin", 0.0),
        cash_by_currency=by_currency,
        account_id=account_id,
    )


def _parse(value: str) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None
