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
    BrokerError,
    BrokerOrderState,
    LiveTradingRefusedError,
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
        self.account = {"cash": "1000.50", "currency": "USD"}
        self.positions: list[dict] = []
        self.orders: dict[str, dict] = {}
        self.submitted: list = []
        self.submit_errors: list[Exception] = []
        self.get_errors: list[Exception] = []
        self.next_status = "new"
        self.next_fill_price: str | None = None

    def get_account(self):
        return self.account

    def get_all_positions(self):
        return self.positions

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


def test_fetch_portfolio_rejects_unmappable_positions(broker, client):
    client.positions = [{"symbol": "BTC/USD", "qty": "1", "side": "long"}]
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


def test_limit_order_carries_limit_price(broker, client):
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
