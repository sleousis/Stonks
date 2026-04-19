"""Unit tests for the execution order-id helper."""

from __future__ import annotations

from stonks.execution.orders import make_client_id


def test_client_id_is_deterministic_for_same_inputs():
    a = make_client_id(tick_id="t1", strategy_id="s1", ticker="AAPL.US", side="buy")
    b = make_client_id(tick_id="t1", strategy_id="s1", ticker="AAPL.US", side="buy")
    assert a == b


def test_client_id_differs_by_side():
    buy = make_client_id(tick_id="t1", strategy_id="s1", ticker="AAPL.US", side="buy")
    sell = make_client_id(tick_id="t1", strategy_id="s1", ticker="AAPL.US", side="sell")
    assert buy != sell


def test_client_id_differs_by_tick():
    a = make_client_id(tick_id="t1", strategy_id="s1", ticker="AAPL.US", side="buy")
    b = make_client_id(tick_id="t2", strategy_id="s1", ticker="AAPL.US", side="buy")
    assert a != b


def test_client_id_shape_contains_tick_prefix():
    cid = make_client_id(tick_id="tick_123", strategy_id="s1", ticker="AAPL.US", side="buy")
    assert cid.startswith("tick_123:")
    assert "AAPL.US" in cid
    assert "buy" in cid
