"""The IBKR form of option contracts, orders and combos (roadmap 17.8):
each refusal named, and IBKR contracts mapped back only when they are a
US listed option we can name."""

from __future__ import annotations

import math
from datetime import date

import pytest

from stonks.core.combos import ComboLeg, ComboOrder
from stonks.core.options import OptionContract
from stonks.core.types import Order
from stonks.execution.brokers.base import OrderRejectedError, UnsupportedTickerError
from stonks.execution.brokers.ibkr.client import IbContract, IbContractDetails
from stonks.execution.brokers.ibkr.options import (
    combo_contract,
    contract_of,
    matches,
    option_id_for_contract,
    option_query,
    snap_net,
    to_ib_combo_order,
    to_ib_option_order,
    whole_contracts,
)
from stonks.execution.brokers.ibkr.settings import IbkrOrderSettings

EXP = date(2026, 10, 16)
CALL = OptionContract("AAPL.US", EXP, 200.0, "call")
CALL_210 = OptionContract("AAPL.US", EXP, 210.0, "call")
SETTINGS = IbkrOrderSettings()


def ib_option(**kw) -> IbContract:
    base = {"con_id": 1001, "symbol": "AAPL", "sec_type": "OPT", "currency": "USD",
            "last_trade_date": "20261016", "strike": 200.0, "right": "C",
            "multiplier": "100"}  # fmt: skip
    base.update(kw)
    return IbContract(**base)


def details(**kw) -> IbContractDetails:
    return IbContractDetails(contract=ib_option(**kw), min_tick=0.01)


def leg_order(**kw) -> Order:
    base = {"client_id": "cmb-1:0", "ticker": CALL.contract_id, "side": "buy",
            "quantity": 2.0, "order_type": "limit", "limit_price": 3.07}  # fmt: skip
    base.update(kw)
    return Order(**base)


def test_the_query_names_the_series_and_refuses_foreign_underlyings():
    q = option_query(CALL)
    assert (q.symbol, q.sec_type, q.last_trade_date, q.right, q.multiplier) == (
        "AAPL",
        "OPT",
        "20261016",
        "C",
        "100",
    )
    with pytest.raises(UnsupportedTickerError, match="options on LSE"):
        option_query(OptionContract("VOD.LSE", EXP, 1.0, "call", currency="GBP"))


def test_a_match_agrees_on_every_field():
    assert matches(details(), CALL)
    assert not matches(details(sec_type="STK"), CALL)
    assert not matches(details(currency="EUR"), CALL)
    assert not matches(details(last_trade_date="20261120"), CALL)
    assert not matches(details(last_trade_date="not-a-date"), CALL)
    assert not matches(details(last_trade_date=None), CALL)
    assert not matches(details(right="P"), CALL)
    assert not matches(details(right=None), CALL)
    assert not matches(details(strike=None), CALL)
    assert not matches(details(strike=205.0), CALL)
    assert not matches(details(multiplier="abc"), CALL)
    assert not matches(details(multiplier="150"), CALL)
    assert matches(details(multiplier=None), CALL)  # IBKR's default of 100


def test_an_ibkr_contract_maps_back_only_when_we_can_name_it():
    assert option_id_for_contract(ib_option()) == CALL.contract_id
    assert (
        option_id_for_contract(ib_option(right="PUT", multiplier=None))
        == OptionContract("AAPL.US", EXP, 200.0, "put").contract_id
    )
    assert option_id_for_contract(ib_option(symbol="BRK B", strike=400.0)).startswith("BRK-B.US")
    assert option_id_for_contract(ib_option(sec_type="STK")) is None
    assert option_id_for_contract(ib_option(currency="EUR")) is None
    assert option_id_for_contract(ib_option(last_trade_date="")) is None
    assert option_id_for_contract(ib_option(right="X")) is None
    assert option_id_for_contract(ib_option(strike=None)) is None
    assert option_id_for_contract(ib_option(strike=0.0)) is None
    assert option_id_for_contract(ib_option(multiplier="abc")) is None
    assert option_id_for_contract(ib_option(symbol="  ")) is None


