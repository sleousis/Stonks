"""The ``Broker`` of an eToro account, over eToro's official public API.

What it does, and refuses, follows ``docs/design/etoro.md``:

- **Market orders only.** eToro's public API has no resting limit or stop
  order, so anything else is refused before it is sent.
- **Opens are long, unleveraged, in the real asset.** A buy opens a
  position with ``leverage`` 1 and ``settlementType`` ``real``, sized in
  units. An instrument eToro trades only as a CFD is refused, as is a
  fractional quantity where only whole units are allowed and a size above
  eToro's maximum per order. An instrument sized by amount only is bought
  for ``quantity * ask`` USD (USD instruments only).
- **A sell closes positions.** eToro closes positions, not quantities: a
  sell closes the oldest plain positions of the instrument (long, leverage
  1, real, outside copies) until the quantity is reached, the last one in
  part. Selling more than those hold is refused, so it never shorts.
- **Idempotent by client id.** An open's ``x-request-id`` is a fixed UUID
  made from the client id. eToro dedupes on it and finds the order by it
  (``referenceId``), so a fresh process can reconcile it. A close carries
  no reference Stonks can search, so its eToro order ids are kept on the
  Stonks order (``broker_order_id`` ``close:<id>,<id>``) through
  reconciliation, and read back from the ledger later.

Capabilities: ``OrderStateSource``, ``OrderCanceller``, ``AccountReader``
and ``OpenOrderSource``. ``real_money`` is true for a real account, so the
stage guard holds opening orders below the Real money stages.
"""

from __future__ import annotations

import math
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from stonks.connections.base import (
    ProviderAuthError,
    ProviderError,
    ProviderUnavailable,
)
from stonks.core.types import Fill, Order, OrderSide, Portfolio
from stonks.execution.brokers.base import (
    QTY_EPSILON,
    BrokerError,
    BrokerOpenOrder,
    BrokerOrderState,
    BrokerUnavailableError,
    LiveAccountState,
    OrderOutcomeUnknownError,
    OrderRejectedError,
    OrderState,
    UnsupportedTickerError,
)
from stonks.execution.brokers.etoro.account import EtoroAccount, EtoroPosition, Env
from stonks.execution.brokers.etoro.client import OutcomeUnknown
from stonks.execution.brokers.etoro.instruments import EtoroInstrument, InstrumentCatalog
from stonks.logging import get_logger

_log = get_logger("stonks.execution.brokers.etoro")

#: Client ids become request ids in this namespace, so the same order always
#: carries the same ``x-request-id``.
REQUEST_NAMESPACE = uuid.UUID("7f1c1f0e-6f3a-4d1f-9a55-2d9c9e4b6a01")
CLOSE_PREFIX = "close:"

#: eToro order status ids (``orders:lookup``) -> the order state machine.
#: PartiallyFilled and the partly filled cancels and rejects executed what
#: they could and will not fill more: ``cancelled``, with the fill booked.
STATUS_STATES: dict[int, OrderState] = {
    1: "submitted",
    2: "accepted",
    3: "filled",
    4: "rejected",
    5: "cancelled",
    6: "pending_cancel",
    7: "cancelled",
    8: "expired",
    9: "cancelled",
    10: "cancelled",
    11: "accepted",
    12: "accepted",
}
_WORKING = frozenset({"submitted", "accepted", "pending_cancel", "partially_filled"})
_COARSE: dict[OrderState, str] = {
    "submitted": "pending",
    "accepted": "pending",
    "pending_cancel": "pending",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "rejected": "rejected",
    "cancelled": "cancelled",
    "expired": "cancelled",
}


def request_id(client_id: str, leg: str = "") -> str:
    """The fixed ``x-request-id`` of an order (and of one close leg)."""
    return str(uuid.uuid5(REQUEST_NAMESPACE, f"{client_id}:{leg}" if leg else client_id))


@dataclass
class _Sent:
    """What this process sent for one client id."""

    kind: Literal["open", "close"]
    ticker: str
    side: OrderSide
    quantity: float
    order_ids: list[int] = field(default_factory=list[int])
    #: Units of close legs eToro refused.
    refused_units: float = 0.0


@dataclass(frozen=True)
class _Eligibility:
    instrument_id: int
    allow_open: bool
    allow_partial_close: bool
    real_long: bool
    whole_units: bool
    amount_only: bool
    min_exposure: float | None
    max_units: float | None


