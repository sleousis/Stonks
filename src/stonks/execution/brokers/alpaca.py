"""Alpaca broker adapter (paper by default) wrapping ``alpaca-py``.

Every alpaca-py type stays inside this module: the client is created with
``raw_data=True`` so responses are plain JSON dicts, and everything leaving
the adapter is one of our types (``Portfolio``, ``Fill``,
``BrokerOrderState``, ``BrokerAccount``, ``MarketClock``) or one of our
exceptions (``BrokerError`` & co).

Idempotency: ``Order.client_id`` is sent as Alpaca's ``client_order_id``.
Alpaca refuses a second order with the same id, so a resubmission (a crashed
tick re-run, or a retry after a lost response) resolves to the existing
order instead of creating a second one.

Supported instruments: US equities (``AAPL.US``, fractional where the asset
allows it, time-in-force DAY) and crypto pairs (``BTC-USD.CC``, GTC).
Before the first order an instance checks the account can trade; before
each order it checks the asset is tradable and fits quantity and price to
the asset's increments, so avoidable rejections never reach the API.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaSide
from alpaca.trading.enums import QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest

from stonks.core.types import Fill, Order, OrderStatus, Portfolio
from stonks.execution.brokers.base import (
    QTY_EPSILON,
    BrokerAccount,
    BrokerError,
    BrokerOrderState,
    LiveTradingRefusedError,
    MarketClock,
    OrderRejectedError,
    UnsupportedTickerError,
    delta_fill,
)
from stonks.execution.brokers.symbols import (
    from_alpaca_symbol,
    is_crypto_ticker,
    to_alpaca_symbol,
)
from stonks.logging import get_logger

_log = get_logger("stonks.execution.brokers.alpaca")

# Alpaca caps client_order_id at 128 characters. We refuse rather than
# truncate: truncation could make two distinct orders collide.
MAX_CLIENT_ORDER_ID_LEN = 128
# Alpaca accepts up to 9 decimal places for fractional equity quantities.
_EQUITY_QTY_INCREMENT = Decimal("0.000000001")
_DUPLICATE_CODE = 40010001
_OPEN_ORDERS_LIMIT = 500
MAX_BACKOFF_SECONDS = 30.0

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


class _Unprocessable(Exception):
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
        self._assets: dict[str, dict] = {}
        self._account_ok = False

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
            ticker = from_alpaca_symbol(pos["symbol"], asset_class=pos.get("asset_class"))
            qty = float(pos["qty"])
            if pos.get("side") == "short" and qty > 0:
                qty = -qty
            positions[ticker] = qty
        return Portfolio(cash=float(account["cash"]), positions=positions)

    def place_order(self, order: Order) -> Fill | None:
        symbol = self._validate_static(order)
        # Resubmission first: an order already at Alpaca under this client_id
        # resolves to that order *before* any pre-trade check, so a rerun
        # can't fail on state the first submission itself changed (shares
        # now locked by the pending sell, cash reserved by the pending buy).
        raw = self._fetch_order(order.client_id)
        if raw is not None:
            _log.info("alpaca.order.already_submitted", client_id=order.client_id)
        else:
            request, qty = self._build_request(order, symbol)
            self._ensure_account_can_trade()
            if order.side == "sell":
                self._ensure_sellable(order, symbol, qty)
            raw = self._submit(order, request)
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

    # ---- account, market clock, order management ----------------------------

    def fetch_account(self) -> BrokerAccount:
        raw = self._call("get_account", self._client.get_account)
        return _to_account(raw)

    def get_market_clock(self) -> MarketClock:
        raw = self._call("get_clock", self._client.get_clock)
        return MarketClock(
            timestamp=_require_ts(raw, "timestamp"),
            is_open=bool(raw["is_open"]),
            next_open=_require_ts(raw, "next_open"),
            next_close=_require_ts(raw, "next_close"),
        )

    def list_open_orders(self) -> list[BrokerOrderState]:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=_OPEN_ORDERS_LIMIT)
        raws = self._call("get_orders", self._client.get_orders, request)
        states: list[BrokerOrderState] = []
        for raw in raws:
            try:
                states.append(self._to_state(raw))
            except UnsupportedTickerError:
                _log.warning("alpaca.open_order.unsupported", symbol=raw.get("symbol"))
        return states

    def cancel_order(self, client_id: str) -> bool:
        """Cancel one working order by our client_id. Returns False when the
        order is unknown or already terminal (nothing to cancel)."""
        raw = self._fetch_order(client_id)
        if raw is None or self._to_state(raw).status in _TERMINAL:
            return False
        try:
            self._call("cancel_order", self._client.cancel_order_by_id, str(raw["id"]))
        except (_NotFound, _Unprocessable):
            return False
        self._open.discard(client_id)
        _log.info("alpaca.order.cancelled", client_id=client_id)
        return True

    def cancel_all_orders(self) -> int:
        """Cancel every open order on the account; returns how many Alpaca
        accepted for cancellation."""
        responses = self._call("cancel_orders", self._client.cancel_orders) or []
        cancelled = sum(1 for r in responses if int(r.get("status", 0)) // 100 == 2)
        _log.info("alpaca.orders.cancel_all", cancelled=cancelled)
        return cancelled

    # ---- pre-trade checks ---------------------------------------------------

    def _ensure_account_can_trade(self) -> None:
        if self._account_ok:
            return
        account = self.fetch_account()
        if account.trading_blocked:
            raise OrderRejectedError("Alpaca account is blocked from trading")
        if not account.can_trade:
            raise OrderRejectedError(f"Alpaca account status is {account.status!r}, not ACTIVE")
        self._account_ok = True

    def _asset(self, symbol: str) -> dict:
        cached = self._assets.get(symbol)
        if cached is not None:
            return cached
        try:
            asset = self._call("get_asset", self._client.get_asset, symbol)
        except _NotFound:
            raise UnsupportedTickerError(f"symbol {symbol!r} is unknown to Alpaca") from None
        self._assets[symbol] = asset
        return asset

    def _ensure_sellable(self, order: Order, symbol: str, qty: float) -> None:
        """Refuse sells beyond the unencumbered holding: at Alpaca such a sell
        would silently open a short position, which no strategy asked for."""
        try:
            pos = self._call(
                "get_open_position", self._client.get_open_position, symbol.replace("/", "")
            )
            available = float(pos.get("qty_available", pos.get("qty")) or 0.0)
        except _NotFound:
            available = 0.0
        if qty > available + QTY_EPSILON:
            raise OrderRejectedError(
                f"sell of {qty} {order.ticker} exceeds the available position of {available}; "
                "refusing to open a short"
            )

    # ---- internals ----------------------------------------------------------

    @staticmethod
    def _validate_static(order: Order) -> str:
        """Checks that need no API call; returns the Alpaca symbol."""
        symbol = to_alpaca_symbol(order.ticker)
        if len(order.client_id) > MAX_CLIENT_ORDER_ID_LEN:
            raise BrokerError(
                f"client_id longer than {MAX_CLIENT_ORDER_ID_LEN} chars is not accepted "
                f"by Alpaca: {order.client_id!r}"
            )
        if order.order_type not in ("market", "limit"):
            raise BrokerError(
                f"order_type {order.order_type!r} is not supported by the Alpaca broker "
                "(market and limit only; stop orders need a stop price)"
            )
        return symbol

    def _submit(self, order: Order, request: MarketOrderRequest | LimitOrderRequest) -> dict:
        try:
            return self._call("submit_order", self._client.submit_order, request)
        except _DuplicateClientOrderId:
            # Lost a race, or a retry after a lost response: the order exists.
            _log.info("alpaca.order.duplicate_resolved", client_id=order.client_id)
            raw = self._fetch_order(order.client_id)
            if raw is None:
                raise BrokerError(
                    f"Alpaca reported client_order_id {order.client_id!r} as duplicate "
                    "but the order could not be fetched"
                ) from None
            return raw
        except _Unprocessable as exc:
            raise OrderRejectedError(f"Alpaca rejected order {order.client_id!r}: {exc}") from None

    def _build_request(
        self, order: Order, symbol: str
    ) -> tuple[MarketOrderRequest | LimitOrderRequest, float]:
        """The Alpaca request and the fitted quantity it sends."""
        crypto = is_crypto_ticker(order.ticker)
        asset = self._asset(symbol)
        if not asset.get("tradable", False) or str(asset.get("status", "")).lower() != "active":
            raise OrderRejectedError(f"{order.ticker} is not tradable at Alpaca")
        qty = _fit_quantity(order, asset, crypto)
        common = {
            "symbol": symbol,
            "qty": qty,
            "side": AlpacaSide.BUY if order.side == "buy" else AlpacaSide.SELL,
            # Equities: DAY is the only time-in-force Alpaca accepts for
            # fractional qty. Crypto: DAY is not accepted; GTC is.
            "time_in_force": TimeInForce.GTC if crypto else TimeInForce.DAY,
            "client_order_id": order.client_id,
        }
        if order.order_type == "market":
            return MarketOrderRequest(**common), qty
        if order.limit_price is None:
            raise OrderRejectedError(f"limit order {order.client_id!r} has no limit price")
        limit_price = _fit_limit_price(float(order.limit_price), asset, crypto, order.side)
        return LimitOrderRequest(limit_price=limit_price, **common), qty

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
            ticker=from_alpaca_symbol(raw["symbol"], asset_class=raw.get("asset_class")),
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
                if status == 422:
                    raise _Unprocessable(message) from None
                retryable = status is not None and (status == 429 or status >= 500)
                error = f"alpaca {op} failed (HTTP {status}): {message}"
            except (requests.ConnectionError, requests.Timeout) as exc:
                retryable = True
                error = f"alpaca {op} failed: {type(exc).__name__}"
            if not retryable or attempt >= self._max_retries:
                raise BrokerError(error) from None
            delay = min(self._backoff * (2**attempt), MAX_BACKOFF_SECONDS)
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


def _fit_quantity(order: Order, asset: dict, crypto: bool) -> float:
    """Floor the quantity to what the asset accepts; never round *up* (a sell
    must not exceed the holding, a buy must not exceed the sized budget)."""
    qty = Decimal(str(order.quantity))
    if crypto:
        increment = _decimal(asset.get("min_trade_increment")) or _EQUITY_QTY_INCREMENT
    elif asset.get("fractionable", False):
        increment = _EQUITY_QTY_INCREMENT
    else:
        increment = Decimal(1)
    fitted = (qty / increment).to_integral_value(rounding=ROUND_FLOOR) * increment
    minimum = _decimal(asset.get("min_order_size")) or increment
    if fitted <= 0 or fitted < minimum:
        raise OrderRejectedError(
            f"quantity {order.quantity} of {order.ticker} is below the minimum tradable "
            f"size {minimum} (increment {increment})"
        )
    if fitted != qty:
        _log.info(
            "alpaca.order.qty_fitted",
            client_id=order.client_id,
            requested=order.quantity,
            submitted=float(fitted),
        )
    return float(fitted)


def _fit_limit_price(price: float, asset: dict, crypto: bool, side: str) -> float:
    """Snap a limit price onto the tick grid, never to a worse price than
    requested: buys round down, sells round up."""
    increment = _decimal(asset.get("price_increment")) if crypto else None
    if increment is None:
        # US equities: penny ticks at/above $1, 1/100 cent below.
        increment = Decimal("0.01") if price >= 1 else Decimal("0.0001")
    rounding = ROUND_FLOOR if side == "buy" else ROUND_CEILING
    ticks = (Decimal(str(price)) / increment).to_integral_value(rounding=rounding)
    return float(ticks * increment)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
    except ArithmeticError:
        return None
    return d if d > 0 and math.isfinite(d) else None


def _to_account(raw: dict) -> BrokerAccount:
    def num(key: str) -> float:
        value = raw.get(key)
        return float(value) if value not in (None, "") else 0.0

    blocked = any(
        bool(raw.get(flag))
        for flag in ("trading_blocked", "account_blocked", "trade_suspended_by_user")
    )
    return BrokerAccount(
        cash=num("cash"),
        equity=num("equity"),
        buying_power=num("buying_power"),
        currency=str(raw.get("currency") or "USD"),
        status=str(raw.get("status") or "UNKNOWN"),
        trading_blocked=blocked,
        pattern_day_trader=bool(raw.get("pattern_day_trader", False)),
    )


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
    return ts.astimezone(UTC) if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _require_ts(raw: dict, key: str) -> datetime:
    ts = _parse_ts(raw.get(key))
    if ts is None:
        raise BrokerError(f"Alpaca clock response has no valid {key!r}")
    return ts


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