def test_contracts_are_whole():
    assert whole_contracts(3.0, "x") == 3
    with pytest.raises(OrderRejectedError, match="whole number"):
        whole_contracts(1.5, "x")
    with pytest.raises(OrderRejectedError, match="whole number"):
        whole_contracts(0.0, "x")


def test_an_option_leg_goes_out_as_a_day_limit():
    req = to_ib_option_order(leg_order(), tick=0.05, account="DU1", settings=SETTINGS)
    assert (req.action, req.order_type, req.tif, req.total_quantity) == ("BUY", "LMT", "DAY", 2.0)
    assert math.isclose(req.limit_price or 0.0, 3.05)  # a buy snaps down to the tick grid
    sell = to_ib_option_order(leg_order(side="sell"), tick=0.05, account="DU1", settings=SETTINGS)
    assert sell.action == "SELL"


@pytest.mark.parametrize(
    ("kw", "account", "match"),
    [
        ({}, "", "no IBKR account"),
        ({"outside_rth": True}, "DU1", "outside regular hours"),
        ({"order_type": "market", "limit_price": None}, "DU1", "never market orders"),
        ({"time_in_force": "gtc"}, "DU1", "day orders, not gtc"),
        ({"quantity": 1.5}, "DU1", "whole number"),
    ],
)
def test_an_option_leg_refuses_what_ibkr_should_never_see(kw, account, match):
    with pytest.raises(OrderRejectedError, match=match):
        to_ib_option_order(leg_order(**kw), tick=0.01, account=account, settings=SETTINGS)


def test_the_net_limit_rounds_down_and_must_be_a_number():
    assert snap_net(1.237, 0.05) == pytest.approx(1.2)
    assert snap_net(-1.23, 0.05) == pytest.approx(-1.25)
    with pytest.raises(OrderRejectedError, match="not a number"):
        snap_net(float("nan"), 0.01)


def spread(**kw) -> ComboOrder:
    base = {"client_id": "cmb-sprd",
            "legs": (ComboLeg("buy", 1.0, CALL), ComboLeg("sell", 1.0, CALL_210)),
            "net_limit": 4.12}  # fmt: skip
    base.update(kw)
    return ComboOrder(**base)


def test_a_combo_contract_is_one_bag_with_a_leg_per_combo_leg():
    bag = combo_contract(spread(), [1001, 1002], "USD")
    assert bag.sec_type == "BAG" and bag.symbol == "AAPL"
    assert [(leg.con_id, leg.ratio, leg.action) for leg in bag.combo_legs] == [
        (1001, 1, "BUY"),
        (1002, 1, "SELL"),
    ]


def test_a_combo_contract_refuses_missing_legs_odd_ratios_and_two_underlyings():
    with pytest.raises(OrderRejectedError, match="every leg"):
        combo_contract(spread(), [1001], "USD")
    odd = spread(legs=(ComboLeg("buy", 1.5, CALL), ComboLeg("sell", 1.0, CALL_210)))
    with pytest.raises(OrderRejectedError, match="whole numbers"):
        combo_contract(odd, [1001, 1002], "USD")
    msft = OptionContract("MSFT.US", EXP, 400.0, "call")
    mixed = spread(legs=(ComboLeg("buy", 1.0, CALL), ComboLeg("sell", 1.0, msft)))
    with pytest.raises(OrderRejectedError, match="one underlying"):
        combo_contract(mixed, [1001, 2001], "USD")


def test_a_combo_order_needs_an_account_and_a_net_limit():
    req = to_ib_combo_order(spread(quantity=2.0), tick=0.01, account="DU1", settings=SETTINGS)
    assert (req.action, req.total_quantity, req.limit_price) == ("BUY", 2.0, 4.12)
    with pytest.raises(OrderRejectedError, match="no IBKR account"):
        to_ib_combo_order(spread(), tick=0.01, account="", settings=SETTINGS)
    with pytest.raises(OrderRejectedError, match="net limit"):
        to_ib_combo_order(spread(net_limit=None), tick=0.01, account="DU1", settings=SETTINGS)


def test_contract_of_refuses_what_is_not_a_contract_id():
    assert contract_of(CALL.contract_id) == CALL
    with pytest.raises(UnsupportedTickerError, match="not an option contract id"):
        contract_of("AAPL.US")
