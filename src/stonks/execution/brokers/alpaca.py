"""Alpaca broker adapter (paper by default) wrapping ``alpaca-py``.

Every alpaca-py type stays inside this module: the client is created with
``raw_data=True`` so responses are plain JSON dicts, and everything leaving
the adapter is one of our types (``Portfolio``, ``Fill``,
``BrokerOrderState``) or one of our exceptions (``BrokerError`` & co).

Idempotency: ``Order.client_id`` is sent as Alpaca's ``client_order_id``.
Alpaca refuses a second order with the same id, so a resubmission (a crashed
tick re-run, or a retry after a lost response) resolves to the existing
order instead of creating a second one.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaSide
from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from stonks.core.types import Fill, Order, OrderStatus, Portfolio
from stonks.execution.brokers.base import (
    BrokerError,
    BrokerOrderState,
    LiveTradingRefusedError,
    delta_fill,
)
from stonks.execution.brokers.symbols import from_alpaca_symbol, to_alpaca_symbol
from stonks.logging import get_logger

_log = get_logger("stonks.execution.brokers.alpaca")

# Alpaca caps client_order_id at 128 characters. We refuse rather than
# truncate: truncation could make two distinct orders collide.
MAX_CLIENT_ORDER_ID_LEN = 128
# Alpaca accepts up to 9 decimal places for fractional quantities.
_QTY_DECIMALS = 9
_DUPLICATE_CODE = 40010001

_STATUS_MAP: dict[str, OrderStatus] = {
    "filled": "filled",
    "partially_filled": "partially_filled",
    "canceled": "cancelled",
    "expired": "cancelled",
    "replaced": "cancelled",
    "rejected": "rejected",
}
_TERMINAL: frozenset[OrderStatus] = frozenset({"filled", "cancelled", "rejected"})


class _DuplicateClientOrderId(Exception):
    pass


class _NotFound(Exception):
    pass


class AlpacaBroker:
    """``Broker`` Protocol implementation over Alpaca's trading API.

    Construct via :meth:`connect` (credentials, paper/live guard) in normal
    use; the constructor takes an already-built client so tests can inject a
    fake.
    """

    def __init__(
        self,
        client: Any,
        *,
        paper: bool = True,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self.paper = paper
        self._max_retries = max(0, max_retries)
        self._backoff = retry_backoff_seconds
        self._sleep = sleep
        # client_id -> (booked qty, booked notional) for reconcile() deltas.
        self._booked: dict[str, tuple[float, float]] = {}
        self._open: set[str] = set()

    @classmethod
    def connect(
        cls,
        api_key: str | None,
        secret_key: str | None,
        *,
        paper: bool = True,
        allow_live: bool = False,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
    ) -> AlpacaBroker:
        if not paper and not allow_live:
            raise LiveTradingRefusedError(
                "refusing to connect to Alpaca's LIVE endpoint: pass allow_live=True "
                "(brokers.alpaca.allow_live = true) to trade real money"
            )
        if not api_key or not secret_key:
            raise BrokerError(
                "Alpaca credentials missing: set ALPACA_API_KEY and ALPACA_SECRET_KEY"
            )
        client = TradingClient(api_key, secret_key, paper=paper, raw_data=True)
        _log.info("alpaca.connected", paper=paper)
        return cls(
            client,
            paper=paper,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )

    # ---- Broker protocol ----------------------------------------------------

    def fetch_portfolio(self) -> Portfolio:
        account = self._call("get_account", self._client.get_account)
        positions_raw = self._call("get_all_positions", self._client.get_all_positions)
        positions: dict[str, float] = {}
        for pos in positions_raw:
            ticker = from_alpaca_symbol(pos["symbol"])
            qty = float(pos["qty"])
            if pos.get("side") == "short" and qty > 0:
                qty = -qty
            positions[ticker] = qty
        return Portfolio(cash=float(account["cash"]), positions=positions)

    def place_order(self, order: Order) -> Fill | None:
        request = self._build_request(order)
        try:
            raw = self._call("submit_order", self._client.submit_order, request)
        except _DuplicateClientOrderId:
            _log.info("alpaca.order.duplicate_resolved", client_id=order.client_id)
            raw = self._fetch_order(order.client_id)
            if raw is None:
                raise BrokerError(
                    f"Alpaca reported client_order_id {order.client_id!r} as duplicate "
                    "but the order could not be fetched"
                ) from None
        state = self._to_state(raw)
        self._warn_on_mismatch(order, state)
        self._booked.setdefault(state.client_id, (0.0, 0.0))
        if state.status == "filled":
            fill = self._book_delta(state, raw)
            self._open.discard(state.client_id)
            return fill
        if state.status not in _TERMINAL:
            self._open.add(state.client_id)
        return None

    def reconcile(self) -> list[Fill]:
        """Fills (as deltas) on orders placed through this instance since
        the last call. Per-order failures are logged and skipped."""
        fills: list[Fill] = []
        for client_id in sorted(self._open):
            try:
                raw = self._fetch_order(client_id)
            except BrokerError as exc:
                _log.warning("alpaca.reconcile.order_failed", client_id=client_id, error=str(exc))
                continue
            if raw is None:
                continue
            state = self._to_state(raw)
            fill = self._book_delta(state, raw)
            if fill is not None:
                fills.append(fill)
            if state.status in _TERMINAL:
                self._open.discard(client_id)
        return fills

    # ---- optional capability: OrderStateSource ------------------------------

    def get_order_state(self, client_id: str) -> BrokerOrderState | None:
        raw = self._fetch_order(client_id)
        return None if raw is None else self._to_state(raw)

    # ---- internals ----------------------------------------------------------

    def _build_request(self, order: Order) -> MarketOrderRequest | LimitOrderRequest:
        symbol = to_alpaca_symbol(order.ticker)
        if len(order.client_id) > MAX_CLIENT_ORDER_ID_LEN:
            raise BrokerError(
                f"client_id longer than {MAX_CLIENT_ORDER_ID_LEN} chars is not accepted "
                f"by Alpaca: {order.client_id!r}"
            )
        common = {
            "symbol": symbol,
            "qty": round(order.quantity, _QTY_DECIMALS),
            "side": AlpacaSide.BUY if order.side == "buy" else AlpacaSide.SELL,
            # DAY is the only time-in-force Alpaca accepts for fractional qty.
            "time_in_force": TimeInForce.DAY,
            "client_order_id": order.client_id,
        }
        if order.order_type == "market":
            return MarketOrderRequest(**common)
        if order.order_type == "limit":
            return LimitOrderRequest(limit_price=order.limit_price, **common)
        raise BrokerError(
            f"order_type {order.order_type!r} is not supported by the Alpaca broker "
            "(market and limit only; stop orders need a stop price)"
        )

    def _fetch_order(self, client_id: str) -> dict | None:
        try:
            return self._call("get_order", self._client.get_order_by_client_id, client_id)
        except _NotFound:
            return None

    def _book_delta(self, state: BrokerOrderState, raw: dict) -> Fill | None:
        booked_qty, booked_notional = self._booked.get(state.client_id, (0.0, 0.0))
        fill = delta_fill(
            state,
            recorded_quantity=booked_qty,
            recorded_notional=booked_notional,
            filled_at=fill_time(raw),
        )
        if fill is not None:
            self._booked[state.client_id] = (
                booked_qty + fill.quantity,
                booked_notional + fill.quantity * fill.price,
            )
        return fill

    @staticmethod
    def _to_state(raw: dict) -> BrokerOrderState:
        filled_qty = float(raw.get("filled_qty") or 0.0)
        avg = raw.get("filled_avg_price")
        return BrokerOrderState(
            client_id=raw["client_order_id"],
            broker_order_id=str(raw["id"]),
            ticker=from_alpaca_symbol(raw["symbol"]),
            side="buy" if str(raw["side"]).lower() == "buy" else "sell",
            status=map_status(str(raw["status"]), filled_qty),
            quantity=float(raw.get("qty") or 0.0),
            filled_quantity=filled_qty,
            avg_fill_price=float(avg) if avg is not None else None,
            updated_at=_parse_ts(raw.get("updated_at")),
        )

    @staticmethod
    def _warn_on_mismatch(order: Order, state: BrokerOrderState) -> None:
        if state.ticker != order.ticker.upper() or state.side != order.side:
            _log.warning(
                "alpaca.order.client_id_mismatch",
                client_id=order.client_id,
                requested_ticker=order.ticker,
                requested_side=order.side,
                broker_ticker=state.ticker,
                broker_side=state.side,
            )

    def _call(self, op: str, fn: Callable[..., Any], *args: Any) -> Any:
        attempt = 0
        while True:
            try:
                return fn(*args)
            except APIError as exc:
                status = _status_code(exc)
                code, message = _error_details(exc)
                if code == _DUPLICATE_CODE or (
                    status == 422 and "client_order_id" in message and "unique" in message
                ):
                    raise _DuplicateClientOrderId(message) from None
                if status == 404:
                    raise _NotFound(message) from None
                retryable = status is not None and (status == 429 or status >= 500)
                error = f"alpaca {op} failed (HTTP {status}): {message}"
            except (requests.ConnectionError, requests.Timeout) as exc:
                retryable = True
                error = f"alpaca {op} failed: {type(exc).__name__}"
            if not retryable or attempt >= self._max_retries:
                raise BrokerError(error) from None
            delay = self._backoff * (2**attempt)
            _log.warning("alpaca.retry", op=op, attempt=attempt + 1, delay=delay, error=error)
            self._sleep(delay)
            attempt += 1


def map_status(alpaca_status: str, filled_quantity: float) -> OrderStatus:
    """Alpaca order status -> our closed ``OrderStatus`` set.

    Anything not explicitly terminal (new, accepted, held, done_for_day,
    pending_*) is still working: ``pending``, or ``partially_filled`` once
    some quantity has filled.
    """
    mapped = _STATUS_MAP.get(alpaca_status.lower())
    if mapped is not None:
        return mapped
    return "partially_filled" if filled_quantity > 0 else "pending"


def fill_time(raw: dict) -> datetime:
    return _parse_ts(raw.get("filled_at")) or _parse_ts(raw.get("updated_at")) or datetime.now(UTC)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _status_code(exc: APIError) -> int | None:
    try:
        return exc.status_code
    except Exception:
        return None


def _error_details(exc: APIError) -> tuple[int | None, str]:
    try:
        body = json.loads(str(exc))
        return body.get("code"), str(body.get("message", ""))
    except (ValueError, TypeError, AttributeError):
        return None, str(exc)[:200]
