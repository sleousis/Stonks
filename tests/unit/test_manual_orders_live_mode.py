"""Whether a manual order is real money follows ``broker_mode``: the
default book at a live IB Gateway trades real money (review 2026-09-27)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stonks.app.manual_orders import ManualOrdersService
from stonks.config import Settings

LIVE_GW = {"g": {"host": "h", "port": 4003, "mode": "live", "portfolios": ["pf_default"]}}


@pytest.mark.parametrize(
    ("brokers", "live"),
    [
        ({}, False),
        ({"kind": "alpaca", "alpaca": {"paper": False, "allow_live": True}}, True),
        ({"kind": "ibkr", "ibkr": {"gateways": LIVE_GW}}, False),
        ({"kind": "ibkr", "ibkr": {"allow_live": True, "gateways": LIVE_GW}}, True),
    ],
)
def test_default_book_is_live_when_the_broker_mode_is_live(brokers, live):
    ctx = SimpleNamespace(settings=Settings(brokers=brokers))
    service = ManualOrdersService(ctx)  # type: ignore[arg-type]
    account = SimpleNamespace(id="pf_default", kind="simulated")
    assert service._live(account) is live  # type: ignore[arg-type]
