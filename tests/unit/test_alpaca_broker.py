"""AlpacaBroker against a hermetic fake of alpaca-py's TradingClient.

The fake speaks the same raw-JSON dict shapes the real client returns with
``raw_data=True`` and raises the real ``alpaca.common.exceptions.APIError``,
so the adapter's error handling is exercised without any network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import requests
from alpaca.common.exceptions import APIError

from stonks.core.types import Fill, Order, Portfolio
from stonks.execution.brokers.alpaca import AlpacaBroker
from stonks.execution.brokers.base import (
    BrokerAccount,
    BrokerError,
    BrokerOrderState,
    LiveTradingRefusedError,
    MarketClock,
    OrderRejectedError,
    UnsupportedTickerError,
)


def api_error(status: int, code: int = 0, message: str = "boom") -> APIError:
    body = json.dumps({"code": code, "message": message})
    http_error = SimpleNamespace(response=SimpleNamespace(status_code=status), request=None)
    return APIError(body, http_error)


def raw_order(
    client_id: str,
    *,
    symbol: str = "AAPL",
    side: str = "buy",
    qty: str = "10",
    filled_qty: str = "0",
    filled_avg_price: str | None = None,
    status: str = "new",
    order_id: str = "broker-1",
    updated_at: str = "2026-01-05T15:30:00Z",
    filled_at: str | None = None,
) -> dict:
    return {
        "id": order_id,
        "client_order_id": client_id,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
        "status": status,
        "updated_at": updated_at,
        "filled_at": filled_at,
    }


class FakeClient:
    def __init__(self) -> None:
        self.account = {"cash": "1000.50", "currency": "USD", "status": "ACTIVE"}
        self.positions: list[dict] = []
        self.orders: dict[str, dict] = {}
        self.submitted: list = []
        self.submit_errors: list[Exception] = []
        self.get_errors: list[Exception] = []
        self.next_status = "new"
        self.next_fill_price: str | None = None
        self.assets: dict[str, dict] = {}
        self.clock = {
            "timestamp": "2026-01-05T10:00:00-05:00",
            "is_open": True,
            "next_open": "2026-01-06T09:30:00-05:00",
            "next_close": "2026-01-05T16:00:00-05:00",
        }
        self.cancelled: list[str] = []
        self.asset_calls: list[str] = []
        self.account_calls = 0

    def get_asset(self, symbol):
        self.asset_calls.append(symbol)
        if symbol in self.assets:
            return self.assets[symbol]
        if symbol.startswith("NOPE"):
            raise api_error(404, 40410000, "asset not found")
        crypto = "/" in symbol
        return {
            "symbol": symbol,
            "class": "crypto" if crypto else "us_equity",
            "status": "active",
            "tradable": True,
            "fractionable": True,
            "min_order_size": "0.0001" if crypto else None,
            "min_trade_increment": "0.0001" if crypto else None,
            "price_increment": "1" if crypto else None,
        }

    def get_clock(self):
        return self.clock

    def cancel_order_by_id(self, order_id):
        for o in self.orders.values():
            if o["id"] == order_id:
                if o["status"] in ("filled", "canceled", "expired", "rejected"):
                    raise api_error(422, 42210000, "order is not cancelable")
                o["status"] = "canceled"
                self.cancelled.append(order_id)
                return
        raise api_error(404, 40410000, "order not found")

    def cancel_orders(self):
        out = []
        for o in self.orders.values():
            if o["status"] in ("new", "accepted", "partially_filled"):
                o["status"] = "canceled"
                out.append({"id": o["id"], "status": 200, "body": None})
        return out

    def get_orders(self, filter=None):
        self.last_orders_filter = filter
        return [
            o
            for o in self.orders.values()
            if o["status"] in ("new", "accepted", "partially_filled")
        ]

    def get_account(self):
        self.account_calls += 1
        return self.account

    def get_all_positions(self):
        return self.positions

    def get_open_position(self, symbol):
        self.position_calls = getattr(self, "position_calls", []) + [symbol]
        for p in self.positions:
            if p["symbol"].replace("/", "") == symbol.replace("/", ""):
                return p
        raise api_error(404, 40410000, "position does not exist")

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        if self.submit_errors:
            raise self.submit_errors.pop(0)
        cid = order_data.client_order_id
        if cid in self.orders:
            raise api_error(422, 40010001, "client_order_id must be unique")
        filled = self.next_status == "filled"
        order = raw_order(
            cid,
            symbol=order_data.symbol,
            side=order_data.side.value,
            qty=str(order_data.qty),
            filled_qty=str(order_data.qty) if filled else "0",
            filled_avg_price=self.next_fill_price if filled else None,
            status=self.next_status,
            order_id=f"broker-{len(self.orders) + 1}",
            filled_at="2026-01-05T15:30:00Z" if filled else None,
        )
        self.orders[cid] = order
        return order

    def get_order_by_client_id(self, client_id):
        if self.get_errors:
            raise self.get_errors.pop(0)
        if client_id not in self.orders:
            raise api_error(404, 40410000, "order not found")
        return self.orders[client_id]


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def broker(client) -> AlpacaBroker:
    return AlpacaBroker(client, max_retries=2, retry_backoff_seconds=0.0, sleep=lambda s: None)


def order(cid="2026-01-05:s1:AAPL.US:buy", **kw) -> Order:
    kw.setdefault("ticker", "AAPL.US")
    kw.setdefault("side", "buy")
    kw.setdefault("quantity", 10)
    return Order(client_id=cid, **kw)


# ---- connection / live guard ------------------------------------------------


def test_connect_refuses_live_without_allow_live():
    with pytest.raises(LiveTradingRefusedError):
        AlpacaBroker.connect("key", "secret", paper=False)


def test_connect_defaults_to_paper(monkeypatch):
    captured = {}

    class Spy:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("stonks.execution.brokers.alpaca.TradingClient", Spy)
    b = AlpacaBroker.connect("key", "secret")
    assert captured["paper"] is True
    assert captured["raw_data"] is True
    assert b.paper is True


def test_connect_live_allowed_with_explicit_flag(monkeypatch):
    captured = {}

    class Spy:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("stonks.execution.brokers.alpaca.TradingClient", Spy)
    b = AlpacaBroker.connect("key", "secret", paper=False, allow_live=True)
    assert captured["paper"] is False
    assert b.paper is False


def test_connect_requires_credentials():
    with pytest.raises(BrokerError, match="credentials"):
        AlpacaBroker.connect("", "secret")


# ---- fetch_portfolio --------------------------------------------------------


def test_fetch_portfolio_maps_cash_and_positions(broker, client):
    client.positions = [
        {"symbol": "AAPL", "qty": "3.5", "side": "long"},
        {"symbol": "BRK.B", "qty": "2", "side": "long"},
    ]
    p = broker.fetch_portfolio()
    assert isinstance(p, Portfolio)
    assert p.cash == pytest.approx(1000.50)
    assert p.positions == {"AAPL.US": 3.5, "BRK-B.US": 2.0}


def test_fetch_portfolio_short_positions_are_negative(broker, client):
    client.positions = [{"symbol": "TSLA", "qty": "-4", "side": "short"}]
    assert broker.fetch_portfolio().positions == {"TSLA.US": -4.0}


def test_fetch_portfolio_maps_crypto_positions(broker, client):
    client.positions = [{"symbol": "BTCUSD", "qty": "0.5", "side": "long", "asset_class": "crypto"}]
    assert broker.fetch_portfolio().positions == {"BTC-USD.CC": 0.5}


def test_fetch_portfolio_rejects_unmappable_positions(broker, client):
    client.positions = [
        {"symbol": "AAPL240119C00100000", "qty": "1", "side": "long", "asset_class": "us_option"}
    ]
    with pytest.raises(UnsupportedTickerError):
        broker.fetch_portfolio()


# ---- place_order ------------------------------------------------------------


def test_market_order_sends_client_order_id_and_fractional_qty(broker, client):
    result = broker.place_order(order(quantity=1.25))
    assert result is None  # accepted, not yet filled
    req = client.submitted[0]
    assert req.symbol == "AAPL"
    assert req.qty == pytest.approx(1.25)
    assert req.side.value == "buy"
    assert req.time_in_force.value == "day"
    assert req.client_order_id == "2026-01-05:s1:AAPL.US:buy"
    assert req.type.value == "market"


AAPL_LONG_10 = {
    "symbol": "AAPL",
    "qty": "10",
    "qty_available": "10",
    "side": "long",
    "asset_class": "us_equity",
}


def test_limit_order_carries_limit_price(broker, client):
    client.positions = [AAPL_LONG_10]
    broker.place_order(
        order(cid="c-sell", side="sell", order_type="limit", limit_price=101.5, quantity=2)
    )
    req = client.submitted[0]
    assert req.type.value == "limit"
    assert req.limit_price == pytest.approx(101.5)
    assert req.side.value == "sell"


def test_stop_orders_are_not_supported(broker):
    with pytest.raises(BrokerError, match="stop"):
        broker.place_order(order(order_type="stop"))


def test_non_us_ticker_rejected_before_submission(broker, client):
    with pytest.raises(UnsupportedTickerError):
        broker.place_order(order(ticker="VOD.LSE"))
    assert client.submitted == []


def test_filled_on_submission_returns_fill(broker, client):
    client.next_status = "filled"
    client.next_fill_price = "190.25"
    fill = broker.place_order(order(quantity=10))
    assert isinstance(fill, Fill)
    assert fill.order_client_id == "2026-01-05:s1:AAPL.US:buy"
    assert fill.ticker == "AAPL.US"
    assert fill.quantity == pytest.approx(10)
    assert fill.price == pytest.approx(190.25)
    assert fill.fee == 0.0
    assert fill.side == "buy"
    assert fill.filled_at == datetime(2026, 1, 5, 15, 30, tzinfo=UTC)


def test_duplicate_client_order_id_fetches_existing_order(broker, client):
    client.orders["2026-01-05:s1:AAPL.US:buy"] = raw_order(
        "2026-01-05:s1:AAPL.US:buy", status="filled", filled_qty="10", filled_avg_price="50"
    )
    fill = broker.place_order(order())
    assert fill is not None
    assert fill.price == pytest.approx(50)


def test_resubmission_of_pending_order_returns_none_without_error(broker, client):
    assert broker.place_order(order()) is None
    assert broker.place_order(order()) is None
    assert len(client.orders) == 1


def test_transient_errors_are_retried(broker, client):
    client.submit_errors = [api_error(429, message="rate limit"), requests.ConnectionError("x")]
    assert broker.place_order(order()) is None
    assert len(client.submitted) == 3


def test_retry_after_lost_response_resolves_via_duplicate(broker, client):
    # First attempt reaches Alpaca (order created) but the response is lost.
    def lost_response(order_data):
        FakeClient.submit_order(client, order_data)
        raise requests.Timeout("read timeout")

    calls = {"n": 0}
    original = client.submit_order

    def flaky(order_data):
        calls["n"] += 1
        if calls["n"] == 1:
            return lost_response(order_data)
        return original(order_data)

    client.submit_order = flaky
    assert broker.place_order(order()) is None
    assert len(client.orders) == 1


def test_retries_are_bounded(broker, client):
    client.submit_errors = [api_error(503)] * 5
    with pytest.raises(BrokerError):
        broker.place_order(order())
    assert len(client.submitted) == 3  # 1 + max_retries


def test_non_retryable_error_raises_immediately(broker, client):
    client.submit_errors = [api_error(403, 40310000, "insufficient buying power")]
    with pytest.raises(BrokerError, match="insufficient buying power"):
        broker.place_order(order())
    assert len(client.submitted) == 1


# ---- get_order_state --------------------------------------------------------


def test_get_order_state_unknown_returns_none(broker):
    assert broker.get_order_state("nope") is None


@pytest.mark.parametrize(
    ("alpaca_status", "filled_qty", "expected"),
    [
        ("new", "0", "pending"),
        ("accepted", "0", "pending"),
        ("partially_filled", "4", "partially_filled"),
        ("filled", "10", "filled"),
        ("canceled", "0", "cancelled"),
        ("expired", "3", "cancelled"),
        ("rejected", "0", "rejected"),
        ("done_for_day", "3", "partially_filled"),
    ],
)
def test_get_order_state_maps_status(broker, client, alpaca_status, filled_qty, expected):
    client.orders["c"] = raw_order(
        "c", status=alpaca_status, filled_qty=filled_qty, filled_avg_price="10"
    )
    state = broker.get_order_state("c")
    assert isinstance(state, BrokerOrderState)
    assert state.status == expected
    assert state.filled_quantity == pytest.approx(float(filled_qty))
    assert state.ticker == "AAPL.US"
    assert state.broker_order_id == "broker-1"


# ---- reconcile ---------------------------------------------------------------


def test_reconcile_returns_only_new_fill_deltas(broker, client):
    broker.place_order(order(quantity=10))
    cid = "2026-01-05:s1:AAPL.US:buy"
    assert broker.reconcile() == []

    client.orders[cid].update(status="partially_filled", filled_qty="4", filled_avg_price="100")
    fills = broker.reconcile()
    assert [(f.quantity, f.price) for f in fills] == [(pytest.approx(4), pytest.approx(100))]

    client.orders[cid].update(status="filled", filled_qty="10", filled_avg_price="101.2")
    fills = broker.reconcile()
    # 6 more shares; 10*101.2 - 4*100 = 612 -> 102/share
    assert [(f.quantity, f.price) for f in fills] == [(pytest.approx(6), pytest.approx(102))]
    assert broker.reconcile() == []


def test_reconcile_does_not_repeat_fill_returned_by_place_order(broker, client):
    client.next_status = "filled"
    client.next_fill_price = "10"
    assert broker.place_order(order()) is not None
    assert broker.reconcile() == []


def test_reconcile_soft_fails_per_order(broker, client):
    broker.place_order(order())
    client.get_errors = [api_error(500)] * 10
    assert broker.reconcile() == []


# ---- crypto ---------------------------------------------------------------------


def test_crypto_orders_use_gtc_and_slash_symbol(broker, client):
    broker.place_order(order(cid="c-btc", ticker="BTC-USD.CC", quantity=0.01234))
    req = client.submitted[0]
    assert req.symbol == "BTC/USD"
    assert req.time_in_force.value == "gtc"
    assert req.qty == pytest.approx(0.0123)  # floored to min_trade_increment


def test_crypto_below_min_order_size_is_rejected(broker, client):
    with pytest.raises(OrderRejectedError, match="minimum"):
        broker.place_order(order(cid="c-btc", ticker="BTC-USD.CC", quantity=0.00005))
    assert client.submitted == []


def test_crypto_limit_price_rounded_to_price_increment(broker, client):
    broker.place_order(
        order(cid="c", ticker="BTC-USD.CC", quantity=0.01, order_type="limit", limit_price=65000.7)
    )
    assert client.submitted[0].limit_price == pytest.approx(65000)


# ---- pre-trade checks --------------------------------------------------------------

GME_NON_FRACTIONABLE = {
    "symbol": "GME",
    "class": "us_equity",
    "status": "active",
    "tradable": True,
    "fractionable": False,
}


def test_untradable_asset_is_rejected_before_submission(broker, client):
    client.assets["XYZ"] = {
        "symbol": "XYZ",
        "class": "us_equity",
        "status": "inactive",
        "tradable": False,
        "fractionable": False,
    }
    with pytest.raises(OrderRejectedError, match="not tradable"):
        broker.place_order(order(ticker="XYZ.US"))
    assert client.submitted == []


def test_asset_unknown_to_alpaca_is_unsupported(broker, client):
    with pytest.raises(UnsupportedTickerError, match="unknown"):
        broker.place_order(order(ticker="NOPE.US"))


def test_non_fractionable_asset_quantity_is_floored(broker, client):
    client.assets["GME"] = GME_NON_FRACTIONABLE
    broker.place_order(order(ticker="GME.US", quantity=3.7))
    assert client.submitted[0].qty == 3


def test_non_fractionable_asset_below_one_share_is_rejected(broker, client):
    client.assets["GME"] = GME_NON_FRACTIONABLE
    with pytest.raises(OrderRejectedError):
        broker.place_order(order(ticker="GME.US", quantity=0.4))
    assert client.submitted == []


def test_asset_lookups_are_cached(broker, client):
    broker.place_order(order(cid="a"))
    broker.place_order(order(cid="b"))
    assert client.asset_calls == ["AAPL"]


@pytest.mark.parametrize(("price", "expected"), [(101.23456, 101.23), (0.123456, 0.1234)])
def test_equity_limit_price_rounded_to_tick(broker, client, price, expected):
    broker.place_order(order(order_type="limit", limit_price=price))
    assert client.submitted[0].limit_price == pytest.approx(expected)


def test_sell_limit_price_rounds_up_never_below_requested(broker, client):
    client.positions = [AAPL_LONG_10]
    broker.place_order(order(cid="s", side="sell", order_type="limit", limit_price=101.23001))
    assert client.submitted[0].limit_price == pytest.approx(101.24)


def test_blocked_account_refuses_orders(broker, client):
    client.account = {"cash": "1", "status": "ACTIVE", "trading_blocked": True}
    with pytest.raises(OrderRejectedError, match="blocked"):
        broker.place_order(order())
    assert client.submitted == []


def test_inactive_account_refuses_orders(broker, client):
    client.account = {"cash": "1", "status": "ACCOUNT_UPDATED", "trading_blocked": False}
    with pytest.raises(OrderRejectedError, match="ACCOUNT_UPDATED"):
        broker.place_order(order())


def test_account_checked_once_per_instance(broker, client):
    client.account = {"cash": "1", "status": "ACTIVE"}
    broker.place_order(order(cid="a"))
    broker.place_order(order(cid="b"))
    assert client.account_calls == 1


# ---- account, clock, order management ------------------------------------------------


def test_fetch_account(broker, client):
    client.account = {
        "cash": "1000.5",
        "equity": "2500",
        "buying_power": "4000",
        "currency": "USD",
        "status": "ACTIVE",
        "trading_blocked": False,
        "pattern_day_trader": True,
    }
    acct = broker.fetch_account()
    assert isinstance(acct, BrokerAccount)
    assert acct.cash == pytest.approx(1000.5)
    assert acct.equity == pytest.approx(2500)
    assert acct.buying_power == pytest.approx(4000)
    assert acct.currency == "USD"
    assert acct.can_trade is True
    assert acct.pattern_day_trader is True


def test_market_clock(broker):
    clock = broker.get_market_clock()
    assert isinstance(clock, MarketClock)
    assert clock.is_open is True
    assert clock.next_close == datetime(2026, 1, 5, 21, 0, tzinfo=UTC)


def test_list_open_orders_returns_our_types(broker, client):
    broker.place_order(order(cid="a"))
    broker.place_order(order(cid="b", ticker="BTC-USD.CC", quantity=0.01))
    open_orders = broker.list_open_orders()
    assert {o.client_id for o in open_orders} == {"a", "b"}
    assert all(isinstance(o, BrokerOrderState) for o in open_orders)
    assert client.last_orders_filter.status.value == "open"


def test_cancel_order_by_client_id(broker, client):
    broker.place_order(order(cid="a"))
    assert broker.cancel_order("a") is True
    assert broker.get_order_state("a").status == "cancelled"


def test_cancel_unknown_or_terminal_order_returns_false(broker, client):
    assert broker.cancel_order("nope") is False
    client.next_status = "filled"
    client.next_fill_price = "1"
    broker.place_order(order(cid="f"))
    assert broker.cancel_order("f") is False


def test_cancel_all_orders(broker, client):
    broker.place_order(order(cid="a"))
    broker.place_order(order(cid="b"))
    assert broker.cancel_all_orders() == 2


# ---- self-review hardening ---------------------------------------------------------


def test_sell_more_than_available_is_rejected_not_shorted(broker, client):
    client.positions = [dict(AAPL_LONG_10, qty_available="3")]
    with pytest.raises(OrderRejectedError, match="short"):
        broker.place_order(order(cid="s", side="sell", quantity=5))
    assert client.submitted == []


def test_sell_without_position_is_rejected(broker, client):
    with pytest.raises(OrderRejectedError):
        broker.place_order(order(cid="s", side="sell", quantity=1))
    assert client.submitted == []


def test_crypto_sell_checks_position_by_slashless_symbol(broker, client):
    client.positions = [
        {
            "symbol": "BTCUSD",
            "qty": "1",
            "qty_available": "1",
            "side": "long",
            "asset_class": "crypto",
        }
    ]
    broker.place_order(order(cid="s", ticker="BTC-USD.CC", side="sell", quantity=0.5))
    assert client.position_calls == ["BTCUSD"]
    assert client.submitted[0].qty == pytest.approx(0.5)


def test_resubmitting_existing_sell_resolves_before_pre_trade_checks(broker, client):
    # The first submission locked the shares (qty_available 0) and the account
    # has since been blocked; a crashed-tick rerun must still resolve to the
    # existing order instead of failing the checks or submitting again.
    cid = "2026-01-05:s1:AAPL.US:sell"
    client.orders[cid] = raw_order(cid, side="sell", qty="10")
    client.positions = [dict(AAPL_LONG_10, qty_available="0")]
    client.account = {"cash": "1", "status": "ACTIVE", "trading_blocked": True}
    assert broker.place_order(order(cid=cid, side="sell", quantity=10)) is None
    assert client.submitted == []


def test_retry_backoff_is_capped(client):
    delays = []
    b = AlpacaBroker(client, max_retries=10, retry_backoff_seconds=10.0, sleep=delays.append)
    client.submit_errors = [api_error(503)] * 3
    b.place_order(order())
    assert max(delays) <= 30.0
