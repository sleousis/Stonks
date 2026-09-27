"""IBKR error codes onto broker exceptions (roadmap 19.2)."""

from __future__ import annotations

import pytest

from stonks.execution.brokers.base import (
    BrokerError,
    BrokerUnavailableError,
    OrderRejectedError,
)
from stonks.execution.brokers.ibkr.client import IbApiError, IbConnectionError
from stonks.execution.brokers.ibkr.errors import classify, is_info, to_broker_error


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        (502, "unavailable"),
        (1100, "link_lost"),
        (1102, "link_restored"),
        (2104, "info"),
        (10197, "competing_session"),
        (201, "rejected"),
        (110, "bad_tick"),
        (354, "no_market_data"),
        (103, "duplicate_order_id"),
        (99999, "other"),
    ],
)
def test_classify(code, kind):
    assert classify(code) == kind


def test_info_codes_are_never_errors():
    assert is_info(2104) and is_info(1101)
    assert not is_info(201)


def test_outages_are_unavailable():
    assert isinstance(to_broker_error(IbConnectionError("x"), action="a"), BrokerUnavailableError)
    assert isinstance(to_broker_error(TimeoutError(), action="a"), BrokerUnavailableError)
    assert isinstance(to_broker_error(IbApiError(504, "x"), action="a"), BrokerUnavailableError)
    assert isinstance(to_broker_error(IbApiError(1100, "x"), action="a"), BrokerUnavailableError)


def test_competing_session_names_the_cause():
    err = to_broker_error(IbApiError(10197, "competing"), action="submit")
    assert isinstance(err, BrokerUnavailableError)
    assert "username" in str(err)


def test_rejections_carry_ibkr_text():
    err = to_broker_error(IbApiError(201, "Order rejected - reason: no funds"), action="submit")
    assert isinstance(err, OrderRejectedError)
    assert "no funds" in str(err)
    assert isinstance(to_broker_error(IbApiError(110, "tick"), action="s"), OrderRejectedError)


def test_other_errors_are_plain_broker_errors():
    err = to_broker_error(IbApiError(321, "bad request"), action="x")
    assert type(err) is BrokerError
    assert type(to_broker_error(ValueError("boom"), action="x")) is BrokerError
