"""IBKR order mapping (roadmap 19.2): orderRef, whole shares, collars, ticks."""

from __future__ import annotations

import pytest

from stonks.core.instruments import InstrumentSpec
from stonks.core.types import Order
from stonks.execution.brokers.base import OrderRejectedError
from stonks.execution.brokers.ibkr.orders import (
    HASH_PREFIX,
    broker_ref,
    collar_price,
    snap_price,
    to_ib_order,
    whole_shares,
)
from stonks.execution.brokers.ibkr.settings import IbkrOrderSettings

SPEC = InstrumentSpec.spot("AAPL.US", tick_size=0.01)
SETTINGS = IbkrOrderSettings()


def order(**kw) -> Order:
    base = {"client_id": "tick1-s1-AAPL.US-buy", "ticker": "AAPL.US", "side": "buy",
            "quantity": 10.0}  # fmt: skip
    base.update(kw)
    return Order(**base)


def test_broker_ref_keeps_a_short_client_id():
    assert broker_ref("abc", 40) == "abc"


def test_broker_ref_hashes_a_long_client_id_stably():
    long_id = "x" * 80
    ref = broker_ref(long_id, 40)
    assert ref.startswith(HASH_PREFIX)
    assert len(ref) == len(HASH_PREFIX) + 20
    assert ref == broker_ref(long_id, 40)
    assert ref != broker_ref("y" * 80, 40)


def test_broker_ref_refuses_an_empty_client_id():
    with pytest.raises(ValueError):
        broker_ref("", 40)


def test_whole_shares_rounds_down_and_refuses_below_one():
    assert whole_shares(order(quantity=10.9)) == 10
    assert whole_shares(order(quantity=2.9999999999)) == 3
    with pytest.raises(OrderRejectedError, match="below one share"):
        whole_shares(order(quantity=0.5))


def test_snap_price_never_worse():
    assert snap_price(100.019, 0.01, side="buy") == pytest.approx(100.01)
    assert snap_price(100.011, 0.01, side="sell") == pytest.approx(100.02)
    assert snap_price(100.015, 0.01, side=None) == pytest.approx(100.02)
    with pytest.raises(OrderRejectedError):
        snap_price(0.001, 0.01, side="buy")
    with pytest.raises(OrderRejectedError):
        snap_price(float("nan"), 0.01, side="buy")


def test_collar_price_moves_through_the_reference():
    assert collar_price(100.0, "buy", 100, 0.01) == pytest.approx(101.0)
    assert collar_price(100.0, "sell", 100, 0.01) == pytest.approx(99.0)


def test_market_with_reference_becomes_a_collared_limit_on_open():
    req = to_ib_order(order(decision_price=200.0), SPEC, account="DU1", settings=SETTINGS)
    assert req.order_type == "LMT"
    assert req.limit_price == pytest.approx(202.0)
    assert req.tif == "OPG"
    assert req.action == "BUY"
    assert req.total_quantity == 10.0
    assert req.account == "DU1"
    assert req.order_ref == "tick1-s1-AAPL.US-buy"
    assert req.outside_rth is False


def test_sell_collar_is_below_the_reference():
    req = to_ib_order(
        order(side="sell", decision_price=200.0, position_effect="close"),
        SPEC,
        account="DU1",
        settings=SETTINGS,
    )
    assert req.action == "SELL"
    assert req.limit_price == pytest.approx(198.0)


def test_market_close_without_reference_is_plain_market():
    req = to_ib_order(
        order(side="sell", position_effect="close"), SPEC, account="DU1", settings=SETTINGS
    )
    assert req.order_type == "MKT"
    assert req.limit_price is None


def test_opening_market_without_reference_is_refused():
    with pytest.raises(OrderRejectedError, match="reference price"):
        to_ib_order(order(), SPEC, account="DU1", settings=SETTINGS)


def test_zero_collar_sends_market_only_for_closes():
    s = IbkrOrderSettings(collar_bps=0)
    req = to_ib_order(
        order(side="sell", position_effect="close", decision_price=10.0),
        SPEC,
        account="DU1",
        settings=s,
    )
    assert req.order_type == "MKT"


