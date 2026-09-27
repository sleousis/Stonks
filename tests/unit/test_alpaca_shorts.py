"""Phase 16.1: the Alpaca adapter maps short sales, off by default."""

from __future__ import annotations

import pytest

from stonks.core.types import Order
from stonks.execution.brokers.alpaca import AlpacaBroker
from stonks.execution.brokers.base import OrderRejectedError
from tests.unit.test_alpaca_broker import FakeClient


def _broker(client: FakeClient, allow_short: bool = True) -> AlpacaBroker:
    return AlpacaBroker(
        client,
        max_retries=0,
        retry_backoff_seconds=0.0,
        sleep=lambda s: None,
        allow_short=allow_short,
    )


def _short(qty: float = 5.0, ticker: str = "AAPL.US") -> Order:
    return Order(f"2026-01-05:s1:{ticker}:short", ticker, "sell", qty, position_effect="open")


def _shortable(client: FakeClient, **extra) -> None:
    client.assets["AAPL"] = {
        "symbol": "AAPL",
        "class": "us_equity",
        "status": "active",
        "tradable": True,
        "fractionable": True,
        "shortable": True,
        "easy_to_borrow": True,
        **extra,
    }


def test_short_sales_are_off_by_default() -> None:
    client = FakeClient()
    _shortable(client)
    broker = AlpacaBroker(client, max_retries=0, retry_backoff_seconds=0.0, sleep=lambda s: None)
    assert broker.allow_short is False
    with pytest.raises(OrderRejectedError, match="short selling is off"):
        broker.place_order(_short())
    assert client.submitted == []


def test_a_short_sale_is_sent_as_a_whole_share_sell() -> None:
    client = FakeClient()
    _shortable(client)
    _broker(client).place_order(_short(qty=5.7))
    [req] = client.submitted
    assert str(req.side).lower().endswith("sell")
    assert req.qty == 5  # whole shares only
    assert req.client_order_id == "2026-01-05:s1:AAPL.US:short"


@pytest.mark.parametrize(
    ("extra", "match"),
    [({"shortable": False}, "not shortable"), ({"easy_to_borrow": False}, "hard to borrow")],
)
def test_unborrowable_assets_are_refused(extra, match) -> None:
    client = FakeClient()
    _shortable(client, **extra)
    with pytest.raises(OrderRejectedError, match=match):
        _broker(client).place_order(_short())
    assert client.submitted == []


def test_crypto_is_never_shorted() -> None:
    client = FakeClient()
    with pytest.raises(OrderRejectedError, match="crypto"):
        _broker(client).place_order(_short(ticker="BTC-USD.CC"))


def test_a_plain_oversell_is_still_refused_with_shorts_on() -> None:
    client = FakeClient()
    _shortable(client)
    with pytest.raises(OrderRejectedError, match="short"):
        _broker(client).place_order(Order("x:sell", "AAPL.US", "sell", 5.0))
    assert client.submitted == []