class EtoroBroker:
    """Trades one eToro account (demo or real) for one portfolio."""

    def __init__(
        self,
        account: EtoroAccount,
        catalog: InstrumentCatalog,
        *,
        account_id: str,
        state: Any = None,
        clock: Any = None,
    ) -> None:
        self._account = account
        self._client = account.client
        self._paths = account.paths
        self._catalog = catalog
        self.account_id = account_id
        self._state = state
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sent: dict[str, _Sent] = {}
        self._eligibility: dict[int, _Eligibility] = {}
        self._lock = threading.Lock()

    @property
    def env(self) -> Env:
        return self._account.env

    @property
    def real_money(self) -> bool:
        """A real account trades real money (the stage guard reads it)."""
        return self.env == "real"

    def __repr__(self) -> str:
        return f"EtoroBroker(env={self.env!r})"

    # ---- Broker protocol -----------------------------------------------------------------

    def fetch_portfolio(self) -> Portfolio:
        snap = self._read("portfolio", self._account.snapshot)
        found = self._instruments([p.instrument_id for p in snap.positions])
        positions: dict[str, float] = {}
        for p in snap.positions:
            ticker = found[p.instrument_id].ticker if p.instrument_id in found else None
            if ticker is None:
                continue
            positions[ticker] = positions.get(ticker, 0.0) + p.signed_units
        return Portfolio(
            cash=snap.available_cash,
            positions={t: q for t, q in positions.items() if abs(q) > QTY_EPSILON},
        )

    def place_order(self, order: Order) -> Fill | None:
        self._check_static(order)
        with self._lock:
            if order.client_id in self._sent:
                return None  # a repeat is a no-op, as the Broker contract asks
        inst = self._resolve(order.ticker)
        if order.side == "buy":
            self._open(order, inst)
        else:
            self._close(order, inst)
        return None

    def reconcile(self) -> list[Fill]:
        """Fills come through ``get_order_state`` (the order-state path)."""
        return []

    # ---- OrderStateSource ----------------------------------------------------------------

    def get_order_state(self, client_id: str) -> BrokerOrderState | None:
        with self._lock:
            sent = self._sent.get(client_id)
        if sent is None:
            sent = self._from_ledger(client_id)
        if sent is not None and sent.kind == "close":
            return self._close_state(client_id, sent)
        if sent is not None and sent.order_ids:
            info = self._lookup({"orderId": sent.order_ids[0]})
        else:
            info = self._lookup({"referenceId": request_id(client_id)})
        if info is None:
            return None
        return self._open_state(client_id, info, sent)

    # ---- OrderCanceller ------------------------------------------------------------------

    def cancel_order(self, client_id: str) -> bool:
        with self._lock:
            sent = self._sent.get(client_id)
        if sent is None:
            sent = self._from_ledger(client_id)
        if sent is None:
            info = self._lookup({"referenceId": request_id(client_id)})
            if info is None:
                return False
            sent = _Sent("open", "", "buy", 0.0, [int(info.get("orderId") or 0)])
        requested = False
        for order_id in sent.order_ids:
            path = (
                self._paths.cancel_close(order_id)
                if sent.kind == "close"
                else self._paths.cancel_open(order_id)
            )
            try:
                self._client.delete("cancel", path, request_id=str(uuid.uuid4()))
            except ProviderError as exc:
                if exc.status in (400, 404, 409):  # already done, or unknown there
                    continue
                raise _broker_error("cancel", exc) from None
            requested = True
        _log.info("etoro.cancel", client_id=client_id, requested=requested)
        return requested

    # ---- AccountReader -------------------------------------------------------------------

    def fetch_account(self) -> LiveAccountState:
        snap = self._read("portfolio", self._account.snapshot)
        cash = snap.available_cash
        return LiveAccountState(
            equity=snap.equity,
            cash=cash,
            settled_cash=cash,
            available_funds=cash,
            buying_power=cash,
            currency="USD",
            account_type="cash",
            account_id=self.account_id,
        )

    # ---- OpenOrderSource -----------------------------------------------------------------

    def open_orders(self) -> Sequence[BrokerOpenOrder]:
        snap = self._read("portfolio", self._account.snapshot)
        ids = [o.instrument_id for o in snap.pending_open] + [
            o.instrument_id for o in snap.pending_close
        ]
        found = self._instruments(ids)
        owners = self._owners()
        out: list[BrokerOpenOrder] = []
        for o in snap.pending_open:
            inst = found.get(o.instrument_id)
            out.append(
                BrokerOpenOrder(
                    broker_order_id=str(o.order_id),
                    client_id=owners.get(("open", o.order_id)),
                    ticker=(inst.ticker if inst else None) or f"etoro:{o.instrument_id}",
                    side="buy" if o.is_buy else "sell",
                    quantity=o.units or 0.0,
                    state="accepted",
                )
            )
        for c in snap.pending_close:
            inst = found.get(c.instrument_id)
            out.append(
                BrokerOpenOrder(
                    broker_order_id=f"{CLOSE_PREFIX}{c.order_id}",
                    client_id=owners.get(("close", c.order_id)),
                    ticker=(inst.ticker if inst else None) or f"etoro:{c.instrument_id}",
                    side="sell",
                    quantity=c.units or 0.0,
                    state="accepted",
                )
            )
        return out

    def close(self) -> None:
        self._client.close()

    # ---- opens ---------------------------------------------------------------------------

    def _open(self, order: Order, inst: EtoroInstrument) -> None:
        if order.position_effect == "close":
            raise OrderRejectedError(
                "eToro shorts are CFDs, which Stonks does not open: there is no short to cover"
            )
        rules = self._rules(inst)
        if not rules.allow_open:
            raise OrderRejectedError(f"eToro does not allow opening {order.ticker} now")
        if not rules.real_long:
            raise OrderRejectedError(
                f"eToro offers {order.ticker} only as a CFD or with leverage, which Stonks"
                " does not trade"
            )
        units = order.quantity
        if rules.whole_units and not _whole(units):
            raise OrderRejectedError(f"eToro trades {order.ticker} in whole units only")
        if rules.max_units is not None and units > rules.max_units + QTY_EPSILON:
            raise OrderRejectedError(
                f"{units:g} units is above eToro's {rules.max_units:g} per order"
            )
        body: dict[str, Any] = {
            "action": "open",
            "transaction": "buy",
            "instrumentId": inst.id,
            "orderType": "mkt",
            "leverage": 1,
            "settlementType": "real",
        }
        if rules.amount_only:
            body["amount"] = self._amount_for(order, inst, rules)
            body["orderCurrency"] = "usd"
        else:
            body["units"] = units
        rid = request_id(order.client_id)
        # a repeat after a lost answer: eToro may have it already
        known = self._lookup({"referenceId": rid})
        if known is not None:
            self._remember(order, "open", [int(known.get("orderId") or 0)])
            return
        try:
            answer = self._client.post("open order", self._paths.open, body, request_id=rid)
        except OutcomeUnknown as exc:
            raise OrderOutcomeUnknownError(order.client_id, str(exc)) from None
        except ProviderError as exc:
            raise _order_error("open order", exc) from None
        order_id = int(answer.get("orderId") or 0) if isinstance(answer, dict) else 0
        self._remember(order, "open", [order_id] if order_id else [])
        _log.info("etoro.order.open", client_id=order.client_id, env=self.env,
                  instrument_id=inst.id, order_id=order_id)  # fmt: skip

    def _amount_for(self, order: Order, inst: EtoroInstrument, rules: _Eligibility) -> float:
        """Units to USD for an instrument eToro sizes by amount only, at
        eToro's ask. Only USD instruments: eToro takes amounts in USD."""
        if not (order.ticker.endswith(".US") or order.ticker.endswith("-USD.CC")):
            raise OrderRejectedError(
                f"eToro sizes {order.ticker} by amount only, which Stonks converts for USD"
                " instruments only"
            )
        ask = self._ask(inst.id)
        amount = round(order.quantity * ask, 2)
        if rules.min_exposure is not None and amount < rules.min_exposure:
            raise OrderRejectedError(
                f"{amount:.2f} USD is below eToro's minimum of {rules.min_exposure:g} USD"
            )
        return amount

    def _ask(self, instrument_id: int) -> float:
        data = self._read(
            "rates", lambda: self._client.get("rates", self._paths.rates,
                                              {"instrumentIds": str(instrument_id)})
        )  # fmt: skip
        for row in data.get("results") or [] if isinstance(data, dict) else []:
            if int(row.get("instrumentId") or -1) == instrument_id:
                ask = float(row.get("ask") or 0.0)
                if ask > 0 and math.isfinite(ask):
                    return ask
        raise OrderRejectedError("eToro sent no price to size the order by amount")

    # ---- closes --------------------------------------------------------------------------

    def _close(self, order: Order, inst: EtoroInstrument) -> None:
        if order.position_effect == "open":
            raise OrderRejectedError(
                "Stonks does not open short positions at eToro (they are CFDs)"
            )
        snap = self._read("portfolio", self._account.snapshot)
        held = sorted(
            (p for p in snap.positions if p.instrument_id == inst.id and p.plain),
            key=lambda p: (p.opened_at or datetime.min.replace(tzinfo=UTC), p.position_id),
        )
        total = sum(p.units for p in held)
        if order.quantity > total + QTY_EPSILON:
            raise OrderRejectedError(
                f"cannot sell {order.quantity:g} {order.ticker}: the account holds {total:g}"
                " in plain positions, and Stonks never sells short at eToro"
            )
        plan = _close_plan(held, order.quantity)
        if any(units is not None for _, units in plan) and not self._rules(inst).allow_partial_close:
            raise OrderRejectedError(f"eToro does not allow closing part of a {order.ticker} position")
        sent = self._remember(order, "close", [])
        for i, (position, units) in enumerate(plan):
            body = {"InstrumentId": inst.id, "UnitsToDeduct": units}
            try:
                answer = self._client.post(
                    "close position",
                    self._paths.close_position(position.position_id),
                    body,
                    request_id=request_id(order.client_id, str(position.position_id)),
                )
            except OutcomeUnknown as exc:
                raise OrderOutcomeUnknownError(order.client_id, str(exc)) from None
            except ProviderError as exc:
                if i == 0:  # nothing went out: a clean rejection
                    with self._lock:
                        self._sent.pop(order.client_id, None)
                    raise _order_error("close position", exc) from None
                _log.warning("etoro.close_leg_refused", client_id=order.client_id,
                             error=str(exc))  # fmt: skip
                sent.refused_units += units if units is not None else position.units
                continue
            close = answer.get("orderForClose") if isinstance(answer, dict) else None
            order_id = int(close.get("orderID") or 0) if isinstance(close, dict) else 0
            if order_id:
                sent.order_ids.append(order_id)
        _log.info("etoro.order.close", client_id=order.client_id, env=self.env,
                  instrument_id=inst.id, legs=len(sent.order_ids))  # fmt: skip

    # ---- order states --------------------------------------------------------------------

    def _open_state(
        self, client_id: str, info: dict[str, Any], sent: _Sent | None
    ) -> BrokerOrderState:
        status = info.get("status") if isinstance(info.get("status"), dict) else {}
        state = STATUS_STATES.get(int(status.get("id") or 0), "submitted")
        units = 0.0
        notional = 0.0
        for execution in info.get("positionExecutions") or []:
            opening = execution.get("openingData") if isinstance(execution, dict) else None
            if not isinstance(opening, dict):
                continue
            u = float(opening.get("units") or 0.0)
            units += u
            notional += u * float(opening.get("avgPrice") or 0.0)
        asset = info.get("asset") if isinstance(info.get("asset"), dict) else {}
        ticker = sent.ticker if sent else self._ticker_of(asset.get("instrumentId"))
        requested = sent.quantity if sent else float(info.get("requestedUnits") or units)
        if state == "cancelled" and units <= QTY_EPSILON and int(status.get("id") or 0) == 10:
            state = "rejected"
        if state == "filled" and units < requested - QTY_EPSILON and units > QTY_EPSILON:
            requested = units  # an amount order: the units are what the amount bought
        return BrokerOrderState(
            client_id=client_id,
            broker_order_id=str(info.get("orderId") or ""),
            ticker=ticker,
            side="buy",
            status=_COARSE[state],  # type: ignore[arg-type]
            quantity=requested,
            filled_quantity=units,
            avg_fill_price=notional / units if units > QTY_EPSILON else None,
            updated_at=_time(info.get("lastUpdate")) or self._clock(),
            state=state,
        )

    def _close_state(self, client_id: str, sent: _Sent) -> BrokerOrderState:
        units = 0.0
        notional = 0.0
        working = False
        rejected = 0
        for order_id in sent.order_ids:
            try:
                info = self._client.get("close order", f"{self._paths.close_info}/{order_id}")
            except ProviderError as exc:
                if exc.status == 404:
                    working = True  # not listed yet
                    continue
                raise _broker_error("close order", exc) from None
            done = [p for p in info.get("positions") or [] if isinstance(p, dict)]
            filled = sum(float(p.get("units") or 0.0) for p in done)
            units += filled
            notional += sum(float(p.get("units") or 0.0) * float(p.get("rate") or 0.0)
                            for p in done)  # fmt: skip
            if int(info.get("errorCode") or 0) != 0:
                rejected += 1
            elif filled <= QTY_EPSILON and int(info.get("statusID") or 0) not in (7, 8):
                working = True
        state: OrderState
        if working:
            state = "partially_filled" if units > QTY_EPSILON else "accepted"
        elif units >= sent.quantity - QTY_EPSILON:
            state = "filled"
        elif units > QTY_EPSILON:
            state = "cancelled"  # the rest was refused or cancelled
        elif rejected or sent.refused_units > QTY_EPSILON or not sent.order_ids:
            state = "rejected"
        else:
            state = "cancelled"
        return BrokerOrderState(
            client_id=client_id,
            broker_order_id=CLOSE_PREFIX + ",".join(str(i) for i in sent.order_ids),
            ticker=sent.ticker,
            side="sell",
            status=_COARSE[state],  # type: ignore[arg-type]
            quantity=sent.quantity,
            filled_quantity=units,
            avg_fill_price=notional / units if units > QTY_EPSILON else None,
            updated_at=self._clock(),
            state=state,
        )

    def _lookup(self, params: dict[str, Any]) -> dict[str, Any] | None:
        try:
            info = self._client.get("order lookup", self._paths.lookup, params)
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise _broker_error("order lookup", exc) from None
        return info if isinstance(info, dict) else None

    # ---- plumbing ------------------------------------------------------------------------

    def _check_static(self, order: Order) -> None:
        if not order.client_id:
            raise OrderRejectedError("an eToro order needs a client id")
        if order.order_type != "market":
            raise OrderRejectedError(
                f"eToro takes market orders only through Stonks (not {order.order_type})"
            )
        if order.outside_rth:
            raise OrderRejectedError("orders outside regular hours are refused")
        if order.quantity <= 0 or not math.isfinite(order.quantity):
            raise OrderRejectedError("the quantity must be positive")

    def _resolve(self, ticker: str) -> EtoroInstrument:
        try:
            return self._read("instrument", lambda: self._catalog.resolve(self._client, ticker))
        except UnsupportedTickerError as exc:
            raise OrderRejectedError(str(exc)) from None

    def _instruments(self, ids: list[int]) -> dict[int, EtoroInstrument]:
        if not ids:
            return {}
        return self._read("instruments", lambda: self._catalog.by_ids(self._client, ids))

    def _ticker_of(self, instrument_id: Any) -> str:
        try:
            iid = int(instrument_id)
        except (TypeError, ValueError):
            return "etoro:unknown"
        inst = self._instruments([iid]).get(iid)
        return (inst.ticker if inst else None) or f"etoro:{iid}"

    def _rules(self, inst: EtoroInstrument) -> _Eligibility:
        with self._lock:
            known = self._eligibility.get(inst.id)
        if known is not None:
            return known
        data = self._read(
            "eligibility",
            lambda: self._client.post_read(
                "eligibility", self._paths.eligibility,
                {"instrumentIds": [inst.id], "currency": "USD"},
            ),
        )  # fmt: skip
        rows = data.get("eligibilities") if isinstance(data, dict) else None
        row = next(
            (r for r in rows or [] if isinstance(r, dict) and int(r.get("instrumentId") or -1) == inst.id),
            None,
        )  # fmt: skip
        if row is None:
            raise OrderRejectedError(f"eToro sent no trading rules for {inst.symbol}")
        configs = [c for c in row.get("leverageConfigs") or [] if isinstance(c, dict)]
        rules = _Eligibility(
            instrument_id=inst.id,
            allow_open=bool(row.get("allowOpenPosition", False)),
            allow_partial_close=bool(row.get("allowPartialClosePosition", True)),
            real_long=any(
                c.get("settlementType") == "real"
                and c.get("direction") == "long"
                and 1 in (c.get("leverageValues") or [])
                for c in configs
            ),
            whole_units=row.get("unitsQuantityType") == "whole",
            amount_only=row.get("allowedOrderQuantityType") == "amountOnly",
            min_exposure=_float(row.get("minPositionExposure")),
            max_units=_float(row.get("maxUnitsPerOrder")),
        )
        with self._lock:
            self._eligibility[inst.id] = rules
        return rules

    def _remember(self, order: Order, kind: Literal["open", "close"], ids: list[int]) -> _Sent:
        sent = _Sent(kind, order.ticker, order.side, order.quantity, list(ids))
        with self._lock:
            self._sent[order.client_id] = sent
        return sent

    def _from_ledger(self, client_id: str) -> _Sent | None:
        """What an earlier process sent, from the order's ledger row."""
        if self._state is None:
            return None
        try:
            rows = self._state.sql(
                "SELECT ticker, side, quantity, broker_order_id FROM orders WHERE client_id = ?",
                [client_id],
            )
        except Exception as exc:  # the ledger is a hint: never fail a lookup on it
            _log.warning("etoro.ledger_read_failed", error=type(exc).__name__)
            return None
        if not rows:
            return None
        row = rows[0]
        ref = str(row["broker_order_id"] or "")
        if ref.startswith(CLOSE_PREFIX):
            ids = [int(x) for x in ref[len(CLOSE_PREFIX) :].split(",") if x.strip().isdigit()]
            return _Sent("close", row["ticker"], "sell", float(row["quantity"]), ids)
        if row["side"] == "sell":
            return None  # a close whose ids were never recorded
        ids = [int(ref)] if ref.isdigit() else []
        return _Sent("open", row["ticker"], "buy", float(row["quantity"]), ids)

    def _owners(self) -> dict[tuple[str, int], str]:
        """eToro order ids -> our client ids, for working orders."""
        out: dict[tuple[str, int], str] = {}
        with self._lock:
            for cid, sent in self._sent.items():
                for oid in sent.order_ids:
                    out[(sent.kind, oid)] = cid
        if self._state is None:
            return out
        try:
            rows = self._state.sql(
                "SELECT client_id, side, broker_order_id FROM orders"
                " WHERE broker_order_id IS NOT NULL AND status IN ('pending', 'partially_filled')"
            )
        except Exception as exc:
            _log.warning("etoro.ledger_read_failed", error=type(exc).__name__)
            return out
        for row in rows:
            ref = str(row["broker_order_id"])
            if ref.startswith(CLOSE_PREFIX):
                for part in ref[len(CLOSE_PREFIX) :].split(","):
                    if part.strip().isdigit():
                        out.setdefault(("close", int(part)), row["client_id"])
            elif ref.isdigit() and row["side"] == "buy":
                out.setdefault(("open", int(ref)), row["client_id"])
        return out

    def _read(self, op: str, fn: Any) -> Any:
        try:
            return fn()
        except ProviderError as exc:
            raise _broker_error(op, exc) from None


