"""Short outage versus fault (roadmap 19.5): which broker failures skip the
day and which pause auto at once."""

from __future__ import annotations

import pytest

from stonks.connections.base import ProviderError
from stonks.execution.brokers.base import (
    BrokerError,
    BrokerUnavailableError,
    LiveTradingRefusedError,
    OrderOutcomeUnknownError,
    OrderRejectedError,
)
from stonks.production.auto_pause import (
    BROKER_DRIFT,
    BROKER_ERROR,
    broker_failure_kind,
    drift_reason,
    is_short_outage,
)


@pytest.mark.parametrize(
    "error",
    [
        BrokerUnavailableError("gateway down"),
        OrderOutcomeUnknownError("c1", "submit timed out"),
        ConnectionError("socket closed"),
        TimeoutError("no answer"),
    ],
)
def test_an_unreachable_broker_is_a_short_outage(error):
    assert broker_failure_kind(error) == "outage"
    assert is_short_outage(error)


@pytest.mark.parametrize(
    "error",
    [
        LiveTradingRefusedError("wrong account"),
        BrokerError("strange answer"),
        ProviderError("fake broker is down", status=503),
        OrderRejectedError("no"),
        "an order raised at the broker",
    ],
)
def test_anything_else_is_a_fault(error):
    assert broker_failure_kind(error) == "fault"
    assert not is_short_outage(error)


def test_drift_reason_names_the_report():
    reason = drift_reason("rec_abc", 2)
    assert reason.startswith(f"{BROKER_DRIFT}: ")
    assert "rec_abc" in reason and "2" in reason
    assert not reason.startswith(BROKER_ERROR)