def test_limit_snaps_to_the_tick():
    req = to_ib_order(
        order(order_type="limit", limit_price=150.129, time_in_force="day"),
        SPEC,
        account="DU1",
        settings=SETTINGS,
    )
    assert req.order_type == "LMT"
    assert req.limit_price == pytest.approx(150.12)
    assert req.tif == "DAY"


def test_stop_needs_a_stop_price_and_defaults_to_day():
    with pytest.raises(OrderRejectedError, match="stop_price"):
        to_ib_order(order(order_type="stop"), SPEC, account="DU1", settings=SETTINGS)
    req = to_ib_order(
        order(order_type="stop", side="sell", stop_price=90.004, position_effect="close"),
        SPEC,
        account="DU1",
        settings=SETTINGS,
    )
    assert (req.order_type, req.aux_price, req.tif) == ("STP", 90.0, "DAY")


def test_stop_limit_carries_both_prices_and_may_be_gtc():
    req = to_ib_order(
        order(
            order_type="stop_limit",
            side="sell",
            stop_price=90.0,
            limit_price=89.5,
            time_in_force="gtc",
        ),
        SPEC,
        account="DU1",
        settings=SETTINGS,
    )
    assert (req.order_type, req.aux_price, req.limit_price, req.tif) == (
        "STP LMT",
        90.0,
        89.5,
        "GTC",
    )


def test_gtc_is_only_for_stops_and_opg_never_for_stops():
    with pytest.raises(OrderRejectedError, match="only for stop"):
        to_ib_order(
            order(order_type="limit", limit_price=10.0, time_in_force="gtc"),
            SPEC,
            account="DU1",
            settings=SETTINGS,
        )
    with pytest.raises(OrderRejectedError, match="opening auction"):
        to_ib_order(
            order(order_type="stop", stop_price=10.0, time_in_force="opg"),
            SPEC,
            account="DU1",
            settings=SETTINGS,
        )


def test_outside_rth_and_missing_account_are_refused():
    with pytest.raises(OrderRejectedError, match="outside regular hours"):
        to_ib_order(order(outside_rth=True, decision_price=1.0), SPEC, account="DU1",
                    settings=SETTINGS)  # fmt: skip
    with pytest.raises(OrderRejectedError, match="account"):
        to_ib_order(order(decision_price=1.0), SPEC, account="", settings=SETTINGS)


def test_ioc_maps():
    req = to_ib_order(
        order(order_type="limit", limit_price=10.0, time_in_force="ioc"),
        SPEC,
        account="DU1",
        settings=SETTINGS,
    )
    assert req.tif == "IOC"


def test_long_client_id_goes_out_hashed():
    req = to_ib_order(
        order(client_id="c" * 60, decision_price=10.0), SPEC, account="DU1", settings=SETTINGS
    )
    assert req.order_ref.startswith(HASH_PREFIX)


def test_a_protective_stop_goes_out_gtc_in_its_oca_group():
    req = to_ib_order(
        order(
            order_type="stop",
            side="sell",
            stop_price=90.0,
            time_in_force="gtc",
            position_effect="close",
            oca_group="stk-oca-1",
        ),
        SPEC,
        account="DU1",
        settings=SETTINGS,
    )
    assert (req.order_type, req.tif, req.aux_price) == ("STP", "GTC", 90.0)
    assert (req.oca_group, req.oca_type) == ("stk-oca-1", 2)


def test_an_exit_shares_the_stop_group_and_an_order_without_one_has_none():
    exit_req = to_ib_order(
        order(side="sell", decision_price=100.0, position_effect="close", oca_group="g"),
        SPEC,
        account="DU1",
        settings=SETTINGS,
    )
    assert (exit_req.oca_group, exit_req.oca_type) == ("g", 2)
    plain = to_ib_order(order(decision_price=100.0), SPEC, account="DU1", settings=SETTINGS)
    assert (plain.oca_group, plain.oca_type) == (None, None)