def _close_plan(held: list[EtoroPosition], quantity: float) -> list[tuple[EtoroPosition, float | None]]:
    """The positions a sell closes, oldest first: ``None`` units close a
    position in full."""
    plan: list[tuple[EtoroPosition, float | None]] = []
    left = quantity
    for position in held:
        if left <= QTY_EPSILON:
            break
        if position.units <= left + QTY_EPSILON:
            plan.append((position, None))
            left -= position.units
        else:
            plan.append((position, round(left, 8)))
            left = 0.0
    return plan


def _broker_error(op: str, exc: ProviderError) -> BrokerError:
    if isinstance(exc, ProviderUnavailable):  # includes rate limits and outcome unknown
        return BrokerUnavailableError(f"eToro {op}: {exc}")
    if isinstance(exc, ProviderAuthError):
        return BrokerError(f"eToro {op}: the keys were refused or lack permission ({exc})")
    return BrokerError(f"eToro {op}: {exc}")


def _order_error(op: str, exc: ProviderError) -> BrokerError:
    """eToro answering an order with a 4xx refused it: nothing went out."""
    if isinstance(exc, ProviderUnavailable | ProviderAuthError):
        return _broker_error(op, exc)
    if exc.status is not None and 400 <= exc.status < 500:
        return OrderRejectedError(f"eToro refused the order: {exc}")
    return _broker_error(op, exc)


def _whole(units: float) -> bool:
    return abs(units - round(units)) <= QTY_EPSILON


def _float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _time(value: Any) -> datetime | None:
    from stonks.execution.brokers.etoro.account import parse_time

    return parse_time(value)


__all__ = ["CLOSE_PREFIX", "STATUS_STATES", "EtoroBroker", "request_id"]
