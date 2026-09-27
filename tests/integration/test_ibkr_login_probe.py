"""The IBKR login probe behind the ``broker_health`` job (roadmap 19.2)."""

from __future__ import annotations

from datetime import UTC, datetime

from stonks.core.clock import FakeClock
from stonks.execution.brokers.ibkr.settings import IbkrBrokerConfig, IbkrHealthSettings
from stonks.production.broker_health import GatewayTarget, IbkrLoginProbe, check_gateways
from tests.fakes.ib_gateway import FakeIbGateway

MON = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)
TARGET = GatewayTarget("paper", "gw", 4004, "paper", portfolios=("pf_default",))
CONFIG = IbkrBrokerConfig(
    gateways={"paper": {"host": "gw", "port": 4004, "mode": "paper", "portfolios": ["pf_default"]}}
)


def probe(gateway: FakeIbGateway) -> IbkrLoginProbe:
    return IbkrLoginProbe(CONFIG, client_factory=lambda endpoint: gateway)


def test_login_probe_up():
    result = probe(FakeIbGateway()).probe(TARGET)
    assert result.connected and result.fault is None and result.latency_ms is not None


def test_login_probe_wrong_account_is_a_fault():
    result = probe(FakeIbGateway(["U1"])).probe(TARGET)
    assert not result.connected and result.fault == "wrong_account"


def test_login_probe_down_is_no_fault():
    gw = FakeIbGateway()
    gw.connect_failures = 1
    result = probe(gw).probe(TARGET)
    assert not result.connected and result.fault is None


def test_login_probe_fault_is_stored(state):
    (check,) = check_gateways(
        state,
        [TARGET],
        probe(FakeIbGateway(["U1"])),
        IbkrHealthSettings(),
        clock=FakeClock(MON),
        publish=lambda e: None,
    )
    assert check.status.fault == "wrong_account"
    assert not check.status.connected
